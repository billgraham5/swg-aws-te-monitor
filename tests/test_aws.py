from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from swg_te_monitor.aws import (
    AwsFacade,
    PreflightError,
    ResolvedTarget,
    _deployed_regional_values,
    _regional_parameters,
    _select_instance_type,
    _smallest_supported_type,
    _stack_parameters,
)
from swg_te_monitor.locations import LOCATIONS
from swg_te_monitor.models import DeploymentConfig

BASE = {
    "locations": ["denver"],
    "thousandeyes_token": "unit-test-token",
    "pac_url": "https://proxy.example.invalid/proxy.pac",
}


def test_stack_parameters_use_noecho_parameter_names() -> None:
    config = DeploymentConfig.model_validate(BASE)
    target = ResolvedTarget(
        LOCATIONS["denver"],
        "us-west-2-den-1a",
        "local-zone",
        "opted-in",
        eip_allocation_id="eipalloc-0123456789abcdef0",
        eip_public_ip="192.0.2.99",
    )
    parameters = _stack_parameters(config, target)
    rendered = str(parameters)
    assert "ThousandEyesToken" in rendered
    assert "eipalloc-0123456789abcdef0" in rendered
    assert "192.0.2.99" in rendered


def test_regional_parameters_bundle_multiple_locations() -> None:
    config = DeploymentConfig.model_validate(
        {**BASE, "locations": ["denver", "oregon", "los-angeles"]}
    )
    targets = [
        ResolvedTarget(
            LOCATIONS["denver"],
            "us-west-2-den-1a",
            "local-zone",
            "opted-in",
            eip_allocation_id="eipalloc-den",
            eip_public_ip="192.0.2.1",
        ),
        ResolvedTarget(
            LOCATIONS["oregon"],
            "us-west-2a",
            "availability-zone",
            "opt-in-not-required",
            eip_allocation_id="eipalloc-pdx",
            eip_public_ip="192.0.2.2",
        ),
        ResolvedTarget(
            LOCATIONS["los-angeles"],
            "us-west-2-lax-1a",
            "local-zone",
            "opted-in",
            eip_allocation_id="eipalloc-lax",
            eip_public_ip="192.0.2.3",
        ),
    ]
    parameters = _regional_parameters(
        config,
        targets,
        "us-west-2",
        ("bucket", "assets/bootstrap.py", "https://example.invalid/location.yaml"),
        "a" * 64,
    )
    # Slots follow location key order (denver, los-angeles, oregon), not the
    # order the operator listed them in, so a slot's VPC CIDR stays with one
    # location as others are added or removed.
    values = {item["ParameterKey"]: item["ParameterValue"] for item in parameters}
    assert values["Location1Code"] == "den"
    assert values["Location2Code"] == "lax"
    assert values["Location3Code"] == "pdx"
    assert values["Location4Code"] == ""
    assert values["Location1InstanceType"] == "t3.small"
    assert values["Location4InstanceType"] == "t3.small"
    assert values["Location1EipAllocationId"] == "eipalloc-den"
    assert values["Location2EipAllocationId"] == "eipalloc-lax"
    assert values["Location3EipAllocationId"] == "eipalloc-pdx"
    assert values["Location4EipAllocationId"] == ""
    assert values["Location1EipPublicIp"] == "192.0.2.1"
    assert values["Location4EipPublicIp"] == ""


def test_regional_parameters_match_the_regional_template_exactly() -> None:
    """Every parameter sent must be declared in regional.yaml, and vice versa.

    CloudFormation rejects the whole update if either side drifts, so a
    parameter left behind in the Python after a template edit fails the deploy
    at its first call.
    """
    import re
    from pathlib import Path

    template = (Path(__file__).parents[1] / "infrastructure" / "regional.yaml").read_text()
    body = template.split("Parameters:", 1)[1].split("\nConditions:", 1)[0]
    declared = set(re.findall(r"^  ([A-Za-z0-9]+):", body, re.MULTILINE))

    config = DeploymentConfig.model_validate({**BASE, "locations": ["denver", "oregon"]})
    targets = [
        ResolvedTarget(LOCATIONS["denver"], "us-west-2-den-1a", "local-zone", "opted-in"),
        ResolvedTarget(LOCATIONS["oregon"], "us-west-2a", "availability-zone", "n/a"),
    ]
    sent = {
        item["ParameterKey"]
        for item in _regional_parameters(
            config, targets, "us-west-2", ("bucket", "key", "url"), "a" * 64
        )
    }

    assert not sent - declared, f"sent but not in template: {sorted(sent - declared)}"
    assert not declared - sent, f"declared but never sent: {sorted(declared - sent)}"


