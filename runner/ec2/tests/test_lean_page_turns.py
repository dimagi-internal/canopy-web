"""Lean page turns: a question asked in a host site's panel starts with only what a page needs.

canopy-web marks such a turn `page_site.lean` (the agent entry's `page_lean:`). The runner then
opens its ACP session with user settings only, canopy's plugin and MCP only and few built-in tools,
puts the page brief in the system prompt, streams the reply being written to the panel, and does
not refresh the agent's plugin, stage its connections or fetch a fresh clone again. A `prepare`
frame makes the conversation's working copy before its first turn arrives.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
import subprocess
import threading
import time

import pytest


def test_lean_is_what_canopy_web_says(cloud_runner):
    assert cloud_runner._lean_page({"page_site": {"site": "connect-labs", "lean": True}})
    assert not cloud_runner._lean_page({"page_site": {"site": "connect-labs"}})
    assert not cloud_runner._lean_page({"page_site": {"site": "connect-labs", "lean": "yes"}})
    assert not cloud_runner._lean_page({})


def _plugin_home(tmp_path):
    plugins = tmp_path / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "canopy@canopy": [{}], "ace@ace": [{}], "chrome-sales@chrome-sales": [{}]}}))
    root = tmp_path / "canopy-plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"mcpServers": {
        "canopy-web": {"type": "http", "url": "https://canopy.test/api/mcp/",
                       "headersHelper": "node ${CLAUDE_PLUGIN_ROOT}/scripts/canopy-web-mcp-headers.js"},
        "projects": {"type": "http", "url": "https://canopy.test/api/mcp/projects/"}}}))
    return root


def test_the_lean_session_loads_canopy_and_the_page_only(cloud_runner, tmp_path):
    root = _plugin_home(tmp_path)
    meta = cloud_runner._lean_session_meta(tmp_path, brief="<page_context/>", plugin_root=root)
    options = meta["claudeCode"]["options"]
    # No project settings: the worktree's CLAUDE.md and .claude/ settings stay out.
    assert options["settingSources"] == ["user"]
    assert options["settings"]["enabledPlugins"] == {"ace@ace": False, "chrome-sales@chrome-sales": False}
    # canopy-web alone, as the plugin declares it, under the name its tools carry.
    assert options["strictMcpConfig"] is True
    assert options["mcpServers"] == {"plugin_canopy_canopy-web": {
        "type": "http", "url": "https://canopy.test/api/mcp/",
        "headersHelper": f"node {root}/scripts/canopy-web-mcp-headers.js"}}
    assert options["env"]["CLAUDE_PLUGIN_ROOT"] == str(root)
    # Few built-in tools, no skills, and a small tool list is never deferred.
    assert options["tools"] == ["Read", "Grep", "Glob", "ToolSearch"]
    assert options["extraArgs"] == {"disable-slash-commands": None}
    assert options["env"]["ENABLE_TOOL_SEARCH"] == "auto:5"
    # The brief rides the system prompt, never the person's message.
    assert meta["systemPrompt"] == {"append": "<page_context/>"}


def test_without_the_plugins_server_entry_the_session_keeps_its_own_mcp(cloud_runner, tmp_path):
    meta = cloud_runner._lean_session_meta(tmp_path, plugin_root=tmp_path / "nowhere")
    options = meta["claudeCode"]["options"]
    assert "strictMcpConfig" not in options and "mcpServers" not in options
    assert options["settings"]["enabledPlugins"] == {} and "systemPrompt" not in meta


def test_the_brief_is_fetched_off_the_turns_thread(cloud_runner, monkeypatch):
    calls = []

    def fake_api(method, path, body=None, **kw):
        calls.append((method, path))
        time.sleep(0.05)
        return 200, {"text": "<page_context>x</page_context>"}

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    brief = cloud_runner._PageBrief("11111111-2222-3333-4444-555555555555")
    assert brief.wait(2) == "<page_context>x</page_context>"
    assert calls == [("GET", "/turns/11111111-2222-3333-4444-555555555555/page-brief")]


def test_a_brief_that_does_not_come_leaves_the_prompt_alone(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (500, None))
    assert cloud_runner._PageBrief("t" * 36).wait(2) == ""


def test_live_text_sends_the_latest_whole_reply_and_coalesces(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "LIVE_TEXT_EVERY_SECONDS", 0.05)
    sent = []
    gate = threading.Event()

    def post(body):
        sent.append(body)
        gate.set()
        return 200, {"sent": True}

    live = cloud_runner._LiveText("t" * 36, post=post)
    long = "x" * (cloud_runner.CREDENTIAL_NOTICE_MAX_CHARS + 1)
    live(0, long + "Ch")
    live(0, long + "Check")
    live(0, long + "Checking.")
    deadline = time.time() + 2
    while time.time() < deadline and (not sent or sent[-1]["text"] != long + "Checking."):
        time.sleep(0.01)
    live.stop()
    assert sent[-1] == {"message": 0, "text": long + "Checking."}
    assert len(sent) <= 3


def test_live_text_never_streams_a_credentials_refusal(cloud_runner, monkeypatch):
    """A capped credential says so in one short line, which the turn holds back while it
    fails over; the live view must not show it either."""
    monkeypatch.setattr(cloud_runner, "LIVE_TEXT_EVERY_SECONDS", 0.01)
    sent = []
    live = cloud_runner._LiveText("t" * 36, post=lambda body: sent.append(body) or (200, {"sent": True}))
    live(0, "You've hit your weekly limit · resets 9pm (UTC)")
    time.sleep(0.2)
    live.stop()
    assert sent == []


def test_live_text_stops_when_nobody_hears_it(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "LIVE_TEXT_EVERY_SECONDS", 0.01)
    sent = []

    def post(body):
        sent.append(body)
        return 200, {"sent": False}  # a sensitive conversation: its words never stream

    live = cloud_runner._LiveText("t" * 36, post=post)
    long = "x" * (cloud_runner.CREDENTIAL_NOTICE_MAX_CHARS + 1)
    live(0, long + "a")
    deadline = time.time() + 2
    while time.time() < deadline and not sent:
        time.sleep(0.01)
    time.sleep(0.05)
    live(0, long + "ab")
    time.sleep(0.1)
    live.stop()
    assert len(sent) == 1


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def agent_clone(cloud_runner, monkeypatch, tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "CLAUDE.md").write_text("v1\n")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1", cwd=origin)
    agents = tmp_path / "agents"
    agents.mkdir()
    _git("clone", "-q", str(origin), str(agents / "ace"), cwd=tmp_path)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(cloud_runner, "_CLONE_FETCH", {})
    monkeypatch.setattr(cloud_runner, "_clone_refresh_env", lambda slug, env: env)
    return agents / "ace"


SESSION = "3a812762-f945-4987-951c-4248998fa3b0"


def test_prepare_cuts_the_conversations_worktree_before_its_turn(cloud_runner, agent_clone, tmp_path):
    assert cloud_runner._prepare_session("ace", SESSION) is True
    worktree = tmp_path / "work" / "sessions" / cloud_runner._safe_session_dirname(SESSION)
    assert (worktree / "CLAUDE.md").read_text() == "v1\n"


def test_prepare_never_bootstraps_an_agent_this_box_lacks(cloud_runner, agent_clone):
    assert cloud_runner._prepare_session("hal", SESSION) is False
    assert cloud_runner._prepare_session("ace", "../../etc") is False
    assert cloud_runner._prepare_session("", SESSION) is False


def test_prepare_with_no_conversation_warms_the_agent(cloud_runner, agent_clone, monkeypatch):
    """The panel opened, nothing asked yet: the clone is pulled and the box's own resolve of
    the agent's credentials is cached for the turn that follows (never a turn's resolve)."""
    resolved = []
    monkeypatch.setattr(cloud_runner, "secrets_off_disk", lambda: True)
    monkeypatch.setattr(cloud_runner, "_turn_values", lambda slug, **kw: resolved.append((slug, kw)) or {})
    assert cloud_runner._prepare_session("ace", "") is True
    assert resolved == [("ace", {"outside_turn": True})]
    assert cloud_runner._prepare_session("hal", "") is False


def test_a_lean_turn_neither_refreshes_the_plugin_nor_refetches_a_fresh_clone(
        cloud_runner, agent_clone, monkeypatch):
    refreshed, fetched = [], []
    monkeypatch.setattr(cloud_runner, "_refresh_agent_plugin", lambda slug, clone: refreshed.append(slug))
    real_fetch = cloud_runner._fetch_clone
    monkeypatch.setattr(cloud_runner, "_fetch_clone",
                        lambda clone, env=None, **kw: fetched.append(kw) or real_fetch(clone, env, **kw))
    monkeypatch.setattr(cloud_runner, "_ensure_agent_clone", lambda *a, **k: None)
    monkeypatch.setattr(cloud_runner, "_is_stored_agent", lambda *a, **k: False)
    assert cloud_runner._prepare_session("ace", SESSION)
    fetched.clear()
    turn = {"id": "9" * 36, "agent_slug": "ace", "origin_ref": {"chat_session_id": SESSION},
            "page_site": {"site": "connect-labs", "lean": True}}
    cloud_runner._TURN_ENV.lean = True
    try:
        path = cloud_runner._turn_cwd(turn, turn["id"])
    finally:
        cloud_runner._TURN_ENV.lean = False
    assert (path / "CLAUDE.md").exists()
    assert refreshed == [] and fetched == []
    # An ordinary turn on the same conversation still does both.
    cloud_runner._turn_cwd(turn, turn["id"])
    assert refreshed == ["ace"] and fetched


def test_the_ws_loops_chores_run_off_the_socket_thread(cloud_runner, monkeypatch):
    """A wake must be claimed at once: the poll tick's chores (streams, backfills, the usage
    report, mints, credentials, the inbox) run on their own thread, never between a wake and
    its claim."""
    ran = []
    for name in ("_drain_session_interrupts", "_sync_session_views", "_drain_mint", "_drain_inbox"):
        monkeypatch.setattr(cloud_runner, name, lambda *a, _n=name, **k: ran.append((_n, threading.current_thread().name)))
    for name in ("_maybe_reread_credentials", "_credential_tick", "_job_cap_tick"):
        monkeypatch.setattr(cloud_runner, name, lambda *a, _n=name, **k: ran.append((_n, threading.current_thread().name)))
    monkeypatch.setattr(cloud_runner, "POLL_SECONDS", 0)
    chores = cloud_runner._WsChores("rid")
    chores.start()
    deadline = time.time() + 2
    while time.time() < deadline and not any(n == "_drain_inbox" for n, _ in ran):
        time.sleep(0.01)
    chores.stop()
    assert {n for n, _ in ran} >= {"_drain_session_interrupts", "_sync_session_views", "_drain_inbox",
                                   "_credential_tick"}
    assert {t for _, t in ran} == {"ws-chores"}
    src = open(cloud_runner.__file__).read()
    ws = src[src.index("def run_over_ws"):src.index("def main()")]
    tick = ws[ws.index("if time.monotonic() - last_poll >= POLL_SECONDS:"):]
    tick = tick[:tick.index("except Exception as exc:")]
    assert "_drain(ws, runner_id)" in tick and "_sync_session_views" not in tick and "_drain_inbox" not in tick


def test_run_acp_starts_a_lean_session_and_streams_the_reply(cloud_runner, monkeypatch, tmp_path):
    """The wiring, end to end through run_acp and the real drive_turn: the agent is made with
    the lean session meta, and every reply fragment reaches the live view whole so far."""
    made, heard = [], []

    class Agent:
        session_id = "acp-lean"

        def __init__(self, *, cwd, env, on_update, session_meta=None, **kw):
            made.append(session_meta)
            self.on_update = on_update
            self._session_meta = session_meta

        def start(self):
            return {}

        def new_session(self, timeout=None):
            return self.session_id

        def set_mode(self, mode):
            return SimpleNamespace(result=lambda timeout=None: {})

        def prompt(self, _p):
            for text in ("EHA ", "is furthest", " behind."):
                self.on_update(self.session_id, {"sessionUpdate": "agent_message_chunk",
                                                 "content": {"type": "text", "text": text}})
            return SimpleNamespace(result=lambda timeout=None: {"stopReason": "end_turn"})

        def cancel(self):
            pass

        def close(self):
            pass

    import canopy_acp

    class Core:
        AcpAgent = Agent
        UpdateReducer = canopy_acp.UpdateReducer

    monkeypatch.setattr(cloud_runner, "_acp_core", lambda: Core)
    monkeypatch.setattr(cloud_runner, "_agent_env", lambda slug: {})
    monkeypatch.setattr(cloud_runner, "_acp_spawn_for", lambda tid: None)
    cloud_runner._TURN_ENV.lean = True
    cloud_runner._TURN_ENV.page_brief = "<page_context/>"
    cloud_runner._TURN_ENV.live_text = lambda i, t: heard.append((i, t))
    try:
        ok, text, _ = cloud_runner.run_acp("which partner?", "t-lean", lambda e: None, cwd=tmp_path)
    finally:
        cloud_runner._TURN_ENV.lean = False
        cloud_runner._TURN_ENV.page_brief = ""
        cloud_runner._TURN_ENV.live_text = None
    assert ok and text == "EHA is furthest behind."
    assert made and made[0]["systemPrompt"] == {"append": "<page_context/>"}
    assert made[0]["claudeCode"]["options"]["settingSources"] == ["user"]
    assert heard == [(0, "EHA "), (0, "EHA is furthest"), (0, "EHA is furthest behind.")]


def test_a_brief_waiting_for_the_passkey_is_asked_again_and_the_turn_waits(cloud_runner, monkeypatch):
    """The page's permission waits for the person's passkey: the brief asks again (without
    asking the person twice) and the model waits past the usual few seconds for it."""
    calls = []

    def fake_api(method, path, body=None, **kw):
        calls.append(path)
        if len(calls) < 4:
            return 200, {"waiting": True, "wait_left": 100.0, "text": ""}
        return 200, {"waiting": False, "text": "<page_context>tools</page_context>"}

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    brief = cloud_runner._PageBrief("t" * 36, poll_every=0.3)
    # Longer than the usual wait: the turn holds for the person.
    assert brief.wait(0.2) == "<page_context>tools</page_context>"
    assert brief.waiting is True
    tid = "t" * 36
    assert calls == [f"/turns/{tid}/page-brief"] + [f"/turns/{tid}/page-brief?poll=true"] * 3


def test_a_brief_still_waiting_at_the_backstop_leaves_the_prompt_alone(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "PAGE_GRANT_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda *a, **k: (200, {"waiting": True, "text": ""}))
    assert cloud_runner._PageBrief("t" * 36, poll_every=0.1).wait(0.1) == ""
