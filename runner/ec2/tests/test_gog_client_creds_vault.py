"""Which vault holds a gog OAuth client's id+secret depends on the CLIENT.

gog refreshes a token by presenting the client_id+client_secret of the app that
minted it, so this file is what makes an imported token usable rather than merely
present. Before 2026-09-06 the choice was a two-way `[[ $client == canopy ]]`
inline in bootstrap_one_agent, keyed off the FALLBACK table — which is computed
before any token has been read.

That is fine while every token's client is in the table. It stops being fine the
moment canopy-web's browser mint exists: those tokens are minted by canopy-web's
WEB client (the only kind Google permits an https redirect on — a Desktop client
like `canopy` is refused outright), so they declare `canopy-web`, and the client
file materialized from the table names a DIFFERENT app. The token would import
cleanly and then fail every refresh, with a plausible-looking credentials file
sitting next to it.
"""
from __future__ import annotations

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


def _op_ref(client: str, agent_vault: str, tmp: pathlib.Path) -> str:
    """Run ensure_client_creds with `op` and gog_config_dir stubbed, and report the
    op:// reference it reached for. Stubbing `op` is the point: the decision under
    test is WHICH secret is fetched, and that is observable without a vault."""
    script = f"""
set -uo pipefail
ok()   {{ :; }}
warn() {{ :; }}
gog_config_dir() {{ printf '%s\\n' "{tmp}/gogcli"; }}
op() {{ printf '%s\\n' "$2" >>"{tmp}/refs"; return 1; }}
command() {{ return 0; }}   # pretend `op` is installed
{_fn("ensure_client_creds")}
ensure_client_creds "{client}" "{agent_vault}" someslug
"""
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    refs = (tmp / "refs")
    return refs.read_text().strip() if refs.exists() else ""


def test_the_shared_desktop_client_comes_from_the_shared_vault(tmp_path):
    assert _op_ref("canopy", "Agent-Eva", tmp_path) == \
        "op://Canopy-Shared/gog-oauth-client/credential"


def test_the_web_client_is_a_DIFFERENT_item_in_the_shared_vault(tmp_path):
    """Not a variant of the same secret — a different OAuth app entirely, with a
    different client_id. Reusing `gog-oauth-client` for it would overwrite the
    Desktop client that eva/ada/hal's live tokens refresh with."""
    assert _op_ref("canopy-web", "Agent-Ace", tmp_path) == \
        "op://Canopy-Shared/gog-oauth-client-web/credential"


def test_an_agent_with_its_own_client_reads_its_own_vault(tmp_path):
    assert _op_ref("echo", "Agent-Echo", tmp_path) == \
        "op://Agent-Echo/gog-oauth-client/credential"


def test_each_client_gets_its_own_file_so_two_can_coexist(tmp_path):
    """A box runs agents on several clients at once. One shared filename would
    make the last bootstrap win and silently break the others."""
    names = set()
    for client in ("canopy", "canopy-web", "echo"):
        script = f"""
set -uo pipefail
ok() {{ :; }}; warn() {{ :; }}
gog_config_dir() {{ printf '%s\\n' "{tmp_path}/gogcli"; }}
op() {{ printf '%s' '{{"client_id":"x","client_secret":"y"}}'; }}
{_fn("ensure_client_creds")}
ensure_client_creds "{client}" Agent-Echo someslug
"""
        subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    names = {p.name for p in (tmp_path / "gogcli").glob("credentials-*.json")}
    assert names == {
        "credentials-canopy.json", "credentials-canopy-web.json", "credentials-echo.json"
    }


def test_a_vault_miss_leaves_no_half_written_file(tmp_path):
    """An empty or partial credentials file is worse than none: gog would read it
    and fail with a parse error rather than the missing-credential message that
    names the fix."""
    script = f"""
set -uo pipefail
ok() {{ :; }}; warn() {{ :; }}
gog_config_dir() {{ printf '%s\\n' "{tmp_path}/gogcli"; }}
op() {{ return 1; }}
{_fn("ensure_client_creds")}
ensure_client_creds canopy-web Agent-Ace someslug
"""
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert not (tmp_path / "gogcli" / "credentials-canopy-web.json").exists()


def test_the_token_declared_client_is_materialized_after_import():
    """The regression this whole file guards. bootstrap_one_agent must call
    ensure_client_creds a SECOND time with the client the token declared —
    the first call can only have used the fallback table."""
    body = _fn("bootstrap_one_agent")
    assert body.count("ensure_client_creds") >= 2, \
        "a token declaring an unmapped client would import and never refresh"
    after_token = body.split('token_client "$tokfile"', 1)[1]
    assert "ensure_client_creds" in after_token
