"""`inject_agent_env` records whether an agent's secrets materialized — and, above
all, does not kill the bootstrap while doing it.

2026-09-22: the first version assigned `ENV_OK[$slug]=1` inside a function
where ENV_OK was undeclared. bash made it an indexed array, evaluated `ace`
arithmetically, and died under `set -u` — stopping the whole bootstrap at the
first agent. The tests below run the function with NOTHING pre-declared, which is
exactly the condition that hid it: a test that declares the arrays first would
have passed.
"""
from __future__ import annotations

import shlex
import subprocess

import pytest

from test_readiness_is_observed import BASH, _fn

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no bash with associative arrays (macOS ships 3.2; `brew install bash`)",
)


def _run(tmp_path, *, op_exit: int, op_stderr: str = "", tpl: bool = True,
         token: str = "AGENT-KEY") -> str:
    stub = tmp_path / "bin"
    stub.mkdir()
    op = stub / "op"
    # A real `op inject` writes the -o file on success; the stub does too.
    op.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "OP_SERVICE_ACCOUNT_TOKEN=%s\\n" "${{OP_SERVICE_ACCOUNT_TOKEN:-<unset>}}" >> {tmp_path}/op.env\n'
        
        f"printf %s {shlex.quote(op_stderr)} >&2\n"
        f'[ {op_exit} -eq 0 ] && for a; do [ "$prev" = -o ] && echo K=v > "$a"; prev="$a"; done\n'
        f"exit {op_exit}\n"
    )
    op.chmod(0o755)
    clone = tmp_path / "ace"
    clone.mkdir()
    if tpl:
        (clone / ".env.tpl").write_text("K=op://<vault>/<item>/<field>\n")
    script = f"""
set -euo pipefail
export PATH="{stub}:/usr/bin:/bin" HOME="{tmp_path}"
ok()   {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
{_fn("mark")}
{_fn("detail_join")}
{_fn("inject_agent_env")}
inject_agent_env ace "{clone}" "Agent-Ace" "{token}"
echo "SURVIVED"
declare -gA ENV_OK BOOTSTRAP_DETAIL
echo "ENV_OK=${{ENV_OK[ace]-unset}}"
echo "DETAIL=${{BOOTSTRAP_DETAIL[ace]:-}}"
"""
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60).stdout


def test_a_successful_inject_is_recorded_and_the_bootstrap_carries_on(tmp_path):
    out = _run(tmp_path, op_exit=0)
    assert "SURVIVED" in out
    assert "ENV_OK=1" in out
    assert (tmp_path / ".ace" / ".env").read_text() == "K=v\n"


def test_a_failed_inject_says_why_and_the_bootstrap_carries_on(tmp_path):
    out = _run(tmp_path, op_exit=1,
               op_stderr='[ERROR] could not resolve "op://Agent-Hal/canopy-pat/credential": not found')
    assert "SURVIVED" in out
    assert "ENV_OK=0" in out
    assert 'op inject from Agent-Ace failed: [ERROR] could not resolve "op://Agent-Hal/canopy-pat/credential"' in out


def test_no_template_is_not_a_failure(tmp_path):
    out = _run(tmp_path, op_exit=1, tpl=False)
    assert "SURVIVED" in out
    assert "ENV_OK=unset" in out


def test_an_unregistered_agent_injects_nothing_and_names_the_remedy(tmp_path):
    """No vault/key in canopy-web is NOT CONFIGURED, not "use whatever key is
    lying around". The old fallback (a derived vault name plus the box-wide key)
    made an unregistered agent look provisioned."""
    out = _run(tmp_path, op_exit=0, token="")
    assert "SURVIVED" in out
    assert "ENV_OK=0" in out
    assert "PUT /api/agents/ace/vault" in out
    assert not (tmp_path / ".ace" / ".env").exists()   # nothing was read or written


def test_the_inject_runs_under_the_agents_own_key(tmp_path):
    out = _run(tmp_path, op_exit=0)
    assert "OP_SERVICE_ACCOUNT_TOKEN=AGENT-KEY" in (tmp_path / "op.env").read_text()
    assert "ENV_OK=1" in out
