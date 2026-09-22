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


def _run(tmp_path, *, op_exit: int, op_stderr: str = "", tpl: bool = True) -> str:
    stub = tmp_path / "bin"
    stub.mkdir()
    op = stub / "op"
    # A real `op inject` writes the -o file on success; the stub does too.
    op.write_text(
        "#!/usr/bin/env bash\n"
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
{_fn("inject_agent_env")}
inject_agent_env ace "{clone}"
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
    assert 'op inject failed: [ERROR] could not resolve "op://Agent-Hal/canopy-pat/credential"' in out


def test_no_template_is_not_a_failure(tmp_path):
    out = _run(tmp_path, op_exit=1, tpl=False)
    assert "SURVIVED" in out
    assert "ENV_OK=unset" in out
