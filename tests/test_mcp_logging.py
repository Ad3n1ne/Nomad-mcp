import pytest

from nomad.mcp_logging import redact_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("API_TOKEN=alpha", "API_TOKEN=[REDACTED]"),
        ("service_token: beta", "service_token: [REDACTED]"),
        ("PASSWORD = hunter2", "PASSWORD = [REDACTED]"),
        ("PASSWD='quoted value'", "PASSWD='[REDACTED]'"),
        ('{"API_KEY":"json-value"}', '{"API_KEY":"[REDACTED]"}'),
        (
            '{"AWS_SECRET_ACCESS_KEY": "aws-value"}',
            '{"AWS_SECRET_ACCESS_KEY": "[REDACTED]"}',
        ),
        ("KEY=private-value", "KEY=[REDACTED]"),
        ("PRIVATE_KEY=private-value", "PRIVATE_KEY=[REDACTED]"),
        ("SSH_PRIVATE_KEY: private-value", "SSH_PRIVATE_KEY: [REDACTED]"),
        ("SIGNING_KEY='private value'", "SIGNING_KEY='[REDACTED]'"),
        ("AUTH=Bearer bearer-value", "AUTH=[REDACTED]"),
        ("CREDENTIAL: credential-value", "CREDENTIAL: [REDACTED]"),
        ("client_secret=secret-value", "client_secret=[REDACTED]"),
    ],
)
def test_redact_text_masks_sensitive_assignments(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "The tokenizer counts tokens and authentication failed.",
        "TOKENIZER=value AUTHOR=alice MONKEY=banana KEYNOTE=value",
        "api_keynote=value credentialed_user=alice",
        "Keep this secret from ordinary log readers.",
    ],
)
def test_redact_text_preserves_non_sensitive_text(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "key_type",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ],
)
def test_redact_text_masks_multiline_pem_private_keys(key_type):
    private_body = "line-one-secret\nline-two-secret"
    text = (
        f"before\n-----BEGIN {key_type}-----\n"
        f"{private_body}\n"
        f"-----END {key_type}-----\nafter"
    )

    redacted = redact_text(text)

    assert private_body not in redacted
    assert redacted == (
        f"before\n-----BEGIN {key_type}-----\n"
        "[REDACTED]\n"
        f"-----END {key_type}-----\nafter"
    )
