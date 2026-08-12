"""Generate standalone CloudFormation templates without calling AWS APIs."""

import base64
import json
from pathlib import Path

from .locations import LOCATIONS
from .models import DeploymentConfig

MANUAL_INSTANCE_TYPES = {"dallas": "t3.medium", "miami": "t3.medium"}


def _yaml_scalar(value: str) -> str:
    return json.dumps(value)


def generate_templates(config: DeploymentConfig, source: Path, bootstrap: Path) -> dict[str, str]:
    """Return one self-contained template per selected location."""
    original = source.read_text(encoding="utf-8")
    bootstrap_b64 = base64.b64encode(bootstrap.read_bytes()).decode("ascii")
    generated: dict[str, str] = {}
    for key in config.locations:
        location = LOCATIONS[key]
        zone = (
            f"{location.preferred_zone_group}a"
            if location.preferred_zone_group
            else f"{location.region}a"
        )
        body = original
        instance_type = MANUAL_INSTANCE_TYPES.get(key, config.instance_type)
        replacements = {
            "  LocationCode: {Type: String, AllowedPattern: '^[a-z0-9]{3,5}$'}": (
                "  LocationCode:\n    Type: String\n    Default: " + _yaml_scalar(location.code)
            ),
            "  AvailabilityZone: {Type: String, AllowedPattern: '^[a-z0-9-]+$'}": (
                "  AvailabilityZone:\n    Type: String\n"
                "    AllowedPattern: '^[a-z0-9-]+$'\n"
                f"    Default: {_yaml_scalar(zone)}"
            ),
            "  InstanceType: {Type: String, Default: t3.small}": (
                f"  InstanceType:\n    Type: String\n    Default: {_yaml_scalar(instance_type)}"
            ),
            "  ThousandEyesToken: {Type: String, NoEcho: true}": (
                "  ThousandEyesToken:\n    Type: String\n    NoEcho: true\n"
                f"    Default: {_yaml_scalar(config.thousandeyes_token.get_secret_value())}"
            ),
            "  PacUrl: {Type: String, NoEcho: true}": (
                "  PacUrl:\n    Type: String\n    NoEcho: true\n"
                f"    Default: {_yaml_scalar(config.pac_url.get_secret_value())}"
            ),
            "  SshEnabled: {Type: String, AllowedValues: ['true', 'false'], Default: 'false'}": (
                "  SshEnabled:\n    Type: String\n    AllowedValues: ['true', 'false']\n"
                f"    Default: '{'true' if config.ssh_enabled else 'false'}'"
            ),
            "  AdminCidr: {Type: String, Default: 127.0.0.1/32}": (
                "  AdminCidr:\n    Type: String\n"
                f"    Default: {_yaml_scalar(str(config.admin_cidr or '127.0.0.1/32'))}"
            ),
            "  KeyPairName: {Type: String, Default: ''}": (
                "  KeyPairName:\n    Type: String\n"
                f"    Default: {_yaml_scalar(config.key_pair_name or '')}"
            ),
        }
        for old, new in replacements.items():
            body = body.replace(old, new)
        body = body.replace("  BootstrapBucket: {Type: String}\n", "")
        body = body.replace("  BootstrapKey: {Type: String}\n", "")
        body = body.replace(
            "  BootstrapSha256: {Type: String, AllowedPattern: '^[a-f0-9]{64}$'}\n", ""
        )
        s3_policy = """      Policies:
        - PolicyName: ReadBootstrap
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: ['s3:GetObject']
                Resource: !Sub 'arn:${AWS::Partition}:s3:::${BootstrapBucket}/${BootstrapKey}'
"""
        body = body.replace(s3_policy, "")
        download = """          aws s3 cp \
            's3://${BootstrapBucket}/${BootstrapKey}' /usr/local/sbin/swg-te-bootstrap
          echo '${BootstrapSha256}  /usr/local/sbin/swg-te-bootstrap' | sha256sum -c -
"""
        if download not in body:
            download = (
                "          aws s3 cp 's3://${BootstrapBucket}/${BootstrapKey}' "
                "/usr/local/sbin/swg-te-bootstrap\n"
                "          echo '${BootstrapSha256}  /usr/local/sbin/swg-te-bootstrap' "
                "| sha256sum -c -\n"
            )
        inline = (
            "          echo '"
            + bootstrap_b64
            + "' | base64 --decode > /usr/local/sbin/swg-te-bootstrap\n"
        )
        body = body.replace(download, inline)
        body = body.replace(
            "Description: One Cisco Secure Access / ThousandEyes monitoring location",
            f"Description: Cisco Secure Access / ThousandEyes monitor for {location.label}",
        )
        generated[key] = body
    return generated


def write_templates(
    config: DeploymentConfig, source: Path, bootstrap: Path, output: Path
) -> list[Path]:
    templates = generate_templates(config, source, bootstrap)
    paths: list[Path] = []
    for key, body in templates.items():
        path = (
            output
            if len(templates) == 1
            else output.with_name(f"{output.stem}-{key}{output.suffix}")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        paths.append(path)
    return paths
