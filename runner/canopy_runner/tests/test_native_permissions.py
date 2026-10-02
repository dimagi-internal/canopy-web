"""The second layer: a caller's capability, restated as Claude Code permission rules
in the session worktree's `.claude/settings.json`."""
import json

import pytest

from canopy_runner import caller, cdp_control, emdash, execute, native_permissions as np

ASK = {"name": "ask", "entry": "/ace:ask --thread {thread_id}",
       "tools": ["Read", "Grep", "mcp__*canopy-web__who_is_asking"],
       "bash": ["canopy email read --repo . {thread_id}",
                "bin/ace-email --reply-all --thread-id {thread_id} --subject * --body-file {cwd}/*"],
       "read_paths": ["{cwd}/**"]}
SERVERS = {"plugin_canopy_canopy-web", "plugin_ace_ace-connect", "gbrain"}
NAMES = {"plugin_canopy_canopy-web": "plugin:canopy:canopy-web",
         "plugin_ace_ace-connect": "plugin:ace:ace-connect", "gbrain": "gbrain"}


def _perms(cap=ASK, wt="/w/cx-a-1234-xyz12", **kw):
    return np.settings_for(cap, worktree=wt, thread_id="18c9abc", servers=SERVERS, **kw)


def test_tools_the_capability_does_not_grant_are_denied_outright():
    deny = _perms()["deny"]
    for tool in ("Edit", "Write", "WebFetch", "WebSearch", "Agent", "NotebookEdit"):
        assert tool in deny
    assert "Grep" not in deny and "Bash" not in deny


def test_skill_is_never_denied_natively_because_the_entry_is_a_skill():
    assert "Skill" not in _perms({"name": "none"})["deny"]


def test_read_is_never_denied_whole_so_the_envelope_stays_readable():
    assert "Read" not in _perms({"name": "none"})["deny"]


def test_no_bash_patterns_means_no_bash():
    assert "Bash" in _perms({**ASK, "bash": []})["deny"]


def test_high_risk_programs_are_denied_unless_the_capability_starts_with_them():
    deny = _perms()["deny"]
    assert "Bash(git *)" in deny and "Bash(gh *)" in deny and "Bash(curl *)" in deny
    git_cap = {**ASK, "bash": ["git status"]}
    assert "Bash(git *)" not in _perms(git_cap)["deny"]


def test_mcp_patterns_are_normalised_to_real_servers():
    """A glob in the SERVER segment makes Claude Code skip an allow rule."""
    p = _perms()
    assert "mcp__plugin_canopy_canopy-web__who_is_asking" in p["allow"]
    assert not any(a.startswith("mcp__*") for a in p["allow"])
    assert "mcp__plugin_ace_ace-connect" in p["deny"] and "mcp__gbrain" in p["deny"]
    assert "mcp__plugin_canopy_canopy-web" not in p["deny"]


def test_a_capability_with_no_mcp_denies_every_server_including_unknown_ones():
    assert "mcp__*" in _perms({**ASK, "tools": ["Read"]})["deny"]


def test_bash_and_paths_are_allowed_exactly_with_placeholders_filled(tmp_path):
    wt = tmp_path / "cx-a-1234-xyz12"
    wt.mkdir()
    p = _perms(wt=str(wt), caller_path=str(tmp_path / "caller" / "t.json"))
    real = str(wt.resolve())
    assert "Bash(canopy email read --repo . 18c9abc)" in p["allow"]
    assert f"Bash(bin/ace-email --reply-all --thread-id 18c9abc --subject * --body-file {real}/*)" in p["allow"]
    assert f"Read(/{real}/**)" in p["allow"]
    assert any(a.startswith("Read(//") and a.endswith("t.json)") for a in p["allow"])
    assert "Read" not in p["allow"]                 # scoped, never bare
    assert p["defaultMode"] == "dontAsk"


def test_credential_stores_are_denied_to_the_path_tools():
    deny = _perms()["deny"]
    assert "Read(~/.ssh/**)" in deny and "Read(~/.canopy/profiles/**)" in deny


# ACE's live `ask` capability (2026-10-02): Write granted, Edit not, writes confined
# to `.ace-ask/` while reads cover the worktree.
ACE_ASK = {"name": "ask", "entry": "/ace:ask --thread {thread_id}",
           "tools": ["Read", "Grep", "Glob", "Write", "mcp__*canopy-web__who_is_asking"],
           "bash": ["canopy email read --repo . {thread_id}"],
           "read_paths": ["{cwd}/**"], "write_paths": ["{cwd}/.ace-ask/*"]}


def _all_rules(p):
    return p["allow"] + p["deny"]


@pytest.mark.parametrize("cap", [ASK, ACE_ASK, {**ACE_ASK, "write_paths": []},
                                 {**ACE_ASK, "tools": ["*"]}, {"name": "none"}])
