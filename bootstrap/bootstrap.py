#!/usr/bin/env python3
"""Idempotent, fail-closed EC2 bootstrap for ThousandEyes."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess  # nosec B404
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

STATE_DIR = Path("/var/lib/swg-te-monitor")
STATUS = STATE_DIR / "status.json"
ENV_FILE = Path("/etc/swg-te-monitor/bootstrap.env")


class Phase(str, Enum):
    STARTED = "started"
    SECRETS = "secrets-retrieved"
    NATIVE_EGRESS = "native-egress-recorded"
    PROXY_REACHABLE = "proxy-reachable"
    INSTALLED = "agent-installed"
    HEALTHY = "healthy"
    FAILED = "failed"


@dataclass(frozen=True)
class Settings:
    thousandeyes_token: str
    pac_url: str
    agent_name: str


def update_status(phase: Phase, detail: str = "") -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"phase": phase.value, "detail": detail, "updated": int(time.time())}
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(STATUS)


def run(args: list[str], *, input_text: str | None = None, timeout: int = 300) -> str:
    # Executable paths are fixed and the shell is never used.
    result = subprocess.run(  # noqa: S603  # nosec B603
        args,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        # TERM avoids spurious non-zero exits from vendor scripts that call
        # tput for colored status output (e.g. the ThousandEyes installer)
        # and don't check its exit status themselves.
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TERM": "xterm",
        },
    )
    if result.returncode:
        raise RuntimeError(f"command failed ({args[0]}, rc={result.returncode})")
    return result.stdout


def read_settings() -> Settings:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return Settings(
        thousandeyes_token=values["THOUSANDEYES_TOKEN"],
        pac_url=values["PAC_URL"],
        agent_name=values["AGENT_NAME"],
    )


def get_url(url: str, timeout: int = 10, limit: int = 1_048_576) -> tuple[str, str]:
    request = urllib.request.Request(  # noqa: S310 - callers require HTTPS
        url, headers={"User-Agent": "swg-te-monitor/0.1"}
    )
    with urllib.request.urlopen(  # noqa: S310  # nosec B310
        request, timeout=timeout
    ) as response:
        final_url = response.geturl()
        if not final_url.startswith("https://"):
            raise RuntimeError("HTTPS request redirected to a non-HTTPS URL")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError("response exceeds safety limit")
        return data.decode("utf-8", errors="strict"), response.headers.get_content_type()


def _aws_public_ip() -> str:
    """Read the public IPv4 address from the EC2 instance metadata service."""
    token_request = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with urllib.request.urlopen(  # noqa: S310  # nosec B310
        token_request, timeout=2
    ) as response:
        token = response.read().decode()
    request = urllib.request.Request(
        "http://169.254.169.254/latest/meta-data/public-ipv4",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with urllib.request.urlopen(  # noqa: S310  # nosec B310
        request, timeout=2
    ) as response:
        address: str = response.read().decode().strip()
    return address


def _gcp_public_ip() -> str:
    """Read the external IPv4 address from the Compute Engine metadata server.

    GCP exposes the address under the interface's access config rather than as
    a top-level key, and refuses any request that omits the Metadata-Flavor
    header, so this cannot share a code path with the EC2 lookup.
    """
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1"
        "/instance/network-interfaces/0/access-configs/0/external-ip",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(  # noqa: S310  # nosec B310
        request, timeout=2
    ) as response:
        address: str = response.read().decode().strip()
    return address


# Tried in order until one answers. Each fails fast on the wrong cloud: the EC2
# token endpoint does not exist on GCP, and metadata.google.internal does not
# resolve on EC2.
METADATA_PROVIDERS = (_aws_public_ip, _gcp_public_ip)


def native_public_ip() -> ipaddress.IPv4Address:
    """Return the instance's public IPv4 address, whichever cloud it booted on.

    On AWS the address arrives with the Elastic IP that UserData associates
    after boot, so the metadata service may 404 briefly until then. On GCP the
    reserved static address is attached at creation and answers immediately.
    Both are polled the same way so a slow association never fails the boot.
    """
    last_exc: Exception | None = None
    for attempt in range(12):
        for provider in METADATA_PROVIDERS:
            try:
                return ipaddress.IPv4Address(provider())
            except Exception as exc:  # noqa: BLE001 - wrong cloud or not ready yet
                last_exc = exc
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError("no public IPv4 address associated with this instance") from last_exc


def validate_pac(url: str) -> None:
    if not url.startswith("https://"):
        raise RuntimeError("PAC URL must use HTTPS")
    body, content_type = get_url(url)
    if len(body) < 20 or "FindProxyForURL" not in body:
        raise RuntimeError("PAC response does not contain FindProxyForURL")
    if content_type not in {
        "application/x-ns-proxy-autoconfig",
        "application/javascript",
        "text/plain",
    }:
        raise RuntimeError(f"unexpected PAC content type: {content_type}")


def check_proxy_reachable(pac_url: str, attempts: int = 3, delay: int = 5) -> None:
    """Confirm the SWG proxy is reachable before installing ThousandEyes.

    A registered-network EIP that has not been manually verified in Cisco
    Secure Access yet will fail to reach the PAC file, so this retries a
    few times to absorb propagation delay before giving up.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            validate_pac(pac_url)
            return
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable yet"
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    raise RuntimeError(
        "The SWG proxy is not reachable. Please check that each EIP is provisioned "
        "as a Registered Network in Cisco Secure Access."
    ) from last_exc


