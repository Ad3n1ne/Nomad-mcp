from nomad.truncate import filter_noise, safe_truncate


def test_safe_truncate_keeps_short_output():
    assert safe_truncate("line 1\nline 2") == "line 1\nline 2"


def test_safe_truncate_limits_by_lines_before_bytes():
    output = "\n".join(f"line {index}" for index in range(5))

    truncated = safe_truncate(output, max_lines=2, max_bytes=10_000)

    assert truncated.startswith("line 0\nline 1")
    assert "line 2" not in truncated
    assert "truncated" in truncated
    assert "3 lines" in truncated
    assert "grep" in truncated
    assert "head" in truncated
    assert "tail" in truncated
    assert "task_status" in truncated


def test_safe_truncate_limits_by_bytes_after_lines():
    output = "abcdef" * 10

    truncated = safe_truncate(output, max_lines=200, max_bytes=12)

    assert truncated.startswith("abcdefabcdef")
    assert "truncated by bytes" in truncated
    assert "grep" in truncated
    assert "head" in truncated
    assert "tail" in truncated


def test_safe_truncate_checks_bytes_after_line_truncation():
    output = "\n".join(["x" * 120, "y" * 120, "z" * 120])

    truncated = safe_truncate(output, max_lines=2, max_bytes=50)

    assert len(truncated.encode("utf-8")) < 365
    assert "truncated the last 1 lines" in truncated
    assert "truncated by bytes" in truncated


def test_safe_truncate_handles_multibyte_utf8_without_error():
    output = "你好世界" * 10

    truncated = safe_truncate(output, max_lines=200, max_bytes=7)

    assert truncated.startswith("你好")
    assert "\ufffd" not in truncated
    assert "truncated by bytes" in truncated


def test_safe_truncate_strips_ansi_escape_codes_before_counting():
    output = "\x1b[31merror\x1b[0m"

    assert safe_truncate(output) == "error"


def test_filter_noise_removes_empty_and_progress_noise():
    lines = [
        "",
        "Downloading package 25%",
        "Already up to date.",
        "useful output",
    ]

    assert filter_noise(lines) == ["useful output"]


def test_filter_noise_preserves_error_lines():
    lines = [
        "ERROR: Downloading package failed at 25%",
        "FAILED test_example",
        "Traceback (most recent call last):",
        "Downloading package 25% ERROR failed checksum mismatch",
        "Downloading package 25% failed",
    ]

    assert filter_noise(lines) == lines
