"""The shared vault is a TENANT fact the box is told, not a name it compiles in.

`ensure_client_creds` routes the two SHARED gog clients — `canopy` and
`canopy-web` — to a vault every agent in a tenant can read. That vault name was
the literal "Canopy-Shared", which is correct for exactly one tenant and
silently wrong for the second: different tenants hold different values under the
same names (Jonathan, 2026-09-07).

It also needs a DIFFERENT KEY than the rest of the pass. bootstrap_one_agent
exports a per-agent scoped OP_SERVICE_ACCOUNT_TOKEN, and a per-agent key reads
op://Agent-<Slug> and nothing else — deliberately, so a compromise is bounded to
one agent (Agent.op_vault, 2026-09-06). Using it against the shared vault is the
2026-09-07 outage: ACE imported a browser-minted token bound to `canopy-web`,
then could not read the client id+secret that token is useless without, and
every gmail call died on `No auth for gmail ace@dimagi-ai.com`.

So canopy-web serves both halves per workspace — and as of 2026-09-22 there is
no fallback under either. An unregistered vault or key means NOT CONFIGURED: the
read is refused and the report says where to register it. The old blank-safe
behaviour (a compiled-in "Canopy-Shared" and whatever key was in the
environment) made a misconfigured agent look healthy and left "which identity
read this secret" unanswerable.
"""
from __future__ import annotations

import pathlib
import shlex
import subprocess

import pytest

# The report arrays are associative, so these need bash 4+ (macOS ships 3.2).
from test_readiness_is_observed import BASH, _fn

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no bash with associative arrays (macOS ships 3.2; `brew install bash`)",
)




def _run(tmp_path, client, *, shared_vault="", shared_token="", agent_token="AGENT-KEY"):
    """Drive ensure_client_creds with an `op` stub that RECORDS what it was asked.

    The stub writes the vault path it was given and the token it saw to a log,
    so the assertions below are about the call actually made — not about a
    message describing it.
    """
    gog_dir = tmp_path / "gogcli"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "calls.log"
    op = stub_dir / "op"
    op.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s|%s\\n" "$*" "${{OP_SERVICE_ACCOUNT_TOKEN:-<unset>}}" >> {shlex.quote(str(log))}\n'
        'printf %s \'{"installed":{"client_id":"x"}}\'\n'
    )
    op.chmod(0o755)

    script = f"""
set -uo pipefail
export PATH="{stub_dir}:/usr/bin:/bin"
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
gog_config_dir() {{ printf '%s\\n' "{gog_dir}"; }}
{_fn("ensure_client_creds")}
{_fn("mark")}
{_fn("detail_join")}
declare -A CLIENT_CREDS_OK BOOTSTRAP_DETAIL
ensure_client_creds {shlex.quote(client)} "Agent-Ace" "ace" {shlex.quote(shared_vault)} {shlex.quote(shared_token)} {shlex.quote(agent_token)}
echo "CLIENT_CREDS_OK=${{CLIENT_CREDS_OK[ace]:-unset}}"
echo "DETAIL=${{BOOTSTRAP_DETAIL[ace]:-}}"
"""
    out = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60).stdout
    calls = log.read_text().strip().splitlines() if log.exists() else []
    return out, calls


def test_the_tenants_vault_is_used_not_the_compiled_default(tmp_path):
    _, calls = _run(tmp_path, "canopy-web", shared_vault="Acme-Shared", shared_token="SHARED-KEY")
    assert "op://Acme-Shared/gog-oauth-client-web/credential" in calls[0]
    assert "Canopy-Shared" not in calls[0]


def test_the_shared_read_uses_the_shared_key_not_the_agent_key(tmp_path):
    """The actual defect. The per-agent key cannot read the shared vault, and
    using it there is what broke ACE's mailbox on 2026-09-07."""
    _, calls = _run(tmp_path, "canopy-web", shared_vault="Acme-Shared", shared_token="SHARED-KEY")
    assert calls[0].endswith("|SHARED-KEY")


def test_a_per_agent_client_still_uses_the_agent_key_and_vault(tmp_path):
    """Only the two SHARED clients cross the boundary. Everything else reads the
    agent's own vault with the agent's own key, and must keep doing so."""
    _, calls = _run(tmp_path, "ace", shared_vault="Acme-Shared", shared_token="SHARED-KEY")
    assert "op://Agent-Ace/gog-oauth-client/credential" in calls[0]
    assert calls[0].endswith("|AGENT-KEY")


def test_an_unconfigured_tenant_reads_nothing_and_says_where_to_fix_it(tmp_path):
    """No fallback. A tenant with no shared vault gets no read at all — not a
    read against a compiled-in name with whatever key happened to be around."""
    out, calls = _run(tmp_path, "canopy-web")
    assert calls == []
    assert "Canopy-Shared" not in out
    assert "CLIENT_CREDS_OK=0" in out
    assert "shared vault" in out and "canopy-web" in out


def test_a_vault_without_a_key_is_not_configured(tmp_path):
    """The half-configured tenant: a shared vault named, no key for it. That
    combination cannot work, so it is refused rather than attempted under
    somebody else's key."""
    out, calls = _run(tmp_path, "canopy-web", shared_vault="Acme-Shared")
    assert calls == []
    assert "CLIENT_CREDS_OK=0" in out


def test_an_unregistered_agent_reads_nothing_for_its_own_client(tmp_path):
    out, calls = _run(tmp_path, "ace", agent_token="")
    assert calls == []
    assert "CLIENT_CREDS_OK=0" in out


def test_the_refusal_reaches_the_report_not_just_the_log(tmp_path):
    """The detail is what /agents/<slug>/readiness shows, and it has to name the
    remedy — this failure is a registration someone has to perform."""
    out, _ = _run(tmp_path, "canopy-web")
    detail = [l for l in out.splitlines() if l.startswith("DETAIL=")][0]
    assert "shared vault" in detail and ("no vault" in detail or "no key" in detail)


def test_an_unregistered_agent_never_inherits_the_tenants_vault(tmp_path):
    """The field-splitting trap, pinned.

    `agent_vault_config` returns four fields. They were TAB separated, and tab is
    IFS *whitespace*, so `read` collapses leading empties: an agent with no vault
    got the TENANT's vault and the TENANT's key in the agent slots. That is a
    cross-level fallback carrying the wrong tier's credential, and it is what
    ada/echo/hal hit on cloud-ec2-1 (2026-09-22) — "op inject from Canopy-Shared
    failed: Agent-Hal isn't a vault in this account".
    """
    script = '''
set -uo pipefail
cfg="$(printf '%s\\x1f%s\\x1f%s\\x1f%s' "" "" "Canopy-Shared" "SHARED-KEY")"
IFS=$'\\x1f' read -r vault op_token shared_vault shared_token <<<"$cfg"
echo "vault=[$vault] key=[$op_token] shared=[$shared_vault] sharedkey=[$shared_token]"
'''
    out = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30).stdout
    assert "vault=[] key=[] shared=[Canopy-Shared] sharedkey=[SHARED-KEY]" in out


def test_the_config_reader_uses_a_non_whitespace_separator(tmp_path):
    """Belt and braces on the file itself: a tab here silently reintroduces the
    collapse above, and every test that passes fields explicitly would still pass."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh").read_text()
    reader = src.split("agent_vault_config()", 1)[1].split("\n}", 1)[0]
    assert "\\x1f" in reader and "\\t" not in reader
