"""Readiness is what the box OBSERVED, never what its configuration implies.

The 2026-09-07 outage in one line: canopy-web held a valid gog-token for ACE and
the credentials screen showed it set, while every gmail call on the box failed —
because the OAuth client id+secret the token is useless without had not
materialized. Two true facts, one visible.

So `verify_mailbox` decides by MAKING THE CALL, and the report carries that
verdict rather than any of the configuration that led to it. A check that reads
back what was configured would have passed on the worst day this system has had.
"""
from __future__ import annotations

import pathlib
import shlex
import shutil
import subprocess

import pytest


# `verify_mailbox` reads associative arrays, so unlike test_client_creds_says_why
# these genuinely need bash 4+. Rather than skip on macOS — which is how this
# directory shipped a test CI was the first thing to ever execute — look for a
# real bash 5 first (`brew install bash`) and only skip if the machine has none.
def _has_assoc_arrays(candidate: str | None) -> bool:
    """Probe a candidate bash. Must swallow FileNotFoundError, not just a bad
    exit code: on Linux CI /opt/homebrew/bin/bash simply does not exist, and an
    uncaught OSError here fails COLLECTION — every test in the file errors
    rather than skipping, which is a worse outcome than the skip it replaced."""
    if not candidate:
        return False
    try:
        return subprocess.run(
            [candidate, "-c", "declare -A _t=( [k]=v )"], capture_output=True
        ).returncode == 0
    except OSError:
        return False


# `bash` from PATH first — that is what CI has (Linux ships 5.x), and on a Mac
# with `brew install bash` it is the brew one. The explicit paths are the
# fallback for a Mac whose PATH still leads to /bin/bash 3.2.
BASH = next(
    (b for b in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash")
     if _has_assoc_arrays(b)),
    None,
)
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no bash with associative arrays (macOS ships 3.2; `brew install bash`)",
)

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"


def _fn(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _verify(tmp_path, *, gog_exit=0, gog_stderr=""):
    """Run verify_mailbox against a stubbed `gog`, returning (log, MAILBOX_OK)."""
    stub = tmp_path / "bin"
    stub.mkdir()
    g = stub / "gog"
    g.write_text(
        "#!/usr/bin/env bash\n"
        f"printf %s {shlex.quote(gog_stderr)} >&2\n"
        f"exit {gog_exit}\n"
    )
    g.chmod(0o755)
    script = f"""
set -uo pipefail
export PATH="{stub}:/usr/bin:/bin"
declare -A MAILBOX_OK=() GOG_CLIENT_USED=() BOOTSTRAP_DETAIL=()
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
{_fn("mark")}
{_fn("verify_mailbox")}
verify_mailbox "ace" "ace@dimagi-ai.com" "canopy"
echo "MAILBOX_OK=${{MAILBOX_OK[ace]:-unset}}"
echo "DETAIL=${{BOOTSTRAP_DETAIL[ace]:-}}"
"""
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60).stdout


def test_a_working_call_is_the_only_thing_that_marks_it_live(tmp_path):
    out = _verify(tmp_path, gog_exit=0)
    assert "MAILBOX_OK=1" in out
    assert "mailbox verified live" in out


def test_the_exact_2026_09_07_failure_reports_not_live(tmp_path):
    """`No auth for gmail <mailbox>` is what a box says when the token is fine
    but its OAuth client is missing — indistinguishable from having no token,
    which is why only the call can tell them apart."""
    out = _verify(tmp_path, gog_exit=4,
                  gog_stderr="No auth for gmail ace@dimagi-ai.com.")
    assert "MAILBOX_OK=0" in out
    assert "mailbox NOT live" in out


def test_the_failure_reason_is_carried_not_discarded(tmp_path):
    """The report has to say WHY, for the same reason ensure_client_creds now
    does: a bare false sends the next reader to the box to find out."""
    out = _verify(tmp_path, gog_exit=10,
                  gog_stderr="OAuth client credentials missing (OAuth client ID JSON).")
    assert "OAuth client credentials missing" in out
    assert "DETAIL=" in out
    assert "gmail check failed" in out


def test_credentials_only_is_accepted_and_skips_the_expensive_half(tmp_path):
    """The timer runs this every tick, so it must not clone, op-inject or
    reinstall plugins under a live agent."""
    src = SCRIPT.read_text()
    assert "--credentials-only" in src
    # The gated early-return runs the cheap half and returns before the clone.
    gated = src.index("if (( CREDENTIALS_ONLY )); then\n    ensure_client_creds")
    clone = src.index('local repo_url; repo_url="$(agent_repo_url "$slug")"')
    assert gated < clone, "the credentials-only path must return before the clone"


def test_one_implementation_of_the_token_rule_not_two(tmp_path):
    """Both the full pass and the credentials-only pass call the SAME functions.
    Two copies of this rule drifting apart is the shape of most of this file's
    history, so it is asserted rather than trusted."""
    src = SCRIPT.read_text()
    assert src.count("refresh_gmail_token \"$slug\"") == 2
    assert src.count("verify_mailbox \"$slug\"") == 2
    assert src.count("refresh_gmail_token() {") == 1
    assert src.count("verify_mailbox() {") == 1


