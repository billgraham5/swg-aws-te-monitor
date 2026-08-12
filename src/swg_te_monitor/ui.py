"""Interactive configuration UI."""

from getpass import getpass
from ipaddress import AddressValueError, IPv4Address, IPv4Network
from typing import Any, cast
from urllib.parse import urlparse

import boto3
import questionary
from pydantic import SecretStr

from .locations import LOCATIONS
from .models import DeploymentConfig

GENERATE_TEMPLATE = "generate"
CONFIGURE_AWS = "deploy"


def _ask(question: Any) -> Any:
    """Return a prompt answer, or honor Ctrl-C instead of treating it as empty input."""
    value = question.ask()
    if value is None:
        raise KeyboardInterrupt
    return value


def _required_hidden(prompt: str) -> SecretStr:
    while True:
        value = getpass(f"{prompt}: ")
        if value and value.strip():
            return SecretStr(value)
        questionary.print(f"{prompt} is required. Please try again.", style="fg:red")


def _required_text(prompt: str) -> str:
    while True:
        value = str(_ask(questionary.text(prompt)))
        if value and value.strip():
            return value.strip()
        questionary.print(f"{prompt.rstrip(':')} is required. Please try again.", style="fg:red")


def _optional_ssh_ip() -> IPv4Network | None:
    while True:
        value = str(_ask(questionary.text("Authorized SSH IP (blank for SSH disabled):"))).strip()
        if not value:
            return None
        try:
            address = IPv4Address(value)
        except AddressValueError:
            questionary.print(
                "Enter one valid IPv4 address without a CIDR suffix, or leave blank.",
                style="fg:red",
            )
            continue
        return IPv4Network(f"{address}/32")


def _required_https_url(prompt: str) -> SecretStr:
    while True:
        value = _required_text(prompt)
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.netloc and not any(char.isspace() for char in value):
            return SecretStr(value)
        questionary.print("Enter a complete HTTPS URL. Please try again.", style="fg:red")


def _required_locations(choices: list[questionary.Choice]) -> list[str]:
    while True:
        selected = _ask(
            questionary.checkbox(
                "Deployment locations (Space toggles, Enter confirms):", choices=choices
            )
        )
        if selected:
            return list(selected)
        questionary.print("Select at least one location. Please try again.", style="fg:red")


def deployment_mode() -> str:
    return str(
        _ask(
            questionary.select(
                "How should the deployment be performed?",
                choices=[
                    questionary.Choice(
                        "Generate CloudFormation template file for manual deployment",
                        value=GENERATE_TEMPLATE,
                    ),
                    questionary.Choice(
                        "Deploy to AWS with this CLI (run 'deploy' after configuring)",
                        value=CONFIGURE_AWS,
                    ),
                ],
            )
        )
    )


def aws_profile() -> str | None:
    profiles = boto3.Session().available_profiles
    choices = [questionary.Choice("Default AWS credential chain", value=None)]
    choices.extend(
        questionary.Choice(f"AWS profile: {profile}", value=profile) for profile in profiles
    )
    return cast(
        str | None,
        _ask(
            questionary.select(
                "Choose the AWS sign-in profile to use (SSO profiles are supported):",
                choices=choices,
            )
        ),
    )


def interactive_config(profile: str | None = None) -> DeploymentConfig:
    choices = [questionary.Choice(item.label, value=item.key) for item in LOCATIONS.values()]
    selected = _required_locations(choices)
    admin_cidr = _optional_ssh_ip()
    ssh = admin_cidr is not None
    key_name = None
    if ssh:
        key_name = _required_text("Existing EC2 key pair name (not the .pem filename):")
        if key_name.lower().endswith(".pem"):
            key_name = key_name[:-4]
            questionary.print(f"Using EC2 key pair name: {key_name}")
    te_token = _required_hidden("ThousandEyes Account Group Installation Token")
    pac_url = _required_https_url("Cisco Secure Access PAC URL:")
    return DeploymentConfig(
        aws_profile=profile,
        locations=selected,
        thousandeyes_token=te_token,
        pac_url=pac_url,
        ssh_enabled=ssh,
        admin_cidr=admin_cidr,
        key_pair_name=key_name,
    )
