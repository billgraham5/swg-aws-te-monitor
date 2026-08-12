"""Curated location intent; AWS APIs remain authoritative for actual availability."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    key: str
    label: str
    code: str
    region: str
    preferred_zone_group: str | None = None
    approximation: str | None = None


LOCATIONS: dict[str, Location] = {
    item.key: item
    for item in (
        Location("northern-virginia", "Northern Virginia (Region)", "iad", "us-east-1"),
        Location("ohio", "Ohio (Region)", "cmh", "us-east-2"),
        Location("miami", "Miami (Local Zone)", "mia", "us-east-1", "us-east-1-mia-2"),
        Location("dallas", "Dallas (Local Zone)", "dfw", "us-east-1", "us-east-1-dfw-2"),
        Location("denver", "Denver (Local Zone)", "den", "us-west-2", "us-west-2-den-1"),
        Location("oregon", "Oregon (Region)", "pdx", "us-west-2"),
        Location(
            "san-jose",
            "San Jose (Northern California Region)",
            "sjc",
            "us-west-1",
            approximation=(
                "AWS Region placement is Northern California; validate latency to San Jose."
            ),
        ),
        Location("los-angeles", "Los Angeles (Local Zone)", "lax", "us-west-2", "us-west-2-lax-1"),
    )
}


def agent_name(location: Location, suffix: str | None = None) -> str:
    """Return a deterministic, collision-safe agent hostname."""
    base = f"{location.code}-aws-te"
    return f"{base}-{suffix}" if suffix else base