def test_deploying_one_location_leaves_another_locations_slot_untouched() -> None:
    """Adding Oregon must not disturb a running Denver.

    Denver is not in this selection, so every Location1 value -- including its
    bootstrap asset, which drives user data and therefore instance replacement
    -- must come through byte-identical from what is already deployed. Emitting
    blanks would clear Location1's condition and delete Denver's stack.
    """
    deployed = {
        "Location1Code": "den",
        "Location1Zone": "us-west-2-den-1a",
        "Location1InstanceType": "t3.medium",
        "Location1EipAllocationId": "eipalloc-den",
        "Location1EipPublicIp": "192.0.2.11",
        "Location1BootstrapKey": "assets/bootstrap-OLDHASH.py",
        "Location1BootstrapSha256": "old" + "0" * 61,
    }
    config = DeploymentConfig.model_validate({**BASE, "locations": ["oregon"]})
    targets = [
        ResolvedTarget(
            LOCATIONS["oregon"],
            "us-west-2a",
            "availability-zone",
            "opt-in-not-required",
            eip_allocation_id="eipalloc-pdx",
            eip_public_ip="192.0.2.12",
        )
    ]

    values = {
        item["ParameterKey"]: item["ParameterValue"]
        for item in _regional_parameters(
            config,
            targets,
            "us-west-2",
            ("bucket", "assets/bootstrap-NEWHASH.py", "url"),
            "new" + "0" * 61,
            deployed,
        )
    }

    for key, expected in deployed.items():
        assert values[key] == expected, f"{key} must be preserved for the running Denver stack"
    # Oregon lands in its own slot, with the current bootstrap asset.
    assert values["Location3Code"] == "pdx"
    assert values["Location3BootstrapKey"] == "assets/bootstrap-NEWHASH.py"


def test_preserved_slot_inherits_pre_migration_regional_bootstrap_asset() -> None:
    """A stack deployed before per-slot bootstrap assets must keep its own asset.

    Older regional stacks carry one BootstrapKey for every location. A slot being
    carried through has no per-slot value to read, so it has to fall back to that
    regional one -- blanking it would rewrite a running location's user data.
    """
    deployed = {
        "Location1Code": "den",
        "Location1Zone": "us-west-2-den-1a",
        "Location1InstanceType": "t3.medium",
        "Location1EipAllocationId": "eipalloc-den",
        "Location1EipPublicIp": "192.0.2.11",
        "BootstrapKey": "assets/bootstrap-DEPLOYED.py",
        "BootstrapSha256": "dep" + "0" * 61,
    }
    config = DeploymentConfig.model_validate({**BASE, "locations": ["oregon"]})
    targets = [
        ResolvedTarget(
            LOCATIONS["oregon"], "us-west-2a", "availability-zone", "opt-in-not-required"
        )
    ]

    values = {
        item["ParameterKey"]: item["ParameterValue"]
        for item in _regional_parameters(
            config,
            targets,
            "us-west-2",
            ("b", "assets/bootstrap-NEW.py", "u"),
            "new" + "0" * 61,
            deployed,
        )
    }

    assert values["Location1BootstrapKey"] == "assets/bootstrap-DEPLOYED.py"
    assert values["Location1BootstrapSha256"] == "dep" + "0" * 61
    assert values["Location1Code"] == "den"
    assert values["Location3BootstrapKey"] == "assets/bootstrap-NEW.py"


def test_regional_slot_is_fixed_per_location_regardless_of_selection() -> None:
    """Deploying a different subset must not hand one location's stack to another.

    Each slot owns a fixed VPC CIDR and nested stack, so Denver must stay in its
    own slot whether or not it is part of the current selection -- otherwise
    Oregon inherits Denver's live stack and its subnet replacement collides with
    the CIDR Denver still holds.
    """

    def code_by_slot(selection: list[str]) -> dict[str, str]:
        config = DeploymentConfig.model_validate({**BASE, "locations": selection})
        targets = [ResolvedTarget(LOCATIONS[key], "zone", "type", "opted-in") for key in selection]
        values = {
            item["ParameterKey"]: item["ParameterValue"]
            for item in _regional_parameters(
                config, targets, "us-west-2", ("bucket", "key", "url"), "a" * 64
            )
        }
        return {f"Location{n}Code": values[f"Location{n}Code"] for n in range(1, 5)}

    denver_only = code_by_slot(["denver"])
    oregon_only = code_by_slot(["oregon"])
    both = code_by_slot(["denver", "oregon"])

    # Denver holds slot 1 in every selection; Oregon never takes it.
    assert denver_only["Location1Code"] == "den"
    assert oregon_only["Location1Code"] == ""
    assert both["Location1Code"] == "den"
    # Oregon keeps one slot of its own across selections.
    oregon_slot = next(k for k, v in oregon_only.items() if v == "pdx")
    assert both[oregon_slot] == "pdx"


