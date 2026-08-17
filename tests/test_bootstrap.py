import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "bootstrap" / "bootstrap.py"
SPEC = importlib.util.spec_from_file_location("bootstrap", MODULE_PATH)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules["bootstrap"] = bootstrap
SPEC.loader.exec_module(bootstrap)


def test_pac_rejects_http() -> None:
    with pytest.raises(RuntimeError, match="HTTPS"):
        bootstrap.validate_pac("http://proxy.example.invalid/proxy.pac")


def test_pac_validates_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap,
        "get_url",
        lambda _url: ("function FindProxyForURL(url, host) { return 'DIRECT'; }", "text/plain"),
    )
    bootstrap.validate_pac("https://proxy.example.invalid/proxy.pac")


def test_pac_rejects_html(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "get_url", lambda _url: ("<html>login</html>", "text/html"))
    with pytest.raises(RuntimeError):
        bootstrap.validate_pac("https://proxy.example.invalid/proxy.pac")


def test_check_proxy_reachable_succeeds_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "validate_pac", lambda _url: calls.append("call"))
    sleeps: list[int] = []
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: sleeps.append(seconds))

    bootstrap.check_proxy_reachable("https://proxy.example.invalid/proxy.pac")

    assert len(calls) == 1
    assert sleeps == []


def test_check_proxy_reachable_retries_and_raises_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def _fail(_url: str) -> None:
        attempts.append(1)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(bootstrap, "validate_pac", _fail)
    sleeps: list[int] = []
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(RuntimeError) as excinfo:
        bootstrap.check_proxy_reachable(
            "https://proxy.example.invalid/proxy.pac", attempts=3, delay=5
        )

    assert len(attempts) == 3
    assert sleeps == [5, 5]
    assert str(excinfo.value) == (
        "The SWG proxy is not reachable. Please check that each EIP is provisioned "
        "as a Registered Network in Cisco Secure Access."
    )


def test_public_ip_uses_aws_metadata_when_it_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The EC2 path must keep working unchanged now that GCP shares the function."""
    monkeypatch.setattr(bootstrap, "_aws_public_ip", lambda: "52.207.21.25")
    monkeypatch.setattr(
        bootstrap, "_gcp_public_ip", lambda: pytest.fail("GCP must not be consulted on EC2")
    )
    monkeypatch.setattr(
        bootstrap, "METADATA_PROVIDERS", (bootstrap._aws_public_ip, bootstrap._gcp_public_ip)
    )
    assert str(bootstrap.native_public_ip()) == "52.207.21.25"


def test_public_ip_falls_back_to_gcp_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """On GCP the EC2 token endpoint does not exist, so the next provider answers."""

    def no_aws() -> str:
        raise OSError("metadata service not found")

    monkeypatch.setattr(bootstrap, "_aws_public_ip", no_aws)
    monkeypatch.setattr(bootstrap, "_gcp_public_ip", lambda: "34.174.205.53")
    monkeypatch.setattr(bootstrap, "METADATA_PROVIDERS", (no_aws, bootstrap._gcp_public_ip))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)
    assert str(bootstrap.native_public_ip()) == "34.174.205.53"


def test_public_ip_retries_then_reports_no_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow association is retried; exhausting every attempt is a hard failure."""
    attempts: list[int] = []

    def never() -> str:
        attempts.append(1)
        raise OSError("not ready")

    sleeps: list[int] = []
    monkeypatch.setattr(bootstrap, "METADATA_PROVIDERS", (never,))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: sleeps.append(seconds))
    with pytest.raises(RuntimeError, match="no public IPv4 address"):
        bootstrap.native_public_ip()
    assert len(attempts) == 12
    assert sleeps == [5] * 11
