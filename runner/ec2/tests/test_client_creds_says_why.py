"""When `op read` fails, the box must say WHY — not just that it failed.

A gog token is useless without its OAuth client's id+secret, and those two
travel separately: the token arrives from the vault or from canopy-web, while
`credentials-<client>.json` is always an `op read`. So a silent failure in
ensure_client_creds leaves a perfectly good token that cannot authorize
anything, and every gmail call dies on `No auth for gmail <mailbox>`.

Measured 2026-09-07 on cloud-ec2-1, and the reason this file exists. In ONE
bootstrap pass, seconds apart:

    14:08:38  OK: ace: using canopy-web's token (newer: 1788788062 > 1777670602)
    14:08:38  OK: ace: gmail token imported
    14:08:39  WARN: ace: op read op://Canopy-Shared/gog-oauth-client-web/credential failed

op://Agent-Ace reads succeeded throughout that same pass — `op inject`, the
gog-token read, and credentials-ace.json were all written. Only the
Canopy-Shared arm failed, because bootstrap_one_agent exports a per-agent
scoped key and a per-agent key cannot read the shared vault.

None of that was visible. The WARN named the path and swallowed op's reason
(`2>/dev/null`), so the failure was indistinguishable from a missing item, a
revoked key, a throttle, or a 1Password outage — and recovering the one line op
had already written took five diagnostic round trips to the box.

The token import twenty lines below already carries this exact lesson in a
comment ("Swallowing it here is what hid a fleet-wide failure for weeks").
These tests hold ensure_client_creds to the same standard.
"""
from __future__ import annotations

import pathlib
import shlex
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"

# Deliberately NOT skipped on bash 3.2, unlike its sibling in this directory.
# That skip exists because token_created_at's caller needs associative arrays;
# ensure_client_creds uses none, so these run on a macOS laptop as well as on
# the box. The sibling's tests reached CI unexecuted by anyone — the epoch bug
# CI caught had been sitting in a green local run. Fewer skips, fewer of those.


def _fn(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _run(tmp_path, client, *, op_stderr=None, op_present=True, op_stdout=""):
    """Drive ensure_client_creds with a stubbed `op`, and return its log output.

    The stub writes op_stdout to the credential file and op_stderr to stderr,
    exiting non-zero when op_stderr is set — the shape of a real failed read.
    """
    gog_dir = tmp_path / "gogcli"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    if op_present:
        op = stub_dir / "op"
        if op_stderr is None:
            op.write_text(f'#!/usr/bin/env bash\nprintf %s {shlex.quote(op_stdout)}\n')
        else:
            op.write_text(f'#!/usr/bin/env bash\nprintf %s {shlex.quote(op_stderr)} >&2\nexit 1\n')
        op.chmod(0o755)

    # A MINIMAL PATH, not an append to the caller's. `op` is a normal laptop
    # install (/opt/homebrew/bin/op), so inheriting PATH would let the
    # absent-op case find the REAL binary — the test would then exercise
    # 1Password instead of the branch it names, and could make a live request.
    script = f"""
set -uo pipefail
export PATH="{stub_dir}:/usr/bin:/bin"
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
gog_config_dir() {{ printf '%s\\n' "{gog_dir}"; }}
{_fn("ensure_client_creds")}
ensure_client_creds "{client}" "Agent-Ace" "ace"
"""
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return res.stdout


REAL_OP_ERROR = (
    '[ERROR] 2026/09/07 14:08:39 could not read secret '
    '"op://Canopy-Shared/gog-oauth-client-web/credential": '
    'error initializing client: No accounts configured for use with 1Password CLI'
)


def test_the_reason_op_gave_reaches_the_log(tmp_path):
    """The whole point: op's own words, not a bare "failed"."""
    out = _run(tmp_path, "canopy-web", op_stderr=REAL_OP_ERROR)
    assert "No accounts configured for use with 1Password CLI" in out


def test_a_shared_client_failure_names_the_vault_boundary(tmp_path):
    """`canopy` and `canopy-web` read Canopy-Shared while the surrounding pass
    holds a per-agent key. That is the actual defect behind the outage, so the
    warning has to point at it rather than leaving the reader to rediscover it."""
    out = _run(tmp_path, "canopy-web", op_stderr=REAL_OP_ERROR)
    assert "Canopy-Shared" in out
    assert "per-agent" in out


def test_a_per_agent_client_does_not_claim_a_shared_vault_problem(tmp_path):
    """The `*)` arm reads the agent's OWN vault, which the scoped key CAN read.
    Blaming the shared vault there would send the next reader somewhere wrong."""
    out = _run(tmp_path, "ace", op_stderr="some other failure")
    assert "some other failure" in out
    assert "per-agent" not in out


def test_success_still_reports_success(tmp_path):
    out = _run(tmp_path, "canopy-web", op_stdout='{"installed":{"client_id":"x"}}')
    assert "OK: ace: gog client creds (canopy-web)" in out
    assert "WARN" not in out


def test_a_missing_op_is_not_silent(tmp_path):
    """It used to `return 0` with no output at all — the one branch that left no
    trace whatsoever, on a box where op is installed by cloud-init and could
    plausibly be absent on a rebuild."""
    out = _run(tmp_path, "canopy-web", op_present=False)
    assert "op unavailable" in out
