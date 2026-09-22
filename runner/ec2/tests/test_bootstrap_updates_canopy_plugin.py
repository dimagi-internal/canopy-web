"""A refresh must actually move the canopy plugin forward.

Step 4 used to ADD the canopy marketplace once and never pull it, so the clone
advanced only when a session's async SessionStart hook happened to, and a
refresh re-ran bootstrap against a stale canopy (hal's drill on cloud-ec2-1,
2026-09-22: plugin 0.2.509 with 0.2.511 on origin).
"""
from __future__ import annotations

import subprocess

import pytest

from test_readiness_is_observed import BASH, _fn

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash 4+")


def _step4(tmp_path, *, installed: bool) -> list[str]:
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "calls"
    listing = "canopy@canopy\\n" if installed else ""
    (stub / "claude").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {log}\n'
        'case "$*" in\n'
        '  "plugin marketplace list") echo "  canopy";;\n'
        f'  "plugin list") printf "{listing}";;\n'
        '  "--version") echo "2.1.278 (Claude Code)";;\n'
        "esac\n"
    )
    (stub / "claude").chmod(0o755)
    script = f"""
set -uo pipefail
export PATH="{stub}:/usr/bin:/bin"
CANOPY_PLUGIN_URL=https://example.invalid/canopy.git
ok() {{ :; }}; warn() {{ :; }}; log() {{ :; }}
ensure_claude_current() {{ :; }}
reinstall_cli_from_marketplace_clone() {{ echo "cli-sync" >> {log}; }}
{_fn("step4_claude_plugins")}
step4_claude_plugins
"""
    subprocess.run([BASH, "-c", script], check=True, timeout=60)
    return log.read_text().splitlines()


def test_an_installed_plugin_is_updated_before_the_cli_syncs_to_it(tmp_path):
    calls = _step4(tmp_path, installed=True)
    assert "plugin marketplace update canopy" in calls
    assert "plugin update canopy@canopy" in calls
    # The CLI matches the clone, so the clone must move first.
    assert calls.index("plugin marketplace update canopy") < calls.index("cli-sync")
    assert calls.index("plugin update canopy@canopy") < calls.index("cli-sync")


def test_a_missing_plugin_is_installed_not_updated(tmp_path):
    calls = _step4(tmp_path, installed=False)
    assert "plugin install canopy@canopy" in calls
    assert "plugin update canopy@canopy" not in calls