def test_no_write_path_rule_is_ever_emitted(cap):
    """Claude Code ignores `Write(path)` (allow AND deny) and warns at startup; only
    `Edit(path)` scopes file writes (#1058)."""
    assert not [r for r in _all_rules(_perms(cap)) if r.startswith("Write(")]
    assert not [r for r in _all_rules(_perms(cap)) if r.startswith("NotebookEdit(")]


def test_credential_stores_are_denied_to_every_file_writing_tool_as_edit_rules():
    deny = _perms(ACE_ASK)["deny"]
    for p in np.SENSITIVE_PATHS:
        assert f"Edit({p})" in deny and f"Read({p})" in deny


def test_writes_land_only_in_write_paths_not_everywhere_reads_reach(tmp_path):
    wt = tmp_path / "cx-a-1234-xyz12"
    wt.mkdir()
    p = _perms(ACE_ASK, wt=str(wt))
    real = str(wt.resolve())
    assert f"Edit(/{real}/.ace-ask/*)" in p["allow"]
    assert f"Read(/{real}/**)" in p["allow"]
    assert f"Edit(/{real}/**)" not in p["allow"]       # bin/ace-email stays unwritable
    assert "Write" not in p["allow"] and "Write" not in p["deny"]   # scoped, never bare
    assert "Edit" in p["deny"]                         # the Edit TOOL is not granted


def test_a_write_tool_with_no_write_paths_is_denied_like_profile_guard_refuses_it():
    p = _perms({**ACE_ASK, "write_paths": []})
    assert "Write" in p["deny"]
    assert not [a for a in p["allow"] if a.startswith(("Edit(", "Write"))]


def test_a_write_path_outside_the_worktree_drops_the_conflicting_edit_denies():
    """An allow cannot carve out a deny, so a capability that writes outside the
    worktree loses the credential Edit denies — exactly as read_paths do for Read."""
    p = _perms({**ACE_ASK, "write_paths": ["~/notes/*"]})
    assert "Edit(~/notes/*)" in p["allow"]
    assert not [d for d in p["deny"] if d.startswith("Edit(")]
    assert "Read(~/.ssh/**)" in p["deny"]


def test_mcp_servers_are_read_from_config_and_installed_plugins(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"gbrain": {}},
        "projects": {"/w": {"mcpServers": {"proj": {}}}, "/other": {"mcpServers": {"no": {}}}}}))
    plug = tmp_path / "cache" / "canopy"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".mcp.json").write_text(json.dumps({"mcpServers": {"canopy-web": {"type": "http"}}}))
    (tmp_path / ".claude" / "plugins").mkdir(parents=True)
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"canopy@canopy": [{"installPath": str(plug)}]}}))
    got = np.mcp_servers(home=tmp_path, worktree="/w/cx-a")
    assert got == {"gbrain", "proj", "plugin_canopy_canopy-web"}
    assert np.mcp_server_names(home=tmp_path, worktree="/w/cx-a")["plugin_canopy_canopy-web"] \
        == "plugin:canopy:canopy-web"


def test_servers_the_capability_never_reaches_are_not_started():
    """An `mcp__<server>` deny still spawns the server; `deniedMcpServers` does not,
    and it is spelled with the CONFIG name."""
    assert np.servers_to_stop(ASK, NAMES) == ["gbrain", "plugin:ace:ace-connect"]
    assert np.servers_to_stop({"name": "none"}, NAMES) == sorted(NAMES.values())
    assert np.servers_to_stop({"tools": ["*"]}, NAMES) == []
    # every server it stops is one settings_for already denies — never more access
    deny = _perms()["deny"]
    tool_form = {v: k for k, v in NAMES.items()}
    assert all(f"mcp__{tool_form[n]}" in deny for n in np.servers_to_stop(ASK, NAMES))


def test_cli_settings_carry_the_stopped_servers(monkeypatch):
    monkeypatch.setattr(np, "mcp_server_names", lambda **k: NAMES)
    doc = np.cli_settings(ASK, cwd="/w/cx-a")
    assert {"serverName": "plugin:ace:ace-connect"} in doc["deniedMcpServers"]
    assert {"serverName": "plugin:canopy:canopy-web"} not in doc["deniedMcpServers"]


def test_stopped_servers_merge_with_a_repos_own_and_replace_ours(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"deniedMcpServers": [{"serverName": "repo-own"}]}))
    np.write_worktree_settings(str(tmp_path), _perms(), stop_servers=["a", "b"])
    np.write_worktree_settings(str(tmp_path), _perms(), stop_servers=["b"])
    doc = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert doc["deniedMcpServers"] == [{"serverName": "repo-own"}, {"serverName": "b"}]


