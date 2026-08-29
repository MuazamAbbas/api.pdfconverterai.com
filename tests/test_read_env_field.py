"""Regression coverage for scripts/read_env_field.py.

This tool exists specifically because a masking bug (a regex that silently
failed to match the real .env value's shape) leaked a live MongoDB password
into a session transcript on 2026-08-27 -- see the project's
`mongo_credential_incident` memory. Its whole job is "never leak the secret
in masked mode"; these tests pin that down so it can't silently regress,
covering both the .env parsing (`parse_env_file`) and the masking logic
(`mask_value`) directly, plus the CLI's `--reveal` path end-to-end.

Loaded via importlib rather than a normal import since `scripts/` is a
standalone-tools directory, not part of the `app` package.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "read_env_field.py"

_spec = importlib.util.spec_from_file_location("read_env_field", SCRIPT_PATH)
read_env_field = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(read_env_field)

parse_env_file = read_env_field.parse_env_file
mask_value = read_env_field.mask_value


# ---------------------------------------------------------------------------
# parse_env_file
# ---------------------------------------------------------------------------

def test_parse_env_file_basic_and_quoting(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment line",
                "",
                "PLAIN=value1",
                'DOUBLE_QUOTED="hello world"',
                "SINGLE_QUOTED='hello world'",
                "export EXPORTED=foo",
                "EMPTY=",
                "DUPLICATE=first",
                "DUPLICATE=second",
            ]
        ),
        encoding="utf-8",
    )
    values = parse_env_file(str(env_file))
    assert values["PLAIN"] == "value1"
    assert values["DOUBLE_QUOTED"] == "hello world"
    assert values["SINGLE_QUOTED"] == "hello world"
    assert values["EXPORTED"] == "foo"
    assert values["EMPTY"] == ""
    assert values["DUPLICATE"] == "second"  # last occurrence wins
    assert "# a comment line" not in values


def test_parse_env_file_missing_var_not_in_dict(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_VAR=x\n", encoding="utf-8")
    values = parse_env_file(str(env_file))
    assert "MISSING_VAR" not in values


# ---------------------------------------------------------------------------
# mask_value
# ---------------------------------------------------------------------------

def test_mask_value_not_set_upstream_handled_separately():
    # parse_env_file simply omits missing keys; masking "NOT SET" is the
    # CLI's job (tested via subprocess below), not mask_value's.
    pass


def test_mask_value_empty():
    assert mask_value("") == "SET (length=0)"


def test_mask_value_plain_secret_shows_length_only():
    secret = "sk-or-v1-abcdef0123456789"
    assert mask_value(secret) == f"SET (length={len(secret)})"


def test_mask_value_non_url_with_scheme_lookalike():
    value = "just some text with :// in it but no creds"
    assert mask_value(value) == f"SET (length={len(value)})"


def test_mask_value_strips_url_credentials():
    masked = mask_value("mongodb://admin:s3cr3tPass123@localhost:27017/pdfconverterai?authSource=admin")
    assert "s3cr3tPass123" not in masked
    assert "admin:s3cr3tPass123" not in masked
    assert masked == "mongodb://***:***@localhost:27017/pdfconverterai?authSource=***"


def test_mask_value_no_credentials_no_masking_needed():
    # A URL with no userinfo has nothing to hide from the netloc; falls back
    # to length-only masking (not reconstructed/leaked verbatim).
    value = "https://example.com/api?token=abc123"
    masked = mask_value(value)
    assert "abc123" not in masked
    assert masked == f"SET (length={len(value)})"


def test_mask_value_query_string_secret_is_masked():
    # Regression for the finding from PR #72's security review: a query
    # param can carry its own secret independent of userinfo credentials.
    masked = mask_value("https://user:pass@host.com/path?api_key=SUPERSECRET&other=1")
    assert "pass" not in masked
    assert "SUPERSECRET" not in masked
    assert masked == "https://***:***@host.com/path?api_key=***&other=***"


def test_mask_value_fragment_secret_is_masked():
    masked = mask_value("postgres://user:pass@host/db#fragmentsecret=abc")
    assert "pass" not in masked
    assert "fragmentsecret=abc" not in masked
    assert masked == "postgres://***:***@host/db#***"


def test_mask_value_ipv6_host_keeps_brackets():
    masked = mask_value("redis://:mypassword@[::1]:6379/0")
    assert "mypassword" not in masked
    assert masked == "redis://***:***@[::1]:6379/0"


@pytest.mark.parametrize(
    "value",
    [
        "mongodb://user:pa/ss@host/db",  # password containing a slash
        "mongodb://user:pa?ss@host/db",  # password containing a question mark
        "mongodb://user:pa#ss@host/db",  # password containing a hash
    ],
)
def test_mask_value_malformed_credential_urls_fail_safe(value):
    # urlsplit can't parse these as username/password on a malformed netloc
    # (the special char breaks it before the userinfo `@`) -- confirm the
    # fallback never reconstructs/leaks the raw value.
    masked = mask_value(value)
    assert masked == f"SET (length={len(value)})"


def test_mask_value_password_with_at_sign_still_parses_and_masks_safely():
    # Unlike the cases above, urlsplit *does* successfully split this one on
    # the last `@` -- confirm it's still masked correctly either way and the
    # raw password substring never survives into the output.
    value = "mongodb://user:pa@ss@host/db"
    masked = mask_value(value)
    assert "pa@ss" not in masked
    assert masked in (f"SET (length={len(value)})", "mongodb://***:***@host/db")


# ---------------------------------------------------------------------------
# CLI end-to-end (subprocess), including --reveal
# ---------------------------------------------------------------------------

def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
    )


def test_cli_masked_mode_default(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=mongodb://admin:secret@localhost:27017/db\n", encoding="utf-8")
    result = _run_cli("--file", str(env_file), "--var", "DATABASE_URL")
    assert result.returncode == 0
    assert "secret" not in result.stdout
    assert result.stdout.strip() == "mongodb://***:***@localhost:27017/db"


def test_cli_reveal_mode_returns_exact_value(tmp_path):
    env_file = tmp_path / ".env"
    raw = "mongodb://admin:secret@localhost:27017/db?authSource=admin"
    env_file.write_text(f"DATABASE_URL={raw}\n", encoding="utf-8")
    result = _run_cli("--file", str(env_file), "--var", "DATABASE_URL", "--reveal")
    assert result.returncode == 0
    assert result.stdout.strip() == raw


def test_cli_missing_var_reports_not_set_exit_1(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_OTHER_VAR=x\n", encoding="utf-8")
    result = _run_cli("--file", str(env_file), "--var", "MISSING")
    assert result.returncode == 1
    assert result.stdout.strip() == "NOT SET"


def test_cli_unreadable_file_reports_error_exit_2(tmp_path):
    missing_path = tmp_path / "does-not-exist.env"
    result = _run_cli("--file", str(missing_path), "--var", "ANYTHING")
    assert result.returncode == 2
    assert "ERROR" in result.stderr


def test_cli_non_utf8_file_fails_clean_not_with_traceback(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"KEY=\xff\xfe invalid utf8 bytes")
    result = _run_cli("--file", str(env_file), "--var", "KEY")
    assert result.returncode == 2
    assert "ERROR" in result.stderr
    assert "Traceback" not in result.stderr
