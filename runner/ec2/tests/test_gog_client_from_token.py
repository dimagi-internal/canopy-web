"""The account->client map comes from the TOKEN, which declares its own client.

Settled 2026-09-06 by reading a real `op://Agent-Ace/gog-token` item:

    {"email": "ace@dimagi-ai.com", "client": "ace",
     "services": [...], "scopes": [...], "refresh_token": "..."}

A gog token carries the client it was minted for. That is the authoritative
answer, and neither of the two things this fleet has used is:

  - the HARDCODED table in bootstrap_agents.sh drifted (it is right for echo,
    wrong for nothing yet — but nothing keeps it honest);
  - `config/agent.json`'s `gog_client` is INTENT, not fact. On 2026-09-05 a fix
    made that authoritative, pointed echo at `canopy`, and broke a working
    mailbox — a token is minted FOR a client and works only with that client
    (reverted in canopy-web#662).

So: read the token. It cannot drift from itself. The table stays only as the
first-boot fallback, for the window before any token has been fetched.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"

_HAS_ASSOC = subprocess.run(
    ["bash", "-c", "declare -A _t=( [k]=v ) 2>/dev/null && echo yes"],
    capture_output=True, text=True,
).stdout.strip() == "yes"
pytestmark = pytest.mark.skipif(
    not _HAS_ASSOC, reason="bash lacks associative arrays (macOS ships 3.2; the box and CI run 5)"
)


def _fn(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _run(tokfile: str, slug: str) -> str:
    decl = next(l for l in SCRIPT.read_text().splitlines()
                if l.startswith("declare -A GOG_CLIENT="))
    script = f'set -euo pipefail\n{decl}\n{_fn("token_client")}\ntoken_client "{tokfile}" {slug}'
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    return res.stdout.strip()


def _tok(tmp: pathlib.Path, body) -> str:
    p = tmp / "tok.json"
    p.write_text(body if isinstance(body, str) else json.dumps(body))
    return str(p)


def test_the_token_names_its_own_client(tmp_path):
    """The real shape, from a live Agent-Ace item."""
    f = _tok(tmp_path, {
        "email": "ace@dimagi-ai.com", "client": "ace",
        "services": ["docs", "drive", "gmail", "sheets"],
        "scopes": ["https://www.googleapis.com/auth/drive"],
        "created_at": "2026-05-01T00:00:00Z", "refresh_token": "x",
    })
    assert _run(f, "ace") == "ace"


def test_a_token_minted_under_the_shared_client_says_so(tmp_path):
    f = _tok(tmp_path, {"email": "eva@dimagi-ai.com", "client": "canopy", "refresh_token": "x"})
    assert _run(f, "eva") == "canopy"


@pytest.mark.parametrize("body", [
    {"email": "hal@dimagi-ai.com", "refresh_token": "x"},   # no client field
    {"email": "hal@dimagi-ai.com", "client": "", "refresh_token": "x"},
    "not json at all",
    "",
])
def test_anything_unreadable_falls_back_to_the_table(tmp_path, body):
    """Never empty: an empty client silently points gog at the wrong OAuth app,
    which is the whole failure class this exists to close."""
    assert _run(_tok(tmp_path, body), "hal") == "canopy"


def test_a_missing_file_falls_back_rather_than_failing(tmp_path):
    """First boot: no token has been fetched yet."""
    assert _run(str(tmp_path / "nope.json"), "ace") == "ace"


def test_the_fallback_is_the_table_not_the_agents_declaration():
    """Guards the 2026-09-05 regression from coming back. config/agent.json's
    gog_client is intent; the token is fact. Nothing here may read that file."""
    fn = _fn("token_client")
    assert "agent.json" not in fn, "token_client must not consult the agent's declaration"
    assert "gog_client" not in fn
    assert "GOG_CLIENT[" in fn, "the hardcoded table is the fallback"
