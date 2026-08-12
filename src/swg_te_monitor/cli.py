"""Command-line entry point."""

import json
from pathlib import Path

import typer
import yaml
from botocore.exceptions import BotoCoreError, ClientError
from rich.console import Console
from rich.table import Table

from .aws import AwsFacade, PreflightError, ResolvedTarget
from .generator import write_templates
from .models import DeploymentConfig, load_config
from .ui import CONFIGURE_AWS, aws_profile, deployment_mode, interactive_config

app = typer.Typer(no_args_is_help=True, help="Deploy Cisco Secure Access ThousandEyes monitors.")
console = Console()
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infrastructure" / "location.yaml"
LOCAL_ZONE_TEMPLATE = ROOT / "infrastructure" / "local-zone-opt-in.yaml"
BOOTSTRAP = ROOT / "bootstrap" / "bootstrap.py"


def _config(path: Path) -> DeploymentConfig:
    try:
        return load_config(path)
    except Exception as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc


@app.command()
def configure(
    output: Path = typer.Option(Path("config.local.yaml")),
    template_output: Path = typer.Option(Path("generated/cloudformation.yaml")),
) -> None:
    """Collect configuration, then generate a template or configure AWS."""
    mode = deployment_mode()
    profile = aws_profile() if mode == CONFIGURE_AWS else None
    config = interactive_config(profile).model_copy(update={"deployment_method": mode})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(config.deployment_dict(), sort_keys=True), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(output)
    console.print(f"Saved private deployment configuration to {output}")
    if mode != CONFIGURE_AWS:
        paths = write_templates(config, TEMPLATE, BOOTSTRAP, template_output)
        for path in paths:
            console.print(f"Generated standalone CloudFormation template: {path}")
        console.print("No AWS API calls were made.")
        return
    # Deliberately does not deploy. Deploying from here would run a second time
    # when the operator follows up with the deploy command, starting a rollout
    # while the first is still in flight.
    console.print("No AWS changes were made. To deploy, review the configuration and run:")
    console.print(f"  swg-te-monitor deploy --config {output} --dry-run")
    console.print(f"  swg-te-monitor deploy --config {output}")


@app.command()
def validate(config: Path = typer.Option(..., exists=True, readable=True)) -> None:
    """Run account-aware preflight without deploying."""
    cfg = _config(config)
    try:
        facade = AwsFacade(cfg.aws_profile, reporter=lambda message: console.print(f"• {message}"))
        targets = facade.preflight(cfg)
    except PreflightError as exc:
        console.print(f"[red]Preflight failed:[/red] {exc}")
        raise typer.Exit(2) from exc
    table = Table("Location", "Region", "Zone", "Type", "Opt-in", "Instance")
    for target in targets:
        instance = target.instance_type
        if target.substituted_instance_type:
            instance = f"[yellow]{instance} (substituted)[/yellow]"
        table.add_row(
            target.location.label,
            target.location.region,
            target.zone_name,
            target.zone_type,
            target.opt_in_status,
            instance,
        )
    console.print(table)
    # Reports only: validate changes nothing, so it never prompts.
    if any(target.substituted_instance_type for target in targets):
        console.print(
            "[yellow]A substituted type is outside the burstable range and costs more per hour; "
            "deploy will ask before using it.[/yellow]"
        )


@app.command()
def deploy(
    config: Path = typer.Option(..., exists=True, readable=True),
    dry_run: bool = typer.Option(False, help="Preflight only; do not change AWS."),
    yes: bool = typer.Option(
        False,
        "--yes",
        help=(
            "Skip the confirmations: that each EIP is registered in Cisco Secure Access, "
            "and that a costlier substituted instance type is acceptable."
        ),
    ),
) -> None:
    """Preflight and deploy every selected location through one StackSet rollout."""
    cfg = _config(config)
    if cfg.deployment_method != CONFIGURE_AWS:
        console.print(
            "[yellow]AWS deployment was not started.[/yellow] This configuration was created "
            "for manual CloudFormation deployment."
        )
        raise typer.Exit(2)
    _deploy_config(cfg, dry_run, assume_yes=yes)


