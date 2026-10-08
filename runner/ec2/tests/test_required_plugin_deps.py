"""A required plugin's clone gets its node deps and its key files (canopy-web#1237).

chrome-sales was cloned into ~/.claude/plugins/agent-deps/chrome-sales and
installed as a directory-source plugin with no `npm install` there, so its MCP
servers died at import and Claude Code reported only CONNECTION_CLOSED. A plugin
already installed was skipped outright, so the next bootstrap never repaired it.
Its gdrive server also reads `<plugin-root>/.gws-sa-key.json`, which nothing on the
box ever staged.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = HERE / "bootstrap_agents.sh"
sys.path.insert(0, str(HERE))

from plugin_secrets import rows  # noqa: E402

SECRETS_YAML = """\
secrets:
  - name: gws-sa-key
    op: "op://AI-Agents/chrome-sales GWS SA/key.json"
    target: "{repo}/.gws-sa-key.json"
    mode: "0600"
  - name: sf-creds
    op: "op://AI-Agents/chrome-sales Salesforce MCP/sf-creds.json"
    target: "{repo}/.sf-creds.json"
    mode: "0600"
    optional: true
"""


def _extract(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _exe(path: pathlib.Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _box(tmp: pathlib.Path, *, installed: bool = False, vaults: dict | None = None):
    """A fake box: claude, npm, op and timeout on PATH; a plugin 'upstream' to clone."""
    bindir = tmp / "bin"
    bindir.mkdir()
    (tmp / "installed").write_text("chrome-sales@chrome-sales\n" if installed else "")
    _exe(bindir / "claude", f"""
case "$1 $2" in
  "plugin list") cat "{tmp}/installed" ;;
  "plugin install") echo "$3" >> "{tmp}/installed" ;;
esac
exit 0
""")
    _exe(bindir / "npm", f'echo "$PWD $*" >> "{tmp}/npm.log"\nmkdir -p node_modules\n'
                         'cp package-lock.json node_modules/.package-lock.json 2>/dev/null\nexit 0\n')
    _exe(bindir / "timeout", 'shift\nexec "$@"\n')
    # op read op://<vault>/<item>/<field> -> the file vaults/<vault>/<item>/<field>
    _exe(bindir / "op", f"""