def test_the_token_declared_client_is_recorded_for_the_verify_and_the_report():
    """`gog_client` was empty on the first real report this system produced, and
    that emptiness is not cosmetic — it is the same missing fact that made the
    verify use the wrong client.

    A refresh token is minted FOR one OAuth client and works only with it. The
    GOG_CLIENT map still says `ace` while a browser mint declares `canopy-web`,
    so falling back to the map presents the token with the wrong client's
    credentials and Google answers `invalid_grant`. That surfaces as "refresh
    token expired or revoked" — which is false, and sends the next reader to
    re-mint a token that was never the problem. Seen on cloud-ec2-1 2026-09-07.
    """
    src = SCRIPT.read_text()
    after_import = src.split('upsert_account_client "$account" "$tclient"', 1)[1]
    assert 'mark GOG_CLIENT_USED "$slug"' in after_import, \
        "the client the TOKEN declared must be recorded, not left to the stale map"
    # And verify_mailbox must prefer it over the map fallback.
    vm = _fn("verify_mailbox")
    assert 'GOG_CLIENT_USED[$slug]' in vm


# ── verify_turn_client: the client the CONSUMER presents ─────────────────────
#
# The 2026-09-08 sequel. `verify_mailbox` above answers "does SOME client work",
# which is the right question for the mailbox and the wrong one for readiness.
# ACE reported `mailbox_ok: true / gog_client: canopy-web` for a day while every
# email turn aborted with `No auth for gmail ace@dimagi-ai.com`, because
# `/ace:turn` presents the client `config/agent.json` DECLARES (`canopy`) and the
# box only ever had tokens under `ace` and `canopy-web`.


def _verify_turn(tmp_path, *, declared="canopy", mailbox_ok="0", live_client="",
                 turn_exit=0, turn_stderr="", agent_json=None):
    """Run verify_turn_client against a stubbed `gog`, returning its stdout."""
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    g = stub / "gog"
    g.write_text(
        "#!/usr/bin/env bash\n"
        f"printf %s {shlex.quote(turn_stderr)} >&2\n"
        f"exit {turn_exit}\n"
    )
    g.chmod(0o755)

    root = tmp_path / "agents"
    if agent_json is not None:
        cfg = root / "ace" / "config"
        cfg.mkdir(parents=True)
        (cfg / "agent.json").write_text(agent_json)

    script = f"""
set -uo pipefail
export PATH="{stub}:/usr/bin:/bin"
AGENT_ROOT="{root}"
FLEET_GOG_CLIENT="{declared}"
declare -A MAILBOX_OK=( [ace]={mailbox_ok} )
declare -A GOG_CLIENT_USED=( [ace]={shlex.quote(live_client)} )
declare -A BOOTSTRAP_DETAIL=() TURN_CLIENT=() TURN_READY=()
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
{_fn("mark")}
{_fn("turn_client_for")}
{_fn("verify_turn_client")}
verify_turn_client "ace" "ace@dimagi-ai.com"
echo "TURN_CLIENT=${{TURN_CLIENT[ace]:-unset}}"
echo "TURN_READY=${{TURN_READY[ace]-unset}}"
echo "DETAIL=${{BOOTSTRAP_DETAIL[ace]:-}}"
"""
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60).stdout


def test_a_green_mailbox_under_the_wrong_client_is_reported_not_turn_ready(tmp_path):
    """THE regression. mailbox_ok=1 under canopy-web, turns need canopy: not ready."""
    out = _verify_turn(tmp_path, mailbox_ok="1", live_client="canopy-web",
                       turn_exit=4, turn_stderr="No auth for gmail ace@dimagi-ai.com.")
    assert "TURN_READY=0" in out
    assert "TURN_CLIENT=canopy" in out
    # The warning must name BOTH clients — otherwise "the mailbox is fine" and
    # "turns are dead" read as a contradiction instead of a diagnosis.
    assert "canopy-web" in out and "'canopy'" in out


def test_no_second_call_when_the_live_client_already_is_the_turn_client(tmp_path):
    """Common case short-circuits: verify_mailbox already proved this exact client."""
    out = _verify_turn(tmp_path, mailbox_ok="1", live_client="canopy",
                       turn_exit=99, turn_stderr="should never run")
    # turn_exit=99 would fail if a call were made — passing proves none was.
    assert "TURN_READY=1" in out
    assert "turns can read the mailbox" in out


def test_the_agents_own_declaration_wins_over_the_fleet_default(tmp_path):
    """`config/agent.json` is the source of what a turn presents."""
    out = _verify_turn(tmp_path, declared="canopy", agent_json='{"gog_client": "echo"}')
    assert "TURN_CLIENT=echo" in out


def test_falls_back_to_the_fleet_client_when_the_repo_is_absent(tmp_path):
    """A cloud box need not have the agent's repo; the fleet client is the answer."""
    out = _verify_turn(tmp_path, declared="canopy", agent_json=None)
    assert "TURN_CLIENT=canopy" in out


def test_turn_ready_stays_unset_when_gog_is_absent(tmp_path):
    """Not checked is NOT broken — the tri-state must survive to the report."""
    empty = tmp_path / "empty"
    empty.mkdir()
    script = f"""
set -uo pipefail
export PATH="{empty}"
AGENT_ROOT="{tmp_path}/nope"
FLEET_GOG_CLIENT="canopy"
declare -A MAILBOX_OK=() GOG_CLIENT_USED=() BOOTSTRAP_DETAIL=() TURN_CLIENT=() TURN_READY=()
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
{_fn("mark")}
{_fn("turn_client_for")}
{_fn("verify_turn_client")}
verify_turn_client "ace" "ace@dimagi-ai.com"
echo "TURN_READY=${{TURN_READY[ace]-unset}}"
"""
    out = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60).stdout
    assert "TURN_READY=unset" in out