def _confirm_registered_networks() -> None:
    """Pause so each Elastic IP can be registered before any instance boots.

    Registration in Cisco Secure Access is manual, and an instance that boots
    before its address is registered fails its proxy check and takes the whole
    stack down with it. Waiting here turns that race into a deliberate step.
    """
    message = (
        "Confirm that each EIP is provisioned as a Registered Network in Cisco Secure Access. "
        "Press Return to continue when this has been manually configured."
    )
    try:
        input(f"\n{message} ")
    except EOFError:
        # No console attached (piped or scheduled run): proceed rather than
        # block forever, but say so, because the race is now live again.
        console.print(
            f"[yellow]{message}[/yellow]\n"
            "[yellow]No interactive console; continuing without confirmation. "
            "Pass --yes to make this explicit.[/yellow]"
        )


def _confirm_substituted_instance_types(substituted: list[ResolvedTarget]) -> bool:
    """Ask before deploying a costlier instance type than the operator asked for.

    Some zones offer no burstable type at all, so the only way to deploy there
    is a larger family that bills at a different rate. That is a spending
    decision, so it defaults to no and is only raised for the locations
    actually affected.
    """
    console.print()
    for target in substituted:
        memory = f", {target.instance_memory_gib:g} GiB" if target.instance_memory_gib else ""
        console.print(
            f"[yellow]{target.location.label}: {target.zone_name} offers none of "
            f"t3.small, t3.medium or t2.medium. The smallest suitable type it does offer is "
            f"{target.instance_type}{memory}.[/yellow]"
        )
    console.print(
        "[yellow]That is outside the burstable range and bills at a higher hourly rate, and "
        "Local Zones cost more than their parent Region. Check current pricing before "
        "continuing.[/yellow]"
    )
    names = ", ".join(sorted({target.instance_type for target in substituted}))
    try:
        answer = input(f"Proceed with {names}? [y/N] ").strip().lower()
    except EOFError:
        # Defaults to no, so a run with no console must not silently spend more.
        console.print(
            "[red]No interactive console to confirm the instance type; stopping.[/red] "
            "Re-run with --yes to accept it non-interactively."
        )
        return False
    return answer in {"y", "yes"}


def _deploy_config(cfg: DeploymentConfig, dry_run: bool, assume_yes: bool = False) -> None:
    profile_label = cfg.aws_profile or "the default AWS credential chain"
    console.print(
        f"Connecting with the AWS Python SDK using {profile_label}. "
        "AWS CLI commands are not used for deployment."
    )
    aws = AwsFacade(cfg.aws_profile, reporter=lambda message: console.print(f"• {message}"))
    try:
        console.print("• Confirming the signed-in AWS account and role...")
        identity = aws.identity()
        console.print(f"• Signed in to AWS account {identity['Account']} as {identity['Arn']}.")
        if not dry_run:
            console.print(
                "• Ensuring selected AWS Local Zones are enabled (this may take several minutes)..."
            )
            aws.enable_local_zones(cfg, LOCAL_ZONE_TEMPLATE)
        console.print("• Checking that AWS can host each selected monitoring instance...")
        targets = aws.preflight(cfg)
    except (BotoCoreError, ClientError, PreflightError, RuntimeError) as exc:
        console.print(f"[red]Deployment preflight failed:[/red] {_friendly_aws_error(exc, cfg)}")
        raise typer.Exit(2) from exc
    substituted = [target for target in targets if target.substituted_instance_type]
    if substituted and not dry_run and not assume_yes:
        # Ask before any Elastic IP is allocated: declining afterwards would
        # strand an address, and nothing here ever releases one.
        if not _confirm_substituted_instance_types(substituted):
            console.print("Deployment cancelled; no AWS changes were made.")
            raise typer.Exit(1)
    try:
        console.print("• Resolving Elastic IPs for each selected location...")
        targets = aws.resolve_eips(cfg, targets, dry_run=dry_run)
    except (BotoCoreError, ClientError, PreflightError, RuntimeError) as exc:
        console.print(f"[red]Deployment preflight failed:[/red] {_friendly_aws_error(exc, cfg)}")
        raise typer.Exit(2) from exc
    for target in targets:
        if target.eip_public_ip:
            console.print(
                f"• {target.location.label}: Elastic IP {target.eip_public_ip} "
                f"({target.eip_allocation_id})"
            )
        else:
            console.print(f"• {target.location.label}: Elastic IP would be allocated (dry run)")
    if not dry_run and not assume_yes:
        _confirm_registered_networks()
    if dry_run:
        console.print(f"Validated {len(targets)} deployment target(s); no changes made.")
        return
    try:
        console.print("• Starting the CloudFormation StackSet deployment...")
        aws.deploy(cfg, targets, TEMPLATE)
    except (BotoCoreError, ClientError, RuntimeError) as exc:
        console.print(f"[red]Deployment failed:[/red] {exc}")
        raise typer.Exit(2) from exc
    console.print(f"Submitted {len(targets)} stack deployment(s).")