def install_agent(settings: Settings, token: str, pac_url: str) -> None:
    if Path("/usr/bin/te-agent").exists() or Path("/usr/sbin/te-agent").exists():
        run(["/usr/bin/systemctl", "enable", "--now", "te-agent.service"])
        return
    # Not /run: that is a tmpfs mounted noexec on Debian, so the downloaded
    # installer cannot be executed from there. The state directory is on the
    # root filesystem on every image this runs on, and is already root-only.
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    installer = STATE_DIR / "install_thousandeyes.sh"
    body, _ = get_url("https://downloads.thousandeyes.com/agent/install_thousandeyes.sh")
    installer.write_text(body, encoding="utf-8")
    installer.chmod(0o700)
    try:
        # Vendor CLI requires the token as an argument. Host access remains SSM-only by default,
        # and the short-lived installer process is not logged.
        # -f forces batch mode; without it the installer blocks on an interactive
        # "change the default log path? [y/N]" prompt (and, without -f, a separate
        # BrowserBot confirmation prompt too).
        run(
            [str(installer), "-f", "-b", "-t", "PAC", "-P", pac_url, token],
            input_text="",
            timeout=1200,
        )
    finally:
        installer.unlink(missing_ok=True)
    run(["/usr/bin/systemctl", "enable", "--now", "te-agent.service"])


def main() -> int:
    settings = read_settings()
    try:
        update_status(Phase.STARTED)
        run(["/usr/bin/hostnamectl", "set-hostname", settings.agent_name])
        token = settings.thousandeyes_token
        pac_url = settings.pac_url
        update_status(Phase.SECRETS)
        native = native_public_ip()
        update_status(Phase.NATIVE_EGRESS, hashlib.sha256(str(native).encode()).hexdigest()[:12])
        check_proxy_reachable(pac_url)
        update_status(Phase.PROXY_REACHABLE)
        install_agent(settings, token, pac_url)
        update_status(Phase.INSTALLED)
        active = run(["/usr/bin/systemctl", "is-active", "te-agent.service"]).strip()
        if active != "active":
            raise RuntimeError("ThousandEyes service is not active")
        update_status(Phase.HEALTHY)
        return 0
    except Exception as exc:
        # The systemctl executable path is fixed and no shell is used.
        subprocess.run(  # nosec B603
            ["/usr/bin/systemctl", "disable", "--now", "te-agent.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        update_status(Phase.FAILED, re.sub(r"https?://\S+", "<REDACTED_URL>", str(exc)))
        # Keep a full traceback for post-mortem via SSM. Signal the failure
        # immediately rather than stalling: the most common failure is a PAC
        # fetch from an Elastic IP not yet registered in Cisco Secure Access,
        # and delaying only slows the retry once that registration is done.
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        traceback_path = STATE_DIR / "debug-traceback.txt"
        traceback_path.write_text(traceback.format_exc(), encoding="utf-8")
        traceback_path.chmod(0o600)
        return 1


if __name__ == "__main__":
    sys.exit(main())
