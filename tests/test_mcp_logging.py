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
        "TOKENIZER=value AUTHOR=alice MONKEY=banana",
        "api_keynote=value credentialed_user=alice",
        "Keep this secret from ordinary log readers.",
    ],
)
def test_redact_text_preserves_non_sensitive_text(text):
    assert redact_text(text) == text
