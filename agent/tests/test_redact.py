import pytest

from app.redact import redact


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("password=hunter2", "hunter2"),
        ('DB_PASSWORD: "s3cr3t value"', "s3cr3t"),
        ("api_key=abc123def", "abc123def"),
        ("key AIzaSyA1234567890123456789012345678901 used", "AIzaSy"),
        ("key AQ.FAKEfakeFAKEfakeFAKEfakeFAKEfake00 used", "FAKEfake"),
        ("bot 1234567890:FAKEtokenFAKEtokenFAKEtokenFAKE0", "FAKEtoken"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnop"),
        ("redis://user:pa55word@redis:6379/0", "pa55word"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123", "eyJzdWIi"),
    ],
)
def test_secrets_are_removed(text, secret):
    assert secret not in redact(text)
    assert "[REDACTED]" in redact(text)


def test_telegram_token_inside_a_url():
    url = "POST https://api.telegram.org/bot1234567890:FAKEtokenFAKEtokenFAKEtokenFAKE0/sendMessage"
    assert redact(url) == "POST https://api.telegram.org/bot[REDACTED]/sendMessage"


def test_agent_logs_never_contain_secrets(capsys):
    import logging

    from app.logging_config import configure_logging

    configure_logging("INFO")
    secret_url = "https://api.telegram.org/bot1234567890:FAKEtokenFAKEtokenFAKEtokenFAKE0/getUpdates"
    logging.getLogger("httpx").info("HTTP Request: POST %s", secret_url)  # silenced
    logging.getLogger("kubesentinel").warning(
        "telegram call failed for %s", secret_url
    )  # redacted
    out = capsys.readouterr().out
    assert "FAKEtoken" not in out
    assert out.count("\n") == 1 and "bot[REDACTED]" in out


def test_ordinary_text_is_untouched():
    text = "pod orders-api-7f9c: restarts 3, last terminated OOMKilled (exit 137), limit 128Mi"
    assert redact(text) == text
