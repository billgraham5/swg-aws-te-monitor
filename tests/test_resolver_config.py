"""The resolver override must survive an instance being rebuilt.

Every host in the fleet has to resolve names the same way for their results to
be comparable, so this is a property of the template that builds a host rather
than of anything installed on one afterwards.
"""

from pathlib import Path

TEMPLATE = Path("infrastructure/location.yaml")


def test_template_pins_the_resolver() -> None:
    body = TEMPLATE.read_text(encoding="utf-8")
    assert "10-swg-resolver.conf" in body
    assert "DNS=208.67.222.222 208.67.220.220" in body
    # Without this, systemd-resolved prefers the DHCP-supplied per-link servers
    # and the global setting is silently ignored.
    assert "Domains=~." in body


def test_template_reverts_rather_than_stranding_the_host() -> None:
    # An unreachable resolver costs SSM and leaves the host unmanageable, which
    # is worse than the wrong resolver: nothing reports a host nobody can reach.
    body = TEMPLATE.read_text(encoding="utf-8")
    assert "getent hosts checkip.amazonaws.com" in body
    assert "rm -f /etc/systemd/resolved.conf.d/10-swg-resolver.conf" in body