ref="${{2#op://}}"
echo "$OP_SERVICE_ACCOUNT_TOKEN $2" >> "{tmp}/op.log"
f="{tmp}/vaults/$ref"
[ -f "$f" ] || {{ echo "item not found" >&2; exit 1; }}
cat "$f"
""")
    for path, value in (vaults or {}).items():
        p = tmp / "vaults" / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
    upstream = tmp / "upstream"
    (upstream / "config").mkdir(parents=True)
    (upstream / "package.json").write_text("{}")
    (upstream / "package-lock.json").write_text("{}")
    (upstream / "config" / "secrets.yaml").write_text(SECRETS_YAML)
    agent = tmp / "eva"
    (agent / "config").mkdir(parents=True)
    (agent / "config" / "agent.json").write_text(json.dumps({"required_plugins": [
        {"name": "chrome-sales", "marketplace": "dimagi-internal/chrome-sales"}]}))
    return bindir, agent


def _run(tmp: pathlib.Path, bindir: pathlib.Path, agent: pathlib.Path,
         creds: tuple[str, str, str, str] = ("", "", "", "")) -> str:
    deps = tmp / "deps"
    script = "\n".join([
        'log() { echo "LOG: $*"; }; ok() { echo "OK: $*"; }; warn() { echo "WARN: $*"; }',
        'mark() { echo "MARK: $1 $2 $3"; }; detail_join() { echo "$2"; }',
        # clone = copy the upstream; pull = record it
        f'clone_or_pull() {{ if [ -d "$2/.git" ]; then echo pull >> "{tmp}/git.log"; '
        f'else mkdir -p "$2/.git" && cp -R "{tmp}/upstream/." "$2/" && echo clone >> "{tmp}/git.log"; fi; }}',
        _extract("ensure_clone_node_deps"),
        _extract("stage_plugin_secrets"),
        _extract("install_required_plugins"),
        f'install_required_plugins eva "{agent}" ' + " ".join(f'"{c}"' for c in creds),
    ])
    env = {**os.environ,
           "PATH": f"{bindir}:{pathlib.Path(sys.executable).parent}:{os.environ['PATH']}",
           "PLUGIN_DEPS_ROOT": str(deps), "SCRIPT_DIR": str(HERE)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _log(tmp: pathlib.Path, name: str) -> list[str]:
    f = tmp / name
    return f.read_text().splitlines() if f.exists() else []


def test_a_fresh_install_gets_node_deps_in_the_plugin_clone(tmp_path):
    bindir, agent = _box(tmp_path)
    out = _run(tmp_path, bindir, agent)
    clone = tmp_path / "deps" / "chrome-sales"
    assert _log(tmp_path, "npm.log") == [f"{clone} ci --silent"]
    assert (clone / "node_modules").is_dir()
    assert "OK: eva: installed required plugin chrome-sales@chrome-sales" in out


def test_an_installed_plugin_with_no_deps_is_repaired_on_the_next_pass(tmp_path):
    """The cloud-ec2-2 state: installed, cloned, no node_modules. It used to be
    skipped as 'already installed' forever."""
    bindir, agent = _box(tmp_path, installed=True)
    clone = tmp_path / "deps" / "chrome-sales"
    (clone / ".git").mkdir(parents=True)
    for f in ("package.json", "package-lock.json"):
        (clone / f).write_text("{}")
    out = _run(tmp_path, bindir, agent)
    assert _log(tmp_path, "git.log") == ["pull"]
    assert _log(tmp_path, "npm.log") == [f"{clone} ci --silent"]
    assert "clone refreshed" in out
    assert "chrome-sales@chrome-sales" in (tmp_path / "installed").read_text()


def test_a_newer_lockfile_after_a_pull_reinstalls(tmp_path):
    bindir, agent = _box(tmp_path, installed=True)
    clone = tmp_path / "deps" / "chrome-sales"
    (clone / ".git").mkdir(parents=True)
    (clone / "node_modules").mkdir()
    (clone / "package.json").write_text("{}")
    (clone / "node_modules" / ".package-lock.json").write_text("{}")
    os.utime(clone / "node_modules" / ".package-lock.json", (1_000_000, 1_000_000))
    (clone / "package-lock.json").write_text('{"new": 1}')
    _run(tmp_path, bindir, agent)
    assert _log(tmp_path, "npm.log") == [f"{clone} ci --silent"]


def test_current_deps_are_not_reinstalled(tmp_path):
    bindir, agent = _box(tmp_path, installed=True)
    clone = tmp_path / "deps" / "chrome-sales"
    (clone / ".git").mkdir(parents=True)
    (clone / "node_modules").mkdir()
    (clone / "package.json").write_text("{}")
    (clone / "package-lock.json").write_text("{}")
    os.utime(clone / "package-lock.json", (1_000_000, 1_000_000))
    (clone / "node_modules" / ".package-lock.json").write_text("{}")
    _run(tmp_path, bindir, agent)
    assert _log(tmp_path, "npm.log") == []


def test_a_plugin_installed_some_other_way_is_left_alone(tmp_path):
    bindir, agent = _box(tmp_path, installed=True)
    out = _run(tmp_path, bindir, agent)
    assert _log(tmp_path, "git.log") == [] and _log(tmp_path, "npm.log") == []
    assert "already installed" in out


def test_the_gws_key_is_staged_from_the_shared_vault_with_the_shared_key(tmp_path):
    bindir, agent = _box(tmp_path, vaults={"Canopy-Shared/chrome-sales GWS SA/key.json": '{"k": 1}'})
    out = _run(tmp_path, bindir, agent, ("Agent-Eva", "AGENTKEY", "Canopy-Shared", "SHAREDKEY"))
    key = tmp_path / "deps" / "chrome-sales" / ".gws-sa-key.json"
    assert json.loads(key.read_text()) == {"k": 1}
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    # each vault read with ITS key — the agent's first, then the tenant's
    assert _log(tmp_path, "op.log") == [
        "AGENTKEY op://Agent-Eva/chrome-sales GWS SA/key.json",
        "SHAREDKEY op://Canopy-Shared/chrome-sales GWS SA/key.json",
    ]
    assert "staged chrome-sales secret 'gws-sa-key' from Canopy-Shared" in out


def test_the_agents_own_vault_wins(tmp_path):
    bindir, agent = _box(tmp_path, vaults={
        "Agent-Eva/chrome-sales GWS SA/key.json": "mine",
        "Canopy-Shared/chrome-sales GWS SA/key.json": "shared"})
    _run(tmp_path, bindir, agent, ("Agent-Eva", "AGENTKEY", "Canopy-Shared", "SHAREDKEY"))
    assert (tmp_path / "deps" / "chrome-sales" / ".gws-sa-key.json").read_text() == "mine"


def test_salesforce_creds_are_never_staged_box_wide(tmp_path):
    """Salesforce is a delegated identity staged per agent per turn (#1291); a
    plugin-root .sf-creds.json would be a second, unowned one."""
    bindir, agent = _box(tmp_path, vaults={
        "Canopy-Shared/chrome-sales Salesforce MCP/sf-creds.json": "{}"})
    _run(tmp_path, bindir, agent, ("", "", "Canopy-Shared", "SHAREDKEY"))
    assert not (tmp_path / "deps" / "chrome-sales" / ".sf-creds.json").exists()
    assert not any("Salesforce" in line for line in _log(tmp_path, "op.log"))


def test_a_missing_secret_warns_naming_the_item_and_still_installs(tmp_path):
    bindir, agent = _box(tmp_path)
    out = _run(tmp_path, bindir, agent, ("Agent-Eva", "AGENTKEY", "", ""))
    assert "share 1Password item 'chrome-sales GWS SA'" in out
    assert "MARK: BOOTSTRAP_DETAIL eva" in out
    assert "installed required plugin chrome-sales@chrome-sales" in out
    assert not list((tmp_path / "deps" / "chrome-sales").glob(".gws-sa-key.json*"))


def test_an_already_staged_secret_is_not_read_again(tmp_path):
    bindir, agent = _box(tmp_path, installed=True)
    clone = tmp_path / "deps" / "chrome-sales"
    (clone / ".git").mkdir(parents=True)
    (clone / "config").mkdir()
    (clone / "config" / "secrets.yaml").write_text(SECRETS_YAML)
    (clone / ".gws-sa-key.json").write_text("present")
    _run(tmp_path, bindir, agent, ("Agent-Eva", "AGENTKEY", "", ""))
    assert _log(tmp_path, "op.log") == []


def test_secrets_rows_shape():
    import yaml
    assert rows(yaml.safe_load(SECRETS_YAML)) == [
        ("gws-sa-key", "chrome-sales GWS SA/key.json", ".gws-sa-key.json", "0600")]


def test_secrets_rows_refuse_targets_outside_the_clone():
    assert rows({"secrets": [
        {"name": "a", "op": "op://V/i/f", "target": "~/.x"},
        {"name": "b", "op": "op://V/i/f", "target": "{repo}/../escape"},
        {"name": "c", "op": "op://V/i/f", "target": "{repo}//abs"},
        {"name": "d", "op": "not-a-ref", "target": "{repo}/x"},
    ]}) == []
    assert rows(None) == [] and rows({"secrets": "nope"}) == []


def test_bootstrap_passes_the_agents_vaults_through():
    body = _extract("bootstrap_one_agent")
    assert ('install_required_plugins "$slug" "$dest" "$vault" "$op_token" '
            '"$shared_vault" "$shared_token"') in body
