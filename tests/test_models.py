import pytest
from pydantic import ValidationError

from swg_te_monitor.locations import LOCATIONS, agent_name
from swg_te_monitor.models import DeploymentConfig

BASE = {
    "locations": ["denver"],
    "thousandeyes_token": "unit-test-token",
    "pac_url": "https://proxy.example.invalid/proxy.pac",
}


def test_valid_minimal_config_redacts_secrets_in_repr() -> None:
    config = DeploymentConfig.model_validate(BASE)
    assert config.deployment_method == "deploy"
    rendered = repr(config)
    assert "unit-test-token" not in rendered


def test_manual_deployment_method_is_persisted() -> None:
    config = DeploymentConfig.model_validate({**BASE, "deployment_method": "generate"})
    assert config.deployment_method == "generate"


@pytest.mark.parametrize(
    "profile",
    ["", "default", "Default AWS credential chain", "the default AWS credential chain"],
)
def test_default_credential_chain_labels_are_not_profile_names(profile: str) -> None:
    config = DeploymentConfig.model_validate({**BASE, "aws_profile": profile})
    assert config.aws_profile is None


def test_named_aws_profile_is_preserved() -> None:
    config = DeploymentConfig.model_validate({**BASE, "aws_profile": " company-sso "})
    assert config.aws_profile == "company-sso"


def test_invalid_deployment_method_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate({**BASE, "deployment_method": "unexpected"})


def test_ssh_requires_cidr_and_key() -> None:
    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate({**BASE, "ssh_enabled": True})


def test_unknown_location_rejected() -> None:
    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate({**BASE, "locations": ["silently-substituted-city"]})


def test_agent_name_is_deterministic() -> None:
    assert agent_name(LOCATIONS["denver"]) == "den-aws-te"
    assert agent_name(LOCATIONS["denver"], "02") == "den-aws-te-02"