def test_regional_slots_do_not_depend_on_configured_location_order() -> None:
    """A location must keep its slot -- and so its VPC CIDR -- however it is listed."""

    def slots(order: list[str]) -> dict[str, str]:
        config = DeploymentConfig.model_validate({**BASE, "locations": order})
        targets = [ResolvedTarget(LOCATIONS[key], "zone", "type", "opted-in") for key in order]
        values = {
            item["ParameterKey"]: item["ParameterValue"]
            for item in _regional_parameters(
                config, targets, "us-west-2", ("bucket", "key", "url"), "a" * 64
            )
        }
        return {
            values[f"Location{n}Code"]: f"slot{n}"
            for n in range(1, 5)
            if values[f"Location{n}Code"]
        }

    assert slots(["denver", "oregon"]) == slots(["oregon", "denver"])
    assert slots(["denver", "oregon"])["den"] == "slot1"


def test_stack_failure_reason_prefers_failed_leaf_resource() -> None:
    cfn = type(
        "CloudFormation",
        (),
        {
            "describe_stack_events": lambda self, StackName: {
                "StackEvents": [
                    {
                        "LogicalResourceId": "test",
                        "ResourceType": "AWS::CloudFormation::Stack",
                        "ResourceStatus": "CREATE_FAILED",
                        "ResourceStatusReason": "resources failed to create",
                    },
                    {
                        "LogicalResourceId": "OptInRole",
                        "ResourceType": "AWS::IAM::Role",
                        "ResourceStatus": "CREATE_FAILED",
                        "ResourceStatusReason": "not authorized to create a role",
                    },
                ]
            }
        },
    )()

    assert AwsFacade._stack_failure_reason(cfn, "test") == (
        "OptInRole: not authorized to create a role"
    )


def test_stack_parameters_use_resolved_fallback_type() -> None:
    config = DeploymentConfig.model_validate({**BASE, "locations": ["dallas"]})
    target = ResolvedTarget(
        LOCATIONS["dallas"],
        "us-east-1-dfw-2a",
        "local-zone",
        "opted-in",
        "t3.medium",
    )

    values = {
        item["ParameterKey"]: item["ParameterValue"] for item in _stack_parameters(config, target)
    }

    assert values["InstanceType"] == "t3.medium"


def test_resolve_eips_reuses_existing_tagged_address() -> None:
    config = DeploymentConfig.model_validate(BASE)
    target = ResolvedTarget(LOCATIONS["denver"], "us-west-2-den-1a", "local-zone", "opted-in")
    ec2 = Mock()
    ec2.describe_addresses.return_value = {
        "Addresses": [
            {
                "AllocationId": "eipalloc-existing",
                "PublicIp": "192.0.2.50",
                "NetworkBorderGroup": "us-west-2-den-1",
            }
        ]
    }
    facade = AwsFacade.__new__(AwsFacade)
    facade.session = Mock(client=Mock(return_value=ec2))
    facade.reporter = lambda _message: None

    resolved = facade.resolve_eips(config, [target])

    assert resolved[0].eip_allocation_id == "eipalloc-existing"
    assert resolved[0].eip_public_ip == "192.0.2.50"
    ec2.allocate_address.assert_not_called()
    ec2.describe_addresses.assert_called_once_with(
        Filters=[{"Name": "tag:Name", "Values": ["den-aws-te"]}]
    )


def test_resolve_eips_rejects_wrong_network_border_group() -> None:
    config = DeploymentConfig.model_validate(BASE)
    target = ResolvedTarget(LOCATIONS["denver"], "us-west-2-den-1a", "local-zone", "opted-in")
    ec2 = Mock()
    ec2.describe_addresses.return_value = {
        "Addresses": [
            {
                "AllocationId": "eipalloc-existing",
                "PublicIp": "192.0.2.50",
                "NetworkBorderGroup": "us-west-2",
            }
        ]
    }
    facade = AwsFacade.__new__(AwsFacade)
    facade.session = Mock(client=Mock(return_value=ec2))
    facade.reporter = lambda _message: None

    with pytest.raises(PreflightError, match="network border group"):
        facade.resolve_eips(config, [target])


