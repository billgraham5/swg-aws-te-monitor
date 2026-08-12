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
