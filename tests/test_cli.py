from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

from swg_te_monitor import cli
from swg_te_monitor.models import DeploymentConfig

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
