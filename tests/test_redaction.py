from swg_te_monitor.redaction import REDACTED, Redactor


def test_supplied_secrets_are_always_redacted() -> None:
    sensitive_value = "unit-test-sensitive-value"
    output = Redactor([sensitive_value]).redact(
        f"token={sensitive_value} passphrase={sensitive_value}"
    )
    assert sensitive_value not in output
    assert REDACTED in output