def test_resolve_eips_allocates_and_tags_when_missing() -> None:
    config = DeploymentConfig.model_validate(BASE)
    target = ResolvedTarget(LOCATIONS["denver"], "us-west-2-den-1a", "local-zone", "opted-in")
    ec2 = Mock()
    ec2.describe_addresses.return_value = {"Addresses": []}
    ec2.allocate_address.return_value = {
        "AllocationId": "eipalloc-new",
        "PublicIp": "192.0.2.60",
    }
    facade = AwsFacade.__new__(AwsFacade)
    facade.session = Mock(client=Mock(return_value=ec2))
    facade.reporter = lambda _message: None

    resolved = facade.resolve_eips(config, [target])

    assert resolved[0].eip_allocation_id == "eipalloc-new"
    assert resolved[0].eip_public_ip == "192.0.2.60"
    ec2.allocate_address.assert_called_once_with(Domain="vpc", NetworkBorderGroup="us-west-2-den-1")
    ec2.create_tags.assert_called_once_with(
        Resources=["eipalloc-new"],
        Tags=[
            {"Key": "Name", "Value": "den-aws-te"},
            {"Key": "Project", "Value": "cisco-secure-access-te-monitor"},
        ],
    )


def test_resolve_eips_allocates_in_parent_region_for_standard_region_location() -> None:
    config = DeploymentConfig.model_validate({**BASE, "locations": ["oregon"]})
    target = ResolvedTarget(
        LOCATIONS["oregon"], "us-west-2a", "availability-zone", "opt-in-not-required"
    )
    ec2 = Mock()
    ec2.describe_addresses.return_value = {"Addresses": []}
    ec2.allocate_address.return_value = {
        "AllocationId": "eipalloc-new",
        "PublicIp": "192.0.2.70",
    }
    facade = AwsFacade.__new__(AwsFacade)
    facade.session = Mock(client=Mock(return_value=ec2))
    facade.reporter = lambda _message: None

    facade.resolve_eips(config, [target])

    ec2.allocate_address.assert_called_once_with(Domain="vpc", NetworkBorderGroup="us-west-2")


def test_resolve_eips_dry_run_skips_allocation_when_missing() -> None:
    config = DeploymentConfig.model_validate(BASE)
    target = ResolvedTarget(LOCATIONS["denver"], "us-west-2-den-1a", "local-zone", "opted-in")
    ec2 = Mock()
    ec2.describe_addresses.return_value = {"Addresses": []}
    facade = AwsFacade.__new__(AwsFacade)
    facade.session = Mock(client=Mock(return_value=ec2))
    facade.reporter = lambda _message: None

    resolved = facade.resolve_eips(config, [target], dry_run=True)

    assert resolved[0].eip_allocation_id is None
    assert resolved[0].eip_public_ip is None
    ec2.allocate_address.assert_not_called()


def test_wait_stackset_idle_blocks_until_operations_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfn = Mock()
    cfn.list_stack_set_operations.side_effect = [
        {"Summaries": [{"OperationId": "op-1", "Status": "RUNNING"}]},
        {"Summaries": [{"OperationId": "op-1", "Status": "STOPPING"}]},
        {"Summaries": [{"OperationId": "op-1", "Status": "SUCCEEDED"}]},
    ]
    facade = AwsFacade.__new__(AwsFacade)
    facade.reporter = lambda _message: None
    monkeypatch.setattr("swg_te_monitor.aws.time.sleep", lambda _seconds: None)

    facade._wait_stackset_idle(cfn, "test-stackset")

    assert cfn.list_stack_set_operations.call_count == 3


