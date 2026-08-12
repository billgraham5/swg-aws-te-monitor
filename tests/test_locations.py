from swg_te_monitor.locations import LOCATIONS


def test_expected_location_mappings() -> None:
    assert LOCATIONS["miami"].preferred_zone_group == "us-east-1-mia-2"
    assert LOCATIONS["dallas"].preferred_zone_group == "us-east-1-dfw-2"
    assert LOCATIONS["denver"].preferred_zone_group == "us-west-2-den-1"
    assert LOCATIONS["los-angeles"].preferred_zone_group == "us-west-2-lax-1"
    assert LOCATIONS["san-jose"].region == "us-west-1"
