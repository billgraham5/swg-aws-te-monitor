from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

from swg_te_monitor import cli
from swg_te_monitor.models import DeploymentConfig

_CONFIG_PATH: list[str] = []


def _written_config() -> str:
    """Write a deploy-mode config once and reuse its path."""
    import tempfile

    if not _CONFIG_PATH:
        import yaml

        d = tempfile.mkdtemp()
        p = Path(d) / "config.local.yaml"
        cfg = DeploymentConfig.model_validate({**BASE, "locations": ["dallas"]})
        p.write_text(yaml.safe_dump(cfg.deployment_dict(), sort_keys=True))
        _CONFIG_PATH.append(str(p))
    return _CONFIG_PATH[0]


BASE = {
    "locations": ["denver"],
    "thousandeyes_token": "unit-test-token",
    "pac_url": "https://proxy.example.invalid/proxy.pac",
}


def test_configure_saves_the_file_without_deploying(tmp_path: Path, monkeypatch) -> None:
    """configure must not deploy.

    Deploying here runs the whole rollout a second time when the operator
    follows up with the documented deploy command, starting a second
    CloudFormation operation while the first is still in flight.
    """
    deploy = Mock()
    monkeypatch.setattr(cli, "_deploy_config", deploy)
    monkeypatch.setattr(cli, "deployment_mode", lambda: cli.CONFIGURE_AWS)
    monkeypatch.setattr(cli, "aws_profile", lambda: None)
    monkeypatch.setattr(
        cli, "interactive_config", lambda _profile: DeploymentConfig.model_validate(BASE)
    )
    output = tmp_path / "config.local.yaml"

    result = CliRunner().invoke(cli.app, ["configure", "--output", str(output)])

    assert result.exit_code == 0, result.output
    deploy.assert_not_called()
    assert output.exists()
    assert "No AWS changes were made" in result.output
    assert "swg-te-monitor deploy --config" in result.output


def _target(substituted: bool, instance_type: str = "c6i.large"):
    from swg_te_monitor.aws import ResolvedTarget
    from swg_te_monitor.locations import LOCATIONS

    return ResolvedTarget(
        LOCATIONS["dallas"],
        "us-east-1-dfw-2a",
        "local-zone",
        "opted-in",
        instance_type,
        substituted_instance_type=substituted,
        instance_memory_gib=4.0 if substituted else None,
    )


def _facade(targets) -> Mock:
    aws = Mock()
    aws.identity.return_value = {"Account": "1", "Arn": "arn:aws:iam::1:user/x"}
    aws.preflight.return_value = targets
    aws.resolve_eips.return_value = targets
    return aws


def test_declining_a_substituted_instance_type_allocates_no_elastic_ip(monkeypatch) -> None:
    """Declining must stop before resolve_eips.

    Elastic IPs are never released by this project, so allocating one and then
    cancelling would strand a billable address nobody asked for.
    """
    aws = _facade([_target(True)])
    monkeypatch.setattr(cli, "AwsFacade", Mock(return_value=aws))
    monkeypatch.setattr("builtins.input", lambda *_a: "")  # Return alone means no

    result = CliRunner().invoke(cli.app, ["deploy", "--config", _written_config()])

    assert result.exit_code == 1
    aws.resolve_eips.assert_not_called()
    aws.deploy.assert_not_called()
    assert "Deployment cancelled" in result.output


def test_accepting_a_substituted_instance_type_continues(monkeypatch) -> None:
    aws = _facade([_target(True)])
    monkeypatch.setattr(cli, "AwsFacade", Mock(return_value=aws))
    monkeypatch.setattr(cli, "_confirm_registered_networks", Mock())
    monkeypatch.setattr("builtins.input", lambda *_a: "y")

    result = CliRunner().invoke(cli.app, ["deploy", "--config", _written_config()])

    assert result.exit_code == 0, result.output
    aws.deploy.assert_called_once()


def test_ordinary_instance_type_is_never_questioned(monkeypatch) -> None:
    aws = _facade([_target(False, "t3.medium")])
    monkeypatch.setattr(cli, "AwsFacade", Mock(return_value=aws))
    monkeypatch.setattr(cli, "_confirm_registered_networks", Mock())
    asked = Mock(side_effect=AssertionError("must not prompt for a preferred type"))
    monkeypatch.setattr("builtins.input", asked)

    result = CliRunner().invoke(cli.app, ["deploy", "--config", _written_config()])

    assert result.exit_code == 0, result.output
    aws.deploy.assert_called_once()


def test_no_console_stops_rather_than_spending_more(monkeypatch) -> None:
    aws = _facade([_target(True)])
    monkeypatch.setattr(cli, "AwsFacade", Mock(return_value=aws))

    def _eof(*_a):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    result = CliRunner().invoke(cli.app, ["deploy", "--config", _written_config()])

    assert result.exit_code == 1
    aws.resolve_eips.assert_not_called()


def test_yes_accepts_the_substituted_type_without_asking(monkeypatch) -> None:
    aws = _facade([_target(True)])
    monkeypatch.setattr(cli, "AwsFacade", Mock(return_value=aws))
    asked = Mock(side_effect=AssertionError("--yes must not prompt"))
    monkeypatch.setattr("builtins.input", asked)

    result = CliRunner().invoke(cli.app, ["deploy", "--config", _written_config(), "--yes"])

    assert result.exit_code == 0, result.output
    aws.deploy.assert_called_once()
