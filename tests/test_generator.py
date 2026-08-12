from pathlib import Path

import yaml

from swg_te_monitor.generator import generate_templates
from swg_te_monitor.models import DeploymentConfig


def test_generated_template_is_standalone(tmp_path: Path) -> None:
    source = tmp_path / "location.yaml"
    source.write_text(
        (Path(__file__).parents[1] / "infrastructure" / "location.yaml").read_text(),
        encoding="utf-8",
    )
    bootstrap = Path(__file__).parents[1] / "bootstrap" / "bootstrap.py"
    config = DeploymentConfig.model_validate(
        {
            "locations": ["dallas"],
            "thousandeyes_token": "test-token",
            "pac_url": "https://proxy.example.invalid/proxy.pac",
        }
    )

    body = generate_templates(config, source, bootstrap)["dallas"]

    assert "BootstrapBucket" not in body
    assert "BootstrapKey" not in body
    assert "aws s3 cp" not in body
    assert "base64 --decode" in body
    assert 'Default: "dfw"' in body
    assert 'Default: "us-east-1-dfw-2a"' in body
    assert 'Default: "t3.medium"' in body


def test_cloudformation_templates_do_not_use_yaml_anchors_or_aliases() -> None:
    infrastructure = Path(__file__).parents[1] / "infrastructure"
    for template in infrastructure.glob("*.yaml"):
        events = list(yaml.parse(template.read_text(encoding="utf-8")))
        anchored = [event for event in events if getattr(event, "anchor", None) is not None]
        assert not anchored, f"{template.name} contains YAML anchors or aliases"
