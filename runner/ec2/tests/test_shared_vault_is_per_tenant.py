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

So canopy-web serves both halves per workspace, and both are blank-safe — a
deployment that configures neither behaves exactly as it did before.
"""
from __future__ import annotations

import pathlib
import shlex
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"


def _fn(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


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
export OP_SERVICE_ACCOUNT_TOKEN={shlex.quote(agent_token)}
DEFAULT_SHARED_VAULT="Canopy-Shared"
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
gog_config_dir() {{ printf '%s\\n' "{gog_dir}"; }}
{_fn("ensure_client_creds")}
ensure_client_creds {shlex.quote(client)} "Agent-Ace" "ace" {shlex.quote(shared_vault)} {shlex.quote(shared_token)}
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60).stdout
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


def test_an_unconfigured_tenant_behaves_exactly_as_before(tmp_path):
    """Blank-safe: no shared vault and no shared key falls back to the historical
    constant and the key already in the environment. This is what makes the
    change additive rather than a flag day across every deployment."""
    _, calls = _run(tmp_path, "canopy-web")
    assert "op://Canopy-Shared/gog-oauth-client-web/credential" in calls[0]
    assert calls[0].endswith("|AGENT-KEY")


def test_a_vault_without_a_key_still_falls_back_to_the_agent_key(tmp_path):
    """The half-configured tenant: a shared vault is named but no key for it.
    The read is then attempted under the per-agent key — which is exactly the
    combination that cannot work — so the wiring must be visible in the call
    rather than inferred, and the failure path (below) has to name it."""
    _, calls = _run(tmp_path, "canopy-web", shared_vault="Acme-Shared")
    assert "op://Acme-Shared/gog-oauth-client-web/credential" in calls[0]
    assert calls[0].endswith("|AGENT-KEY")


def test_the_no_shared_key_warning_names_the_remedy(tmp_path):
    """Same case, but with op failing — which is how it presented in production."""
    gog_dir = tmp_path / "gogcli"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    op = stub_dir / "op"
    op.write_text('#!/usr/bin/env bash\nprintf %s "denied" >&2\nexit 1\n')
    op.chmod(0o755)
    script = f"""
set -uo pipefail
export PATH="{stub_dir}:/usr/bin:/bin"
export OP_SERVICE_ACCOUNT_TOKEN="AGENT-KEY"
DEFAULT_SHARED_VAULT="Canopy-Shared"
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
gog_config_dir() {{ printf '%s\\n' "{gog_dir}"; }}
{_fn("ensure_client_creds")}
ensure_client_creds "canopy-web" "Agent-Ace" "ace" "" ""
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60).stdout
    assert "denied" in out                      # op's own reason still surfaces
    assert "NO shared-vault key" in out
    assert "shared_op_vault" in out             # names where to fix it
