"""A gog token can arrive from two stores; the box must not prefer the stale one.

    op://<vault>/gog-token/credential   minted at a terminal
    canopy-web /credentials/resolve     minted in a browser

canopy-web CANNOT write back to the vault — its service account is read-only by
design — so a browser mint lands in canopy-web only. A box reading the vault
alone keeps the old token forever.

Measured 2026-09-07 and the reason this exists: a token minted in canopy-web at
13:34Z was invisible to cloud-ec2-1, which still held one created 2026-05-01
under a client whose mailbox had been dead for four months. `gog auth list` on
the box showed `ace@dimagi-ai.com / client=ace / 2026-05-01T21:23:22Z` while
canopy-web reported the credential freshly set.
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


# The per-agent pass is spread over several functions — bootstrap_one_agent
# delegates the gmail half to refresh_gmail_token and the verdict to
# verify_mailbox, so that the credentials-only pass (update_runner.sh's timer)
# runs exactly the same code rather than a second copy of it.
#
# These assertions are about the PASS, not about which function happens to hold
# a line today, so they read the concatenation. Pinning them to one function
# name made a pure refactor look like a regression.
def _agent_pass() -> str:
    return "\n".join(_fn(n) for n in
                     ("bootstrap_one_agent", "refresh_gmail_token", "verify_mailbox"))


def _created_at(tmp_path, body) -> int:
    f = tmp_path / "tok.json"
    f.write_text(body if isinstance(body, str) else json.dumps(body))
    script = f'set -uo pipefail\n{_fn("token_created_at")}\ntoken_created_at "{f}"'
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return int((res.stdout or "0").strip() or 0)


def test_a_real_timestamp_parses(tmp_path):
    """eva's live token, verbatim — and the expected value is DERIVED, not typed.

    A hand-computed epoch here was wrong by exactly 86400 on the first run: it
    added nothing the ordering tests don't already cover, and gave CI a magic
    number to disagree with. Deriving it still tests something real, because the
    parsing under test happens in bash + a subprocess, not here."""
    import calendar
    import time

    stamp = "2026-07-24T04:09:54Z"
    expected = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    assert _created_at(tmp_path, {"created_at": stamp}) == expected


def test_the_may_token_is_older_than_the_september_mint(tmp_path):
    """The exact pair that was live on the box."""
    stale = _created_at(tmp_path, {"created_at": "2026-05-01T21:23:22Z"})
    fresh = _created_at(tmp_path, {"created_at": "2026-09-07T13:34:22Z"})
    assert 0 < stale < fresh


def test_a_missing_or_junk_token_sorts_oldest_rather_than_erroring(tmp_path):
    """0 must lose to any real timestamp — an unparseable token that sorted
    NEWEST would silently beat a good one, which is the failure this whole
    comparison exists to prevent."""
    assert _created_at(tmp_path, "") == 0
    assert _created_at(tmp_path, "not json at all") == 0
    assert _created_at(tmp_path, {"no_created_at": True}) == 0
    assert _created_at(tmp_path, {"created_at": "24 July 2026"}) == 0
    assert 0 < _created_at(tmp_path, {"created_at": "2026-05-01T21:23:22Z"})


def test_a_missing_file_is_zero_not_a_failure(tmp_path):
    script = f'set -uo pipefail\n{_fn("token_created_at")}\ntoken_created_at "{tmp_path}/nope.json"'
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and res.stdout.strip() == "0"


def test_the_import_compares_both_stores_before_choosing():
    """Guards the regression directly: reading only the vault is what stranded a
    freshly minted token."""
    body = _agent_pass()
    assert "fetch_canopy_web_token" in body, "canopy-web's copy must be considered"
    assert "token_created_at" in body, "the choice must be by age, not by source"
    # Not "canopy-web always wins" — that would lose a fresh vault rotation to a
    # stale browser mint. The comparison is the point.
    assert "wage > vage" in body


def test_canopy_web_is_read_through_the_gated_route():
    body = _fn("fetch_canopy_web_token")
    assert "/credentials/resolve" in body
    assert "Authorization: Bearer" in body


def test_a_canopy_web_miss_leaves_an_empty_file_not_a_stale_one(tmp_path):
    """An agent nobody has minted is the normal case. A leftover file from a
    previous agent's fetch would import the WRONG mailbox's token."""
    out = tmp_path / "web.json"
    out.write_text('{"created_at":"2099-01-01T00:00:00Z"}')  # would win if not cleared
    script = f'''
set -uo pipefail
CANOPY_BASE_URL=""; CANOPY_TOKEN=""
{_fn("fetch_canopy_web_token")}
fetch_canopy_web_token ace "{out}"
'''
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.read_text() == ""


def test_it_degrades_toward_the_vault_when_ages_are_unknowable():
    """Discovered by running the comparison in an image without python3: every
    token scored 0, so `wage > vage` is false and the vault copy wins — which is
    exactly today's behaviour. That direction is the safe one (a stale token
    stays put); the reverse would import canopy-web's copy over a good vault
    rotation. Stated in the script so it stays deliberate rather than lucky."""
    body = _agent_pass()
    assert "wage > vage" in body, "the vault must be the else-branch, not canopy-web"