def test_wait_stackset_idle_raises_when_never_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    cfn = Mock()
    cfn.list_stack_set_operations.return_value = {
        "Summaries": [{"OperationId": "op-1", "Status": "RUNNING"}]
    }
    facade = AwsFacade.__new__(AwsFacade)
    facade.reporter = lambda _message: None
    monkeypatch.setattr("swg_te_monitor.aws.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="still had an operation in flight"):
        facade._wait_stackset_idle(cfn, "test-stackset", attempts=3)


def test_instance_selection_uses_affordable_offering() -> None:
    assert _select_instance_type("t3.small", {"t3.medium", "t2.medium"}) == "t3.medium"


def test_instance_selection_reports_when_the_zone_offers_no_preferred_type() -> None:
    """No preferred type must not become a guess.

    Returning a hard-coded type here launched an instance type the zone does
    not offer, which EC2 rejects only at create time with "The requested
    configuration is currently not supported".
    """
    assert _select_instance_type("t3.small", {"c6i.large", "m6i.large"}) is None


def test_smallest_supported_type_skips_arm_and_undersized_offerings() -> None:
    """Pick by what actually boots: the image is x86_64, and BrowserBot needs room.

    The Dallas Local Zone's smallest offering by memory is Graviton, so size
    alone would choose a type the pinned image cannot run.
    """
    ec2 = Mock()
    ec2.describe_instance_types.return_value = {
        "InstanceTypes": [
            {
                "InstanceType": "c6gn.medium",
                "ProcessorInfo": {"SupportedArchitectures": ["arm64"]},
                "VCpuInfo": {"DefaultVCpus": 1},
                "MemoryInfo": {"SizeInMiB": 2048},
            },
            {
                "InstanceType": "c6i.large",
                "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                "VCpuInfo": {"DefaultVCpus": 2},
                "MemoryInfo": {"SizeInMiB": 4096},
            },
            {
                "InstanceType": "m6i.large",
                "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                "VCpuInfo": {"DefaultVCpus": 2},
                "MemoryInfo": {"SizeInMiB": 8192},
            },
        ]
    }

    assert _smallest_supported_type(ec2, {"c6gn.medium", "c6i.large", "m6i.large"}, 4.0) == (
        "c6i.large",
        4.0,
    )


def test_smallest_supported_type_returns_none_when_nothing_qualifies() -> None:
    ec2 = Mock()
    ec2.describe_instance_types.return_value = {
        "InstanceTypes": [
            {
                "InstanceType": "c6gn.medium",
                "ProcessorInfo": {"SupportedArchitectures": ["arm64"]},
                "VCpuInfo": {"DefaultVCpus": 1},
                "MemoryInfo": {"SizeInMiB": 2048},
            }
        ]
    }

    assert _smallest_supported_type(ec2, {"c6gn.medium"}, 4.0) is None


def test_missing_execution_role_is_created_without_replacing_admin_role() -> None:
    iam = Mock()
    waiter = Mock()
    iam.get_waiter.return_value = waiter

    AwsFacade._ensure_stackset_roles(
        iam,
        "123456789012",
        "aws",
        {"AWSCloudFormationStackSetAdministrationRole"},
    )

    iam.create_role.assert_called_once()
    assert iam.create_role.call_args.kwargs["RoleName"] == (
        "AWSCloudFormationStackSetExecutionRole"
    )
    iam.attach_role_policy.assert_called_once_with(
        RoleName="AWSCloudFormationStackSetExecutionRole",
        PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess",
    )
    assert iam.update_assume_role_policy.call_count == 2
    assert iam.put_role_policy.call_count == 2
    assert waiter.wait.call_count == 2


def test_stackset_operation_failure_includes_target_reason() -> None:
    cfn = Mock()
    cfn.describe_stack_set_operation.return_value = {
        "StackSetOperation": {
            "Status": "FAILED",
            "StatusReason": "one or more stack instances failed",
        }
    }
    cfn.list_stack_set_operation_results.return_value = {
        "Summaries": [
            {
                "Account": "123456789012",
                "Region": "us-west-2",
                "StatusReason": "execution role cannot be assumed",
            }
        ]
    }

    try:
        AwsFacade._wait_stackset_operation(cfn, "test", "operation-1")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected failed StackSet operation")

    assert "123456789012/us-west-2" in message
    assert "execution role cannot be assumed" in message


def _facade_with_stacksets(owned: dict[str, list[str]]) -> AwsFacade:
    """Build a facade where owned maps a StackSet's Region to the Regions it manages."""
    facade = AwsFacade.__new__(AwsFacade)

    def client(_service: str, region_name: str) -> Mock:
        stub = Mock()
        if region_name not in owned:
            stub.describe_stack_set.side_effect = ClientError(
                {"Error": {"Code": "StackSetNotFoundException", "Message": "StackSet not found"}},
                "DescribeStackSet",
            )
        else:
            stub.list_stack_instances.return_value = {
                "Summaries": [{"Region": r} for r in owned[region_name]]
            }
        return stub

    facade.session = Mock(client=Mock(side_effect=client))
    facade.reporter = lambda _message: None
    return facade


def test_home_region_picks_the_stackset_that_owns_the_target_region() -> None:
    """With two StackSets, deploy through whichever already manages the target.

    Choosing the other one creates a second stack instance in a Region that is
    already managed, duplicating VPCs and instances over running agents.
    """
    facade = _facade_with_stacksets({"us-east-2": ["us-east-2"], "us-west-2": ["us-west-2"]})

    assert facade.home_region(("us-west-2",)) == "us-west-2"
    assert facade.home_region(("us-east-2",)) == "us-east-2"


def test_home_region_refuses_a_deployment_spanning_two_stacksets() -> None:
    facade = _facade_with_stacksets({"us-east-2": ["us-east-2"], "us-west-2": ["us-west-2"]})

    with pytest.raises(PreflightError, match="spans StackSets"):
        facade.home_region(("us-east-2", "us-west-2"))


def test_home_region_uses_the_only_stackset_for_an_unmanaged_region() -> None:
    facade = _facade_with_stacksets({"us-west-2": ["us-west-2"]})

    assert facade.home_region(("us-east-1",)) == "us-west-2"


def test_home_region_falls_back_to_a_fixed_default_when_none_exists() -> None:
    assert _facade_with_stacksets({}).home_region(("us-east-2",)) == "us-east-1"


def test_deployed_values_ignores_a_stack_that_is_not_standing() -> None:
    """A failed or deleted stack's parameters must not be carried forward.

    CloudFormation still answers for a deleted stack. Its parameters describe
    an attempt that did not work, so carrying them forward re-creates a
    location that never deployed with the settings that failed it -- and one
    failed slot rolls back the whole Region, taking the locations being
    deployed alongside it.
    """
    for status in ("DELETE_COMPLETE", "ROLLBACK_COMPLETE", "CREATE_FAILED"):
        cfn = Mock()
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackStatus": status,
                    "Parameters": [{"ParameterKey": "Location1Code", "ParameterValue": "dfw"}],
                }
            ]
        }
        assert _deployed_regional_values(cfn, "stack-id") == {}, status


