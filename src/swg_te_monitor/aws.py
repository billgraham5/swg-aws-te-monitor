"""AWS discovery, preflight, and CloudFormation orchestration."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from .locations import LOCATIONS, Location, agent_name
from .models import DeploymentConfig

PROJECT_NAME = "cisco-secure-access-te-monitor"
INSTANCE_TYPE_CANDIDATES = ("t3.small", "t3.medium", "t2.medium")
# Every parent Region a location can map to, searched when locating the Region
# that already holds this project's control-plane resources.
CANDIDATE_HOME_REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2")
DEFAULT_HOME_REGION = "us-east-1"


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedTarget:
    location: Location
    zone_name: str
    zone_type: str
    opt_in_status: str
    instance_type: str = "t3.small"
    eip_allocation_id: str | None = None
    eip_public_ip: str | None = None
    # Set when the zone offered none of the preferred burstable types and a
    # larger, costlier one had to be chosen, so the operator can be asked first.
    substituted_instance_type: bool = False
    instance_memory_gib: float | None = None


class AwsFacade:
    def __init__(
        self, profile: str | None = None, reporter: Callable[[str], None] | None = None
    ) -> None:
        self.session = boto3.Session(profile_name=profile)
        self.reporter = reporter or (lambda _message: None)

    def identity(self) -> dict[str, Any]:
        identity = self.session.client("sts").get_caller_identity()
        return cast(dict[str, Any], identity)

    def resolve_target(self, location: Location) -> ResolvedTarget:
        if location.preferred_zone_group is None:
            zones = self.session.client(
                "ec2", region_name=location.region
            ).describe_availability_zones(
                Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}],
            )["AvailabilityZones"]
            available = next((z for z in zones if z["State"] == "available"), None)
        else:
            zones = self.session.client(
                "ec2", region_name=location.region
            ).describe_availability_zones(
                AllAvailabilityZones=True,
                Filters=[
                    {"Name": "zone-type", "Values": ["local-zone"]},
                    {"Name": "group-name", "Values": [location.preferred_zone_group]},
                ],
            )["AvailabilityZones"]
            available = next(
                (
                    z
                    for z in zones
                    if z["State"] == "available" and z.get("OptInStatus") == "opted-in"
                ),
                None,
            )
        if available is None:
            group = location.preferred_zone_group or location.region
            raise PreflightError(
                f"{location.label}: {group} is not available to this account; "
                "opt in under EC2 Settings > Zones, or contact AWS Support if access is restricted"
            )
        return ResolvedTarget(
            location=location,
            zone_name=available["ZoneName"],
            zone_type=available["ZoneType"],
            opt_in_status=available.get("OptInStatus", "opt-in-not-required"),
        )

    def preflight(self, config: DeploymentConfig) -> list[ResolvedTarget]:
        try:
            self.identity()
            targets = []
            for key in config.locations:
                self.reporter(f"Finding an available AWS zone for {LOCATIONS[key].label}...")
                targets.append(self.resolve_target(LOCATIONS[key]))
            for target_index, target in enumerate(targets):
                candidates = list(dict.fromkeys((config.instance_type, *INSTANCE_TYPE_CANDIDATES)))
                self.reporter(
                    f"Checking instance sizes {', '.join(candidates)}, the Amazon Linux image, "
                    f"and access settings in {target.zone_name}..."
                )
                ec2 = self.session.client("ec2", region_name=target.location.region)
                paginator = ec2.get_paginator("describe_instance_type_offerings")
                pages = paginator.paginate(
                    LocationType="availability-zone",
                    Filters=[{"Name": "location", "Values": [target.zone_name]}],
                )
                offered = {
                    item["InstanceType"] for page in pages for item in page["InstanceTypeOfferings"]
                }
                matched = [
                    instance_type for instance_type in candidates if instance_type in offered
                ]
                self.reporter(
                    f"AWS reports these affordable options in {target.zone_name}: "
                    f"{', '.join(matched) if matched else 'none'}."
                )
                selected_type = _select_instance_type(config.instance_type, offered)
                substituted, memory_gib = False, None
                if selected_type is None:
                    # BrowserBot needs headroom beyond the 2 GiB the agent alone
                    # wants, which is why t3.small is rejected for it elsewhere.
                    minimum_gib = 4.0 if config.browserbot else 2.0
                    fallback = _smallest_supported_type(ec2, offered, minimum_gib)
                    if fallback is None:
                        raise PreflightError(
                            f"{target.zone_name} offers no x86_64 instance type with at least "
                            f"{minimum_gib:g} GiB of memory, so {target.location.label} cannot be "
                            "deployed there with the current image."
                        )
                    selected_type, selected_gib = fallback
                    substituted, memory_gib = True, selected_gib
                    self.reporter(
                        f"{target.zone_name} offers none of {', '.join(candidates)}; using the "
                        f"smallest suitable type it does offer, {selected_type} "
                        f"({selected_gib:g} GiB)."
                    )
                if selected_type != config.instance_type:
                    self.reporter(
                        f"{config.instance_type} is unavailable in {target.zone_name}; "
                        f"using {selected_type} instead."
                    )
                target = ResolvedTarget(
                    location=target.location,
                    zone_name=target.zone_name,
                    zone_type=target.zone_type,
                    opt_in_status=target.opt_in_status,
                    instance_type=selected_type,
                    substituted_instance_type=substituted,
                    instance_memory_gib=memory_gib,
                )
                targets[target_index] = target
                self.session.client("ssm", region_name=target.location.region).get_parameter(
                    Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
                )
                if config.ssh_enabled:
                    keys = ec2.describe_key_pairs(KeyNames=[config.key_pair_name])["KeyPairs"]
                    if not keys:
                        raise PreflightError(f"key pair is missing in {target.location.region}")
            return targets
        except (BotoCoreError, ClientError) as exc:
            raise PreflightError(f"AWS preflight failed: {exc}") from exc

    def _discover_stack_sets(self, name: str) -> tuple[list[str], dict[str, str]]:
        """Return the Regions holding a StackSet of this name, and who manages what."""
        homes: list[str] = []
        managed_by: dict[str, str] = {}
        for region in CANDIDATE_HOME_REGIONS:
            client = self.session.client("cloudformation", region_name=region)
            try:
                client.describe_stack_set(StackSetName=name)
            except ClientError as exc:
                if "not found" in str(exc).lower():
                    continue
                raise
            homes.append(region)
            for summary in client.list_stack_instances(StackSetName=name).get("Summaries", []):
                managed_by.setdefault(summary["Region"], region)
        return homes, managed_by

    def home_region(
        self, target_regions: tuple[str, ...] | None = None, stack_set_name: str | None = None
    ) -> str:
        """Return the Region of the StackSet that owns the Regions being deployed.

        A StackSet is a Regional resource, and nothing stops an account from
        holding more than one of the same name -- an earlier version of this
        code derived the Region from the current selection and created exactly
        that. Picking the wrong one would deploy a second stack instance into a
        Region another StackSet already manages, duplicating VPCs and instances
        over running agents, so resolve by ownership and refuse to guess when
        ownership is ambiguous.
        """
        name = stack_set_name or f"{PROJECT_NAME}-locations"
        homes, managed_by = self._discover_stack_sets(name)
        if not homes:
            return DEFAULT_HOME_REGION
        owners = {managed_by[region] for region in target_regions or () if region in managed_by}
        if len(owners) > 1:
            raise PreflightError(
                f"The {', '.join(sorted(target_regions or ()))} deployment spans StackSets in "
                f"{', '.join(sorted(owners))}, which cannot be updated as one operation. "
                "Deploy those locations separately, or consolidate the StackSets."
            )
        if owners:
            return owners.pop()
        if len(homes) == 1:
            return homes[0]
        raise PreflightError(
            f"Found StackSets named {name} in {', '.join(homes)} and none of them manages "
            f"{', '.join(sorted(target_regions or ())) or 'the requested Regions'}. "
            "Consolidate them, or remove the unused one, before deploying."
        )

    def resolve_eips(
        self, config: DeploymentConfig, targets: list[ResolvedTarget], dry_run: bool = False
    ) -> list[ResolvedTarget]:
        """Reuse or allocate one Elastic IP per selected location, tag-matched by hostname.

        Elastic IPs are never released by this tool -- once allocated they must
        stay registered as a Cisco Secure Access Network, so releasing one would
        silently break that location and risks it being reused by AWS for an
        unrelated account. In dry-run mode, a missing EIP is reported but not
        allocated.
        """
        resolved: list[ResolvedTarget] = []
        for target in targets:
            hostname = agent_name(target.location, config.agent_suffix)
            # Local Zone subnets sit in a distinct network border group from their
            # parent region; an Elastic IP allocated for the parent region cannot be
            # associated with an instance in the Local Zone (AWS rejects it with
            # "Cannot associate addresses across network border groups").
            expected_border_group = target.location.preferred_zone_group or target.location.region
            ec2 = self.session.client("ec2", region_name=target.location.region)
            self.reporter(
                f"Checking for an existing Elastic IP tagged {hostname} in "
                f"{target.location.region}..."
            )
            addresses = ec2.describe_addresses(
                Filters=[{"Name": "tag:Name", "Values": [hostname]}]
            )["Addresses"]
            allocation_id: str | None
            public_ip: str | None
            if addresses:
                address = addresses[0]
                border_group = address.get("NetworkBorderGroup")
                if border_group != expected_border_group:
                    raise PreflightError(
                        f"Elastic IP {address['PublicIp']} ({address['AllocationId']}) tagged "
                        f"{hostname} is in network border group {border_group!r} but "
                        f"{target.location.label} requires {expected_border_group!r}; it cannot "
                        "be associated with an instance there. Re-tag or release this address "
                        "manually, then rerun so a correctly-scoped Elastic IP can be allocated."
                    )
                allocation_id = address["AllocationId"]
                public_ip = address["PublicIp"]
                self.reporter(
                    f"Reusing existing Elastic IP {public_ip} ({allocation_id}) for {hostname}."
                )
            elif dry_run:
                allocation_id = None
                public_ip = None
                self.reporter(
                    f"[dry run] Would allocate a new Elastic IP for {hostname} in "
                    f"{target.location.region}."
                )
            else:
                self.reporter(
                    f"Allocating a new Elastic IP for {hostname} in {target.location.region}..."
                )
                allocated = ec2.allocate_address(
                    Domain="vpc", NetworkBorderGroup=expected_border_group
                )
                allocation_id = allocated["AllocationId"]
                public_ip = allocated["PublicIp"]
                ec2.create_tags(
                    Resources=[allocation_id],
                    Tags=[
                        {"Key": "Name", "Value": hostname},
                        {"Key": "Project", "Value": PROJECT_NAME},
                    ],
                )
                self.reporter(f"Allocated Elastic IP {public_ip} ({allocation_id}) for {hostname}.")
            resolved.append(
                ResolvedTarget(
                    location=target.location,
                    zone_name=target.zone_name,
                    zone_type=target.zone_type,
                    opt_in_status=target.opt_in_status,
                    instance_type=target.instance_type,
                    eip_allocation_id=allocation_id,
                    eip_public_ip=public_ip,
                    substituted_instance_type=target.substituted_instance_type,
                    instance_memory_gib=target.instance_memory_gib,
                )
            )
        return resolved

    def enable_local_zones(self, config: DeploymentConfig, template: Path) -> None:
        targets = sorted(
            {
                f"{location.region}|{location.preferred_zone_group}"
                for key in config.locations
                if (location := LOCATIONS[key]).preferred_zone_group is not None
            }
        )
        if not targets:
            self.reporter("No Local Zone opt-in is needed for the selected locations.")
            return
        admin_region = self.home_region(
            tuple(sorted({LOCATIONS[key].region for key in config.locations}))
        )
        cfn = self.session.client("cloudformation", region_name=admin_region)
        stack_name = f"{PROJECT_NAME}-local-zone-opt-in"
        parameters = [{"ParameterKey": "ZoneTargets", "ParameterValue": ",".join(targets)}]
        try:
            cfn.describe_stacks(StackName=stack_name)
            try:
                self.reporter("Updating the AWS Local Zone opt-in helper stack...")
                cfn.update_stack(
                    StackName=stack_name,
                    TemplateBody=template.read_text(encoding="utf-8"),
                    Parameters=parameters,
                    Capabilities=["CAPABILITY_NAMED_IAM"],
                )
                self._wait_stack(cfn, stack_name, "stack_update_complete")
            except ClientError as exc:
                if "No updates are to be performed" not in str(exc):
                    raise
        except ClientError as exc:
            if "does not exist" not in str(exc):
                raise
            self.reporter("Creating the AWS Local Zone opt-in helper stack...")
            cfn.create_stack(
                StackName=stack_name,
                TemplateBody=template.read_text(encoding="utf-8"),
                Parameters=parameters,
                Capabilities=["CAPABILITY_NAMED_IAM"],
                OnFailure="DO_NOTHING",
            )
            self._wait_stack(cfn, stack_name, "stack_create_complete")

    def deploy(
        self, config: DeploymentConfig, targets: list[ResolvedTarget], template: Path
    ) -> None:
        regional_template = template.with_name("regional.yaml")
        permissions_template = template.with_name("stackset-permissions.yaml")
        regional_body = regional_template.read_text(encoding="utf-8")
        location_bytes = template.read_bytes()
        bootstrap = template.parents[1] / "bootstrap" / "bootstrap.py"
        bootstrap_bytes = bootstrap.read_bytes()
        bootstrap_hash = sha256(bootstrap_bytes).hexdigest()
        account = self.identity()["Account"]
        regions = sorted({target.location.region for target in targets})
        assets: dict[str, tuple[str, str, str]] = {}
        for region in regions:
            self.reporter(f"Preparing private deployment assets in {region}...")
            s3 = self.session.client("s3", region_name=region)
            bucket = f"{PROJECT_NAME}-{account}-{region}-bootstrap"
            self._ensure_asset_bucket(s3, bucket, region)
            asset_key = f"assets/bootstrap-{bootstrap_hash}.py"
            s3.put_object(
                Bucket=bucket, Key=asset_key, Body=bootstrap_bytes, ServerSideEncryption="AES256"
            )
            location_key = f"assets/location-{sha256(location_bytes).hexdigest()}.yaml"
            s3.put_object(
                Bucket=bucket, Key=location_key, Body=location_bytes, ServerSideEncryption="AES256"
            )
            url = f"https://{bucket}.s3.{region}.amazonaws.com/{location_key}"
            assets[region] = (bucket, asset_key, url)

        admin_region = self.home_region(tuple(regions))
        cfn = self.session.client("cloudformation", region_name=admin_region)
        permission_stack = f"{PROJECT_NAME}-stackset-permissions"
        permission_parameters = [
            {"ParameterKey": "BootstrapBucketPrefix", "ParameterValue": PROJECT_NAME}
        ]
        role_names = (
            "AWSCloudFormationStackSetAdministrationRole",
            "AWSCloudFormationStackSetExecutionRole",
        )
        iam = self.session.client("iam")
        existing_roles: list[str] = []
        can_inspect_roles = True
        for role_name in role_names:
            try:
                iam.get_role(RoleName=role_name)
                existing_roles.append(role_name)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                    can_inspect_roles = False
                    break
        partition = self.session.get_partition_for_region(admin_region)
        if can_inspect_roles and existing_roles:
            if len(existing_roles) == len(role_names):
                self.reporter(
                    "Validating and updating the existing CloudFormation StackSet service roles..."
                )
            else:
                missing = sorted(set(role_names) - set(existing_roles))
                self.reporter(
                    "Repairing the incomplete CloudFormation StackSet service-role pair; "
                    f"creating {', '.join(missing)}..."
                )
            self._ensure_stackset_roles(iam, account, partition, set(existing_roles))
        else:
            try:
                described = cfn.describe_stacks(StackName=permission_stack)
            except ClientError as exc:
                if "does not exist" not in str(exc):
                    raise
                self.reporter("Creating the CloudFormation StackSet service roles...")
                cfn.create_stack(
                    StackName=permission_stack,
                    TemplateBody=permissions_template.read_text(encoding="utf-8"),
                    Parameters=permission_parameters,
                    Capabilities=["CAPABILITY_NAMED_IAM"],
                    OnFailure="DO_NOTHING",
                )
                self._wait_stack(cfn, permission_stack, "stack_create_complete")
            else:
                stack_status = described["Stacks"][0]["StackStatus"]
                if can_inspect_roles and not existing_roles:
                    self.reporter(
                        f"The permission stack is {stack_status} but its roles are missing; "
                        "recreating and configuring both service roles..."
                    )
                    self._ensure_stackset_roles(iam, account, partition, set())

        stackset_name = f"{PROJECT_NAME}-locations"

        def deployed_values(region: str) -> dict[str, str]:
            try:
                summaries = cfn.list_stack_instances(
                    StackSetName=stackset_name,
                    StackInstanceAccount=account,
                    StackInstanceRegion=region,
                ).get("Summaries", [])
            except ClientError:
                return {}
            stack_id = summaries[0].get("StackId") if summaries else None
            return _deployed_regional_values(cfn, stack_id) if stack_id else {}

        stackset_exists = True
        try:
            cfn.describe_stack_set(StackSetName=stackset_name)
        except ClientError as exc:
            if "not found" not in str(exc).lower():
                raise
            stackset_exists = False
        first_parameters = _regional_parameters(
            config,
            targets,
            regions[0],
            assets[regions[0]],
            bootstrap_hash,
            deployed_values(regions[0]) if stackset_exists else {},
        )
        role_arn = (
            f"arn:{partition}:iam::{account}:role/AWSCloudFormationStackSetAdministrationRole"
        )
        refreshed_regions: set[str] = set()
        if stackset_exists:
            self._wait_stackset_idle(cfn, stackset_name)
        if not stackset_exists:
            self.reporter("Creating the Cisco Secure Access monitoring StackSet...")
            cfn.create_stack_set(
                StackSetName=stackset_name,
                Description="Cisco Secure Access ThousandEyes regional monitoring bundle",
                TemplateBody=regional_body,
                Parameters=first_parameters,
                PermissionModel="SELF_MANAGED",
                AdministrationRoleARN=role_arn,
                ExecutionRoleName="AWSCloudFormationStackSetExecutionRole",
                Capabilities=["CAPABILITY_NAMED_IAM"],
            )
        else:
            # update_stack_set updates the StackSet's registered template and
            # Parameters regardless of whether its own automatic instance-sync
            # operation succeeds. That sync can fail for an existing region whose
            # stored per-instance parameter overrides still reference names the
            # new template no longer declares (e.g. after a parameter rename).
            # Deliberately updating overrides for that region *before* this call
            # would use the stale, not-yet-updated template instead -- which,
            # for a region with no live underlying stack, would trigger a full,
            # slow rebuild on the wrong template. So the sync failure here is
            # tolerated: the per-region refresh loop below applies a complete,
            # current override set against the now-updated template and fixes
            # any such region regardless of how this call's own sync went.
            self.reporter("Updating the Cisco Secure Access monitoring StackSet...")
            response = cfn.update_stack_set(
                StackSetName=stackset_name,
                TemplateBody=regional_body,
                Parameters=first_parameters,
                AdministrationRoleARN=role_arn,
                ExecutionRoleName="AWSCloudFormationStackSetExecutionRole",
                Capabilities=["CAPABILITY_NAMED_IAM"],
            )
            if response.get("OperationId"):
                try:
                    self._wait_stackset_operation(cfn, stackset_name, response["OperationId"])
                except RuntimeError as exc:
                    self.reporter(
                        "The automatic StackSet sync reported a failure (commonly stale "
                        f"per-region parameter overrides): {exc}. Continuing to refresh each "
                        "region explicitly."
                    )
            for region in regions:
                summaries = cfn.list_stack_instances(
                    StackSetName=stackset_name,
                    StackInstanceAccount=account,
                    StackInstanceRegion=region,
                ).get("Summaries", [])
                if not summaries:
                    continue
                self.reporter(f"Refreshing the existing StackSet parameters in {region}...")
                overrides = _regional_parameters(
                    config, targets, region, assets[region], bootstrap_hash, deployed_values(region)
                )
                self._wait_stackset_idle(cfn, stackset_name)
                operation = cfn.update_stack_instances(
                    StackSetName=stackset_name,
                    Accounts=[account],
                    Regions=[region],
                    ParameterOverrides=overrides,
                    OperationPreferences={
                        "RegionConcurrencyType": "SEQUENTIAL",
                        "FailureToleranceCount": 0,
                        "MaxConcurrentCount": 1,
                    },
                )
                self._wait_stackset_operation(cfn, stackset_name, operation["OperationId"])
                refreshed_regions.add(region)

        for region in regions:
            if region in refreshed_regions:
                continue
            self.reporter(f"Deploying the monitoring stack in {region}...")
            overrides = _regional_parameters(
                config, targets, region, assets[region], bootstrap_hash, deployed_values(region)
            )
            summaries = cfn.list_stack_instances(
                StackSetName=stackset_name,
                StackInstanceAccount=account,
                StackInstanceRegion=region,
            ).get("Summaries", [])
            self._wait_stackset_idle(cfn, stackset_name)
            operation = (
                cfn.update_stack_instances(
                    StackSetName=stackset_name,
                    Accounts=[account],
                    Regions=[region],
                    ParameterOverrides=overrides,
                    OperationPreferences={
                        "RegionConcurrencyType": "SEQUENTIAL",
                        "FailureToleranceCount": 0,
                        "MaxConcurrentCount": 1,
                    },
                )
                if summaries
                else cfn.create_stack_instances(
                    StackSetName=stackset_name,
                    Accounts=[account],
                    Regions=[region],
                    ParameterOverrides=overrides,
                    OperationPreferences={
                        "RegionConcurrencyType": "SEQUENTIAL",
                        "FailureToleranceCount": 0,
                        "MaxConcurrentCount": 1,
                    },
                )
            )
            self._wait_stackset_operation(cfn, stackset_name, operation["OperationId"])

    @staticmethod
    def _ensure_stackset_roles(
        iam: Any, account: str, partition: str, existing_roles: set[str]
    ) -> None:
        administration_role = "AWSCloudFormationStackSetAdministrationRole"
        execution_role = "AWSCloudFormationStackSetExecutionRole"
        cloudformation_trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "cloudformation.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        if administration_role not in existing_roles:
            iam.create_role(
                RoleName=administration_role,
                AssumeRolePolicyDocument=json.dumps(cloudformation_trust),
                Description="Administration role for same-account CloudFormation StackSets",
            )
        iam.update_assume_role_policy(
            RoleName=administration_role,
            PolicyDocument=json.dumps(cloudformation_trust),
        )
        iam.put_role_policy(
            RoleName=administration_role,
            PolicyName="AssumeStackSetExecutionRole",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Resource": f"arn:{partition}:iam::{account}:role/{execution_role}",
                        }
                    ],
                }
            ),
        )
        if execution_role not in existing_roles:
            iam.create_role(
                RoleName=execution_role,
                AssumeRolePolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {
                                    "AWS": (
                                        f"arn:{partition}:iam::{account}:role/{administration_role}"
                                    )
                                },
                                "Action": "sts:AssumeRole",
                            }
                        ],
                    }
                ),
                Description="Execution role for same-account CloudFormation StackSets",
            )
        execution_trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": f"arn:{partition}:iam::{account}:role/{administration_role}"
                    },
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        iam.update_assume_role_policy(
            RoleName=execution_role,
            PolicyDocument=json.dumps(execution_trust),
        )
        iam.attach_role_policy(
            RoleName=execution_role,
            PolicyArn=f"arn:{partition}:iam::aws:policy/AdministratorAccess",
        )
        iam.put_role_policy(
            RoleName=execution_role,
            PolicyName="ReadPrivateStackSetAssets",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "s3:GetObject",
                            "Resource": (
                                f"arn:{partition}:s3:::{PROJECT_NAME}-{account}"
                                "-*-bootstrap/assets/*"
                            ),
                        }
                    ],
                }
            ),
        )
        for role_name in (administration_role, execution_role):
            iam.get_waiter("role_exists").wait(
                RoleName=role_name,
                WaiterConfig={"Delay": 2, "MaxAttempts": 30},
            )

    @staticmethod
    def _ensure_asset_bucket(s3: Any, bucket: str, region: str) -> None:
        try:
            s3.head_bucket(Bucket=bucket)
            return
        except ClientError:
            args: dict[str, Any] = {"Bucket": bucket}
            if region != "us-east-1":
                args["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**args)
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )

    @staticmethod
    def _wait_stack(cfn: Any, name: str, waiter: str) -> None:
        try:
            cfn.get_waiter(waiter).wait(
                StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 60}
            )
        except (WaiterError, ClientError) as exc:
            reason = AwsFacade._stack_failure_reason(cfn, name)
            raise RuntimeError(f"CloudFormation stack {name} failed: {reason}") from exc

    @staticmethod
    def _stack_failure_reason(cfn: Any, name: str) -> str:
        try:
            events = cfn.describe_stack_events(StackName=name).get("StackEvents", [])
        except ClientError:
            return "AWS removed the failed stack before its resource error could be read"
        failed_events = [
            event
            for event in events
            if str(event.get("ResourceStatus", "")).endswith("FAILED")
            and event.get("ResourceStatusReason")
        ]
        for event in failed_events:
            if event.get("ResourceType") != "AWS::CloudFormation::Stack":
                resource = event.get("LogicalResourceId", "unknown resource")
                return f"{resource}: {event['ResourceStatusReason']}"
        if failed_events:
            event = failed_events[0]
            resource = event.get("LogicalResourceId", "unknown resource")
            return f"{resource}: {event['ResourceStatusReason']}"
        return "no failed resource reason was returned by AWS"

    def _wait_stackset_idle(self, cfn: Any, name: str, attempts: int = 180) -> None:
        """Block until no StackSet operation is in flight.

        CloudFormation allows only one operation per StackSet at a time and
        starts some of its own (for example reconciling a stack instance whose
        underlying stack was deleted). Without this, an unrelated in-flight
        operation makes the very first create/update call raise
        OperationInProgressException and aborts the whole deployment.
        """
        reported = False
        for _ in range(attempts):
            busy = [
                operation
                for operation in cfn.list_stack_set_operations(
                    StackSetName=name, MaxResults=10
                ).get("Summaries", [])
                if operation["Status"] in {"RUNNING", "STOPPING"}
            ]
            if not busy:
                return
            if not reported:
                reported = True
                self.reporter(
                    "Waiting for an in-flight CloudFormation StackSet operation "
                    f"({busy[0]['OperationId']}) to finish..."
                )
            time.sleep(10)
        raise RuntimeError(
            f"StackSet {name} still had an operation in flight after waiting; retry once it settles"
        )

    @staticmethod
    def _wait_stackset_operation(cfn: Any, name: str, operation_id: str) -> None:
        for _ in range(180):
            operation = cfn.describe_stack_set_operation(
                StackSetName=name, OperationId=operation_id
            )["StackSetOperation"]
            status = operation["Status"]
            if status == "SUCCEEDED":
                return
            if status in {"FAILED", "STOPPED"}:
                reasons = []
                if operation.get("StatusReason"):
                    reasons.append(str(operation["StatusReason"]))
                results = cfn.list_stack_set_operation_results(
                    StackSetName=name, OperationId=operation_id
                ).get("Summaries", [])
                for result in results:
                    if result.get("StatusReason"):
                        target = (
                            f"{result.get('Account', 'unknown')}/{result.get('Region', 'unknown')}"
                        )
                        reasons.append(f"{target}: {result['StatusReason']}")
                detail = "; ".join(dict.fromkeys(reasons)) or "AWS returned no failure reason"
                raise RuntimeError(
                    f"StackSet operation {operation_id} ended with {status}: {detail}"
                )
            time.sleep(10)
        raise RuntimeError(f"StackSet operation {operation_id} timed out")


def _select_instance_type(requested: str, offered: set[str]) -> str | None:
    """Return the first preferred type the zone offers, or None if it offers none."""
    candidates = list(dict.fromkeys((requested, *INSTANCE_TYPE_CANDIDATES)))
    return next((instance_type for instance_type in candidates if instance_type in offered), None)


def _smallest_supported_type(
    ec2: Any, offered: set[str], minimum_gib: float, architecture: str = "x86_64"
) -> tuple[str, float] | None:
    """Return the smallest offered type that can actually run the agent.

    Some zones offer nothing from the preferred list -- the Dallas Local Zone
    offers no burstable type at all -- so the choice has to come from what is
    really there. Architecture matters as much as size: that zone's smallest
    offering by memory is Graviton, which cannot boot the x86_64 image this
    project pins.
    """
    usable: list[tuple[int, float, str]] = []
    types = sorted(offered)
    for start in range(0, len(types), 100):
        described = ec2.describe_instance_types(InstanceTypes=types[start : start + 100])
        for item in described["InstanceTypes"]:
            if architecture not in item["ProcessorInfo"]["SupportedArchitectures"]:
                continue
            gib = item["MemoryInfo"]["SizeInMiB"] / 1024
            if gib + 1e-9 < minimum_gib:
                continue
            usable.append((item["VCpuInfo"]["DefaultVCpus"], gib, item["InstanceType"]))
    if not usable:
        return None
    vcpus, gib, name = min(usable)
    return name, gib


def _stack_parameters(config: DeploymentConfig, target: ResolvedTarget) -> list[dict[str, str]]:
    values = {
        "LocationCode": target.location.code,
        "AvailabilityZone": target.zone_name,
        "AgentSuffix": config.agent_suffix or "",
        "VpcCidr": str(config.vpc_cidr),
        "InstanceType": target.instance_type,
        "EipAllocationId": target.eip_allocation_id or "",
        "EipPublicIp": target.eip_public_ip or "",
        "ThousandEyesToken": config.thousandeyes_token.get_secret_value(),
        "PacUrl": config.pac_url.get_secret_value(),
        "SshEnabled": "true" if config.ssh_enabled else "false",
        "AdminCidr": str(config.admin_cidr or "127.0.0.1/32"),
        "KeyPairName": config.key_pair_name or "",
    }
    return [{"ParameterKey": key, "ParameterValue": value} for key, value in values.items()]


SLOT_KEYS = (
    "Code",
    "Zone",
    "InstanceType",
    "EipAllocationId",
    "EipPublicIp",
    "BootstrapKey",
    "BootstrapSha256",
)


def _regional_parameters(
    config: DeploymentConfig,
    targets: list[ResolvedTarget],
    region: str,
    assets: tuple[str, str, str],
    bootstrap_hash: str,
    deployed: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    # A slot number is a property of the location itself, never of which
    # locations happen to be selected. Each slot owns a fixed VPC CIDR and a
    # nested stack in regional.yaml, so if slots were packed in selection order
    # then deploying a different subset would hand one location's live stack to
    # another -- changing that stack's availability zone and forcing a subnet
    # replacement into a CIDR its predecessor still holds, which AWS rejects
    # outright. Deriving the slot from the region's full location list keeps
    # every location pinned to its own stack as the selection changes.
    regional_slots = {
        location.key: index
        for index, location in enumerate(
            sorted(
                (item for item in LOCATIONS.values() if item.region == region),
                key=lambda item: item.key,
            ),
            start=1,
        )
    }
    if len(regional_slots) > 4:
        raise PreflightError(f"regional template supports at most four locations in {region}")
    slotted = {
        regional_slots[target.location.key]: target
        for target in targets
        if target.location.region == region
    }
    bucket, bootstrap_key, location_url = assets
    values = {
        "LocationTemplateUrl": location_url,
        "ThousandEyesToken": config.thousandeyes_token.get_secret_value(),
        "PacUrl": config.pac_url.get_secret_value(),
        # The bootstrap asset is passed per slot, not once per region, so that
        # refreshing one location cannot rewrite another location's user data.
        "BootstrapBucket": bucket,
        "SshEnabled": "true" if config.ssh_enabled else "false",
        "AdminCidr": str(config.admin_cidr or "127.0.0.1/32"),
        "KeyPairName": config.key_pair_name or "",
        "AgentSuffix": config.agent_suffix or "",
    }
    deployed = deployed or {}
    for index in range(1, 5):
        target = slotted.get(index)
        if target is not None:
            values[f"Location{index}Code"] = target.location.code
            values[f"Location{index}Zone"] = target.zone_name
            values[f"Location{index}InstanceType"] = target.instance_type
            values[f"Location{index}EipAllocationId"] = target.eip_allocation_id or ""
            values[f"Location{index}EipPublicIp"] = target.eip_public_ip or ""
            values[f"Location{index}BootstrapKey"] = bootstrap_key
            values[f"Location{index}BootstrapSha256"] = bootstrap_hash
            continue
        # This slot belongs to a location that is not part of this deployment.
        # Carry its deployed values through untouched -- including the bootstrap
        # asset, so its user data is byte-identical and its instance is not
        # replaced. Emitting blanks instead would clear the slot's condition and
        # delete a running location's stack.
        for suffix in SLOT_KEYS:
            key = f"Location{index}{suffix}"
            fallback = ""
            if suffix == "InstanceType":
                fallback = "t3.small"
            elif suffix in {"BootstrapKey", "BootstrapSha256"}:
                # Stacks deployed before the bootstrap asset became per-slot
                # carry it as one regional value; keep using that one so the
                # location's user data does not change underneath it.
                fallback = deployed.get(suffix, "")
            values[key] = deployed.get(key) or fallback
    return [{"ParameterKey": key, "ParameterValue": value} for key, value in values.items()]


LIVE_STACK_STATUSES = frozenset(
    {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
        # A stack in cleanup has already reached its new state and is only
        # discarding resources the update replaced. It is standing, and its
        # parameters are the ones now deployed. Treating these as "not live"
        # blanks every slot in the Region and deletes the running locations.
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
    }
)

# Nothing usable is standing, so carrying nothing forward is correct.
GONE_STACK_STATUSES = frozenset(
    {
        "DELETE_COMPLETE",
        "DELETE_FAILED",
        "CREATE_FAILED",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
    }
)


def _deployed_regional_values(
    cfn: Any, stack_id: str, *, attempts: int = 60, delay: int = 10
) -> dict[str, str]:
    """Read the parameters a region's bundle stack is currently deployed with.

    Only a stack that is actually standing has values worth carrying forward.
    CloudFormation still answers for a deleted or failed stack, and its
    parameters describe an attempt that did not work -- carrying those forward
    re-creates a location that never deployed, using the settings that failed
    it, and one failed slot rolls back the whole Region including the locations
    being deployed alongside it.

    Anything still moving is waited out rather than read. A stack mid-update
    may report parameters belonging to neither the old nor the new state, and
    an unreadable answer must never become an empty one: a blank slot clears
    its condition and deletes whatever is running in it. When the state cannot
    be established this refuses instead, because refusing costs a re-run and
    guessing costs a production agent.
    """
    for attempt in range(attempts):
        try:
            stack = cfn.describe_stacks(StackName=stack_id)["Stacks"][0]
        except ClientError:
            return {}
        status = str(stack.get("StackStatus", ""))
        if status in LIVE_STACK_STATUSES:
            return {
                parameter["ParameterKey"]: parameter.get("ParameterValue", "")
                for parameter in stack.get("Parameters", [])
            }
        if status in GONE_STACK_STATUSES:
            return {}
        if not status.endswith("_IN_PROGRESS"):
            raise RuntimeError(
                f"{stack_id} is {status}, which is not a state this can safely carry "
                "forward from; re-run once the stack has settled"
            )
        if attempt < attempts - 1:
            time.sleep(delay)
    raise RuntimeError(
        f"{stack_id} was still in progress after {attempts * delay}s; re-run once it settles "
        "rather than deploying against an unknown state"
    )
