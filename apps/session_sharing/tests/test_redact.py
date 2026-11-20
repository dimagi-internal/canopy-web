"""Unit tests for the best-effort secret scrub."""
from __future__ import annotations

from apps.session_sharing import redact


def test_openai_style_key_redacted():
    text = "use sk-abcdEFGH1234567890ijklMNOP as the key"
    out, n = redact.redact_text(text)
    assert n == 1
    assert "sk-abcd" not in out
    assert "‹redacted:api-key›" in out


def test_github_token_redacted():
    out, n = redact.redact_text("token=ghp_0123456789abcdefABCDEF0123456789abcd")
    assert n == 1
    assert "ghp_" not in out


def test_authorization_bearer_keeps_prefix():
    out, n = redact.redact_text("Authorization: Bearer abcdef123456ghijkl")
    assert n == 1
    assert out.startswith("Authorization: Bearer ")
    assert "abcdef123456ghijkl" not in out


def test_env_assignment_redacted_value_kept_name():
    out, n = redact.redact_text('AWS_SECRET_ACCESS_KEY="hunter2hunter2hunter2"')
    assert n == 1
    assert out.startswith("AWS_SECRET_ACCESS_KEY=")
    assert "hunter2" not in out


def test_jwt_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.QWERTYuiop1234567890abcd"
    out, n = redact.redact_text(f"cookie {jwt}")
    assert n == 1
    assert jwt not in out


def test_private_key_block_redacted():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA...\nlots of base64...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, n = redact.redact_text(f"key:\n{block}\ndone")
    assert n == 1
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert out.endswith("done")


def test_ordinary_prose_and_code_not_mangled():
    # False-positive guard: nothing secret-shaped here.
    text = "def add(a, b):\n    return a + b  # simple helper, version=1.2.3"
    out, n = redact.redact_text(text)
    assert n == 0
    assert out == text


def test_nul_bytes_stripped_not_counted():
    # Postgres jsonb/text reject \x00 — it must be removed (but not counted as
    # a secret redaction) or bulk_create 500s the whole upload.
    out, n = redact.redact_text("before\x00after")
    assert out == "beforeafter"
    assert n == 0


def test_redact_turn_strips_nul_in_nested_content():
    _, content, n = redact.redact_turn(
        "ok", {"type": "tool_result", "content": [{"type": "text", "text": "a\x00b"}]}
    )
    import json as _json

    assert "\x00" not in _json.dumps(content)


def test_redact_turn_walks_nested_content():
    content = {
        "type": "tool_result",
        "content": [{"type": "text", "text": "key sk-abcdEFGH1234567890ijklMNOP here"}],
    }
    plaintext, new_content, n = redact.redact_turn("plain text", content)
    assert n == 1
    assert "sk-abcd" not in str(new_content)
    assert plaintext == "plain text"


def test_canopy_and_cloud_credentials_in_agent_work_are_scrubbed():
    for text, marker in [
        ("cact_" + "A1b2C3d4E5f6G7h8I9j0K1l2", "canopy-token"),
        ("sealed zks1:AbCdEfGhIjKlMnOpQrStUv==", "sealed-secret"),
        ("token ya29.A0ARrdaM-AbCdEfGhIjKlMnOpQrSt", "google-token"),
        ("postgres://canopy:hunter2pass@db.internal/canopy", "url-credential"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==", "basic-auth"),
    ]:
        out, n = redact.redact_text(text)
        assert marker in out and n >= 1, (text, out)
    out, _ = redact.redact_text("see https://canopy.dimagi.com/w/x/projects/y and a:b")
    assert "redacted" not in out                                       # a plain URL is left alone