def _friendly_aws_error(exc: Exception, cfg: DeploymentConfig) -> str:
    text = str(exc)
    if "SSO" in text or "token has expired" in text.lower():
        profile = cfg.aws_profile or "<your-sso-profile>"
        return f"Your AWS SSO session is not active. Run: aws sso login --profile {profile}"
    return text


@app.command()
def status(config: Path = typer.Option(..., exists=True, readable=True)) -> None:
    """Display non-sensitive StackSet instance status."""
    cfg = _config(config)
    aws = AwsFacade(cfg.aws_profile)
    from .locations import LOCATIONS

    admin_region = aws.home_region(tuple(sorted({LOCATIONS[k].region for k in cfg.locations})))
    cfn = aws.session.client("cloudformation", region_name=admin_region)
    name = "cisco-secure-access-te-monitor-locations"
    for summary in cfn.list_stack_instances(StackSetName=name).get("Summaries", []):
        console.print(
            json.dumps(
                {
                    "stack_set": name,
                    "account": summary.get("Account"),
                    "region": summary.get("Region"),
                    "status": summary.get("Status"),
                    "detailed_status": summary.get("StackInstanceStatus", {}).get("DetailedStatus"),
                }
            )
        )


@app.command()
def remove(
    config: Path = typer.Option(..., exists=True, readable=True),
    yes: bool = typer.Option(False, "--yes", help="Confirm destructive stack deletion."),
) -> None:
    """Delete every StackSet instance and the StackSet itself.

    Removal is all or nothing. A stack instance covers a whole Region, so
    deleting one takes every location deployed in that Region with it; there is
    no way to remove a single location this way. Elastic IPs are left
    allocated, since they stay registered in Cisco Secure Access.
    """
    if not yes:
        console.print("Refusing removal without --yes.")
        raise typer.Exit(2)
    cfg = _config(config)
    aws = AwsFacade(cfg.aws_profile)
    from .locations import LOCATIONS

    admin_region = aws.home_region(tuple(sorted({LOCATIONS[k].region for k in cfg.locations})))
    account = aws.identity()["Account"]
    cfn = aws.session.client("cloudformation", region_name=admin_region)
    name = "cisco-secure-access-te-monitor-locations"
    # Drive this from what is actually deployed, not from the configuration:
    # deleting the StackSet fails while any instance it does not know about
    # remains, and the configuration only lists the most recent selection.
    deployed = cfn.list_stack_instances(StackSetName=name).get("Summaries", [])
    regions = sorted({summary["Region"] for summary in deployed})
    console.print(f"Deleting every location in: {', '.join(regions) or 'none'}")
    for region in regions:
        operation = cfn.delete_stack_instances(
            StackSetName=name,
            Accounts=[account],
            Regions=[region],
            RetainStacks=False,
            OperationPreferences={"FailureToleranceCount": 0, "MaxConcurrentCount": 1},
        )
        aws._wait_stackset_operation(cfn, name, operation["OperationId"])
    cfn.delete_stack_set(StackSetName=name)
    console.print(f"Deleted StackSet and stack instances: {name}")


if __name__ == "__main__":
    app()
