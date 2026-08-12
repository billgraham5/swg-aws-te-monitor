from pathlib import Path


def test_local_zone_opt_in_is_inline_and_bounded() -> None:
    template = (Path(__file__).parents[1] / "infrastructure" / "local-zone-opt-in.yaml").read_text()
    assert "ec2:ModifyAvailabilityZoneGroup" in template
    assert "ZipFile:" in template
    assert "Timeout: 600" in template
    assert "RetentionInDays: 7" in template
    assert "s3://" not in template


def test_ec2_instances_remain_on_demand() -> None:
    template = (Path(__file__).parents[1] / "infrastructure" / "location.yaml").read_text()
    assert "InstanceMarketOptions" not in template
    assert "SpotOptions" not in template
