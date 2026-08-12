"""Central redaction for logs and diagnostics."""

import re
from collections.abc import Iterable

REDACTED = "<REDACTED>"


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = re.sub(
            r"(?i)(token|passphrase|preshared[_ -]?key)\s*[:=]\s*\S+", r"\1=<REDACTED>", text
        )
        return text
