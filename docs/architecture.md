# Architecture decisions

## Deployment unit

One self-managed StackSet spans the selected parent Regions. A StackSet can create only one stack instance per account/Region, while multiple requested locations share `us-east-1` and `us-west-2`. Therefore each stack instance uses `regional.yaml` as a bundle and creates one nested `location.yaml` stack per selected city. Each nested stack owns its VPC, subnet, EC2 host, role/profile, and security group.

The CLI creates the conventional same-account StackSet administration and execution roles, uploads private nested templates to each Region, creates or updates the StackSet, applies per-Region parameter overrides, and waits for each operation. Self-managed permissions work in a standalone account without AWS Organizations. The execution role is intentionally privileged because it provisions IAM, EC2, VPC, and nested CloudFormation resources; production environments should apply an organization-approved permissions boundary if required.

Selected Local Zone groups are enabled before preflight by `local-zone-opt-in.yaml`. Its inline Lambda calls `ModifyAvailabilityZoneGroup`, polls `DescribeAvailabilityZones`, and blocks the rollout until a zone becomes available. Deletion retains opt-in because AWS does not provide a normal self-service opt-out path. The Lambda remains for safe updates but has no idle compute charge.

## Bootstrap mechanism

First boot uses EC2 user data only as a small trust bootstrap: it writes non-secret references, downloads a private content-addressed Python asset from S3, verifies SHA-256, and executes it. The script retrieves secrets using the instance role and signals CloudFormation. This is simpler than Image Builder for the initial release, contains no customer data in an AMI, and remains independently testable. A future golden AMI may cache public packages and the verified bootstrap asset, but must never contain secret values or registered agent state.

## Network model

Trust is established by source IP. A dedicated Elastic IP per location — resolved or allocated by the CLI and tag-matched by agent hostname — is the identity an operator manually registers as a Registered Network in Cisco Secure Access, which is what authorizes the instance to reach the PAC file and proxy. `SourceDestCheck` remains enabled because this release monitors from the host and does not forward other subnets.

The subnet auto-assigns a public IPv4 (`MapPublicIpOnLaunch: true`) and user data associates the Elastic IP over it during first boot, then blocks until IMDS reports the expected address before doing anything else. The auto-assigned address exists only to give the host enough connectivity to call `ec2:AssociateAddress`, and nothing that matters to Cisco Secure Access egresses before the swap completes.

Both halves of that arrangement are load-bearing, and each has a deadlock waiting behind it:

- **Do not move the association into an `AWS::EC2::EIPAssociation` resource.** It cannot complete until the instance reports `CREATE_COMPLETE` through `cfn-signal`, and the instance cannot reach the proxy to report success until it holds the address.
- **Do not disable the auto-assigned address.** An Internet Gateway routes nothing for an instance that has no public address, so the call to `ec2:AssociateAddress` would never leave the host.

Attaching the Elastic IP to a standalone ENI created ahead of the instance would satisfy both constraints without an auto-assigned address, at the cost of another resource. That remains the option to reach for if the auto-assigned address ever becomes unacceptable.

## Availability and failover

Region deployments select an available standard AZ. Local Zone deployments discover the expected zone group and require an opted-in, available zone. CloudFormation cannot opt an account into a Local Zone. This release intentionally configures one Elastic IP identity per location; no secondary/failover EIP or multi-homed registration is modeled.

## Agent identity and storage

The deterministic hostname is `<city-code>-aws-te[-suffix]`. ThousandEyes documentation links appliance names and Linux hostnames, but a native AL2023 display-name integration test remains required. The agent lives on a disposable encrypted 20 GiB root volume. No unsupported identity migration via detached EBS is claimed.

## Secret flow

Interactive secrets go from hidden terminal input to a mode `0600`, Git-ignored local YAML file and CloudFormation `NoEcho` parameters. EC2 stores bootstrap values in a root-owned mode `0600` file. PAC content is bounded and inspected in memory. The installation token necessarily appears briefly in the local installer process arguments because that is the vendor-supported CLI; no command output or argument is logged.

## Elastic IP lifecycle

`resolve_eips` runs after preflight and before any StackSet changes, scoped only to the regions of the locations actually selected for this deploy. For each location it tag-matches an existing Elastic IP by the location's deterministic agent hostname (e.g. `Name=den-aws-te`); if none exists it allocates and tags a new one. `--dry-run` skips the real allocation call and reports what would happen instead. No code path in this project releases an Elastic IP, including `remove`, because release risks silently breaking a location's Cisco Secure Access trust and losing the address to reassignment.