def test_writing_keeps_a_repos_own_settings_and_deny_rules(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"x": 1}, "permissions": {"allow": ["Bash"], "deny": ["Read(.env)"],
                                            "defaultMode": "acceptEdits"}}))
    np.write_worktree_settings(str(tmp_path), _perms(), capability="ask")
    np.write_worktree_settings(str(tmp_path), _perms(), capability="ask")   # idempotent
    doc = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert doc["hooks"] == {"x": 1}
    assert doc["permissions"]["defaultMode"] == "dontAsk"
    assert "Bash" not in doc["permissions"]["allow"]      # the agent's grants are not the caller's
    assert doc["permissions"]["deny"].count("Read(.env)") == 1
    assert doc["permissions"]["deny"].count("Edit") == 1


# --- wiring: the runner writes it for a caller's session, and only theirs -------------

@pytest.fixture()
def _box(monkeypatch, tmp_path):
    monkeypatch.setattr(caller, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(caller, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")
    monkeypatch.setattr(np, "mcp_server_names", lambda **k: NAMES)
    monkeypatch.setattr(execute, "NATIVE_WAIT_SECONDS", 0)
    return tmp_path


def _restricted():
    return {"id": "3f2b8c1e-0000-4000-8000-000000000001", "agent_slug": "ace",
            "origin_ref": {"thread_id": "18c9abc"}, "prompt": "/ace:turn",
            "caller_context": {"profile": "restricted", "capability": ASK,
                               "conversation": {"thread_id": "18c9abc"}}}


def test_reuse_writes_the_native_layer_before_sending(monkeypatch, _box):
    from tests.test_execute import FakeClient, _cfg

    wt = _box / "wt" / "emdash-cx-payments-9abc-k2j4l"
    wt.mkdir(parents=True)
    monkeypatch.setattr(emdash, "task_worktree", lambda db, project, task: str(wt))
    seen = {}
    monkeypatch.setattr(cdp_control, "open_and_send", lambda task, text, port=9222, **k: seen.update(
        had=(wt / ".claude" / "settings.json").exists()) or {"ok": True})
    execute.execute_turn(_cfg(), FakeClient({"reuse": True, "emdash_task_id": "cx-payments-9abc"}),
                         "r-1", _restricted())
    assert seen == {"had": True}
    doc = json.loads((wt / ".claude" / "settings.json").read_text())
    assert "Edit" in doc["permissions"]["deny"]


def test_a_worktree_not_named_for_the_task_is_never_written(monkeypatch, _box):
    from tests.test_execute import FakeClient, _cfg

    other = _box / "wt" / "emdash-c-someone-else-1111-aaaaa"
    other.mkdir(parents=True)
    monkeypatch.setattr(emdash, "task_worktree", lambda db, project, task: str(other))
    monkeypatch.setattr(cdp_control, "open_and_send", lambda *a, **k: {"ok": True})
    client = FakeClient({"reuse": True, "emdash_task_id": "cx-payments-9abc"})
    execute.execute_turn(_cfg(), client, "r-1", _restricted())
    assert not (other / ".claude").exists()
    assert not client.failed                        # the hook still confines it; the turn runs
    statuses = [e["payload"] for _, evs in client.events for e in evs
                if e["payload"].get("status") == "native_permissions"]
    assert statuses and statuses[0]["written"] is False


def test_a_full_profile_turn_gets_no_native_layer(monkeypatch, _box):
    from tests.test_execute import FakeClient, _cfg

    monkeypatch.setattr(emdash, "task_worktree",
                        lambda *a: pytest.fail("a full-profile session is not confined"))
    monkeypatch.setattr(cdp_control, "create_task", lambda agent, prompt, task_name, port: {"task": task_name})
    turn = {**_restricted(), "caller_context": {"profile": "full"}}
    execute.execute_turn(_cfg(), FakeClient({"reuse": False}), "r-1", turn)


def test_emdash_names_the_worktree_from_its_own_db(tmp_path):
    import sqlite3

    db = tmp_path / "emdash4.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE projects (id text, name text);
        CREATE TABLE tasks (id text, project_id text, name text, deleted_at text);
        CREATE TABLE conversations (id text, task_id text, cwd text, created_at text);
        INSERT INTO projects VALUES ('p1', 'ace');
        INSERT INTO tasks VALUES ('t1', 'p1', 'cx-payments-9abc', NULL);
        INSERT INTO conversations VALUES ('c1', 't1', '/wt/emdash-cx-payments-9abc-k2j4l', '2026-09-26');
    """)
    con.commit()
    con.close()
    assert emdash.task_worktree(str(db), "ace", "cx-payments-9abc") == "/wt/emdash-cx-payments-9abc-k2j4l"
    assert emdash.task_worktree(str(db), "hal", "cx-payments-9abc") is None
    assert emdash.task_worktree(str(tmp_path / "missing.db"), "ace", "x") is None
