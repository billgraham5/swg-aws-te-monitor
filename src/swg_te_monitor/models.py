"""Validated non-secret deployment models."""

from ipaddress import IPv4Network
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .locations import LOCATIONS


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aws_profile: str | None = None
    deployment_method: Literal["generate", "deploy"] = "deploy"
    locations: list[str]
    thousandeyes_token: SecretStr
    pac_url: SecretStr
    ssh_enabled: bool = False
    admin_cidr: IPv4Network | None = None
    key_pair_name: str | None = None
    instance_type: str = "t3.small"
    browserbot: bool = False
    agent_suffix: str | None = None
    vpc_cidr: IPv4Network = IPv4Network("10.97.0.0/24")
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("aws_profile", mode="before")
    @classmethod
    def normalize_default_profile(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped.lower() in {
                "default",
                "default aws credential chain",
                "the default aws credential chain",
            }:
                return None
            return stripped
        return value

    @field_validator("locations")
    @classmethod
    def validate_locations(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - LOCATIONS.keys())
        if unknown:
            raise ValueError(f"unknown locations: {', '.join(unknown)}")
        if not values:
            raise ValueError("at least one location is required")
        return list(dict.fromkeys(values))

    @field_validator("agent_suffix")
    @classmethod
    def safe_name(cls, value: str | None) -> str | None:
        if value is not None and not value.replace("-", "").isalnum():
            raise ValueError("may contain only letters, numbers, and hyphens")
        return value

    @field_validator("thousandeyes_token", "pac_url")
    @classmethod
    def validate_secret_value(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or "\n" in raw or "\r" in raw:
            raise ValueError("must be non-empty and contain no newlines")
        return value

    @model_validator(mode="after")
    def validate_ssh(self) -> "DeploymentConfig":
        if self.ssh_enabled and (self.admin_cidr is None or not self.key_pair_name):
            raise ValueError("SSH requires admin_cidr and an existing regional key_pair_name")
        if not self.ssh_enabled and (self.admin_cidr is not None or self.key_pair_name is not None):
            raise ValueError("admin_cidr/key_pair_name must be omitted when SSH is disabled")
        if self.browserbot and self.instance_type == "t3.small":
            raise ValueError(
                "BrowserBot requires at least 2 GiB RAM; choose an explicitly sized type"
            )
        return self

    def deployment_dict(self) -> dict[str, object]:
        """Serialize deployment values for a mode-0600, gitignored local file."""
        result = self.model_dump(mode="json", exclude_none=True)
        result["thousandeyes_token"] = self.thousandeyes_token.get_secret_value()
        result["pac_url"] = self.pac_url.get_secret_value()
        return result


def load_config(path: Path) -> DeploymentConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    return DeploymentConfig.model_validate(raw)