def test_deployed_values_are_read_from_a_standing_stack() -> None:
    cfn = Mock()
    cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackStatus": "CREATE_COMPLETE",
                "Parameters": [{"ParameterKey": "Location1Code", "ParameterValue": "den"}],
            }
        ]
    }
    assert _deployed_regional_values(cfn, "stack-id") == {"Location1Code": "den"}


def test_a_stack_in_cleanup_is_still_standing() -> None:
    """Regression: a cleanup status is a live stack, not a missing one.

    An update that has reached its new state and is only discarding replaced
    resources reports *_CLEANUP_IN_PROGRESS. Reading that as "nothing
    deployed" returns no values to carry forward, which blanks every slot the
    deployment did not name and deletes the locations running in them. This
    terminated a running production agent once already.
    """
    for status in (
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
    ):
        cfn = Mock()
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackStatus": status,
                    "Parameters": [{"ParameterKey": "Location3Code", "ParameterValue": "iad"}],
                }
            ]
        }
        assert _deployed_regional_values(cfn, "stack-id") == {"Location3Code": "iad"}, status


def test_an_in_progress_stack_is_waited_out_rather_than_blanked() -> None:
    """A stack mid-move is not readable, so settle first and then read it."""
    cfn = Mock()
    cfn.describe_stacks.side_effect = [
        {"Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS", "Parameters": []}]},
        {
            "Stacks": [
                {
                    "StackStatus": "UPDATE_COMPLETE",
                    "Parameters": [{"ParameterKey": "Location3Code", "ParameterValue": "iad"}],
                }
            ]
        },
    ]
    assert _deployed_regional_values(cfn, "stack-id", delay=0) == {"Location3Code": "iad"}


def test_an_unreadable_stack_refuses_instead_of_returning_nothing() -> None:
    """Failing closed costs a re-run; returning {} costs a running agent."""
    cfn = Mock()
    cfn.describe_stacks.return_value = {
        "Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS", "Parameters": []}]
    }
    with pytest.raises(RuntimeError, match="still in progress"):
        _deployed_regional_values(cfn, "stack-id", attempts=3, delay=0)


def test_an_unrecognised_status_refuses_rather_than_guessing() -> None:
    cfn = Mock()
    cfn.describe_stacks.return_value = {
        "Stacks": [{"StackStatus": "REVIEW_IN_PROGRESS_SOMETHING_NEW", "Parameters": []}]
    }
    with pytest.raises(RuntimeError, match="not a state this can safely carry"):
        _deployed_regional_values(cfn, "stack-id", attempts=1, delay=0)
