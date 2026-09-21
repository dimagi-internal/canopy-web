"""A caller's turn runs confined: its own `cx-` session, its profile written first,
started with its capability's entry — or not at all."""
import json

import pytest

from canopy_runner import caller, cdp_control, emdash, execute, session_naming
from tests.test_execute import FakeClient, _cfg

CAP = {"name": "ask", "entry": "/ace:ask --thread {thread_id}",
       "tools": ["Read"], "bash": ["canopy email read --repo . {thread_id}"], "read_paths": ["{cwd}/**"]}


def _restricted(**kw):
    d = {"id": "3f2b8c1e-0000-4000-8000-000000000001", "agent_slug": "ace",
         "origin_ref": {"thread_id": "18c9abc", "subject": "payments?"},
         "prompt": "/ace:turn --thread 18c9abc",
         "caller_context": {"profile": "restricted", "capability": CAP,
                            "conversation": {"thread_id": "18c9abc"}}}
    d.update(kw)
    return d


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")
    monkeypatch.setattr(caller, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(caller, "CALLER_ROOT", tmp_path / "caller")
    return tmp_path


# --- naming and continuity -----------------------------------------------------------

def test_a_callers_session_is_cx_and_on_its_own_thread_key():
    t = _restricted()
    assert session_naming.build_task_name("ace", t).startswith("cx-")
    assert execute._thread_key(t) == "18c9abc#ask"
    assert session_naming._thread_key(t, "ace") == execute._thread_key(t)
    admin = {**t, "caller_context": {"profile": "full", "capability": None}}
    assert execute._thread_key(admin) == "18c9abc"
    assert session_naming.build_task_name("ace", admin).startswith("c-")


def test_cx_names_are_still_canopy_tasks():
    assert session_naming.is_canopy_task("cx-payments-9abc")
    assert session_naming.is_restricted_task("emdash-cx-payments-9abc")
    assert not session_naming.is_restricted_task("c-payments-9abc")
    assert session_naming.parse_task_name("cx-payments-9abc") == {"subject": "payments", "disc": "9abc"}


# --- execution ----------------------------------------------------------------------

def test_create_writes_the_profile_before_the_session_and_starts_with_the_entry(monkeypatch, _isolated):
    seen = {}

    def create(agent, prompt, task_name, port):
        # The guard must find the profile on the session's FIRST tool call.
        seen["profile_at_create"] = json.loads((_isolated / "profiles" / f"{task_name}.json").read_text())
        seen.update(task=task_name, prompt=prompt)
        return {"task": task_name}

    monkeypatch.setattr(cdp_control, "create_task", create)
    client = FakeClient({"reuse": False})
    execute.execute_turn(_cfg(), client, "r-1", _restricted())
    assert not client.failed, client.failed
    assert seen["task"].startswith("cx-")
    assert seen["prompt"].startswith("/ace:ask --thread 18c9abc --caller ")
    prof = seen["profile_at_create"]
    assert prof["capability"]["name"] == "ask" and prof["thread_id"] == "18c9abc"
    # And the server was told the caller's own thread key, not the admin's.
    assert client.calls[0][2] == "18c9abc#ask"


def test_a_renamed_task_gets_its_profile_too(monkeypatch, _isolated):
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda agent, prompt, task_name, port: {"task": task_name + "-2"})
    client = FakeClient({"reuse": False})
    execute.execute_turn(_cfg(), client, "r-1", _restricted())
    names = sorted(p.name for p in (_isolated / "profiles").glob("*.json"))
    assert len(names) == 2 and any(n.endswith("-2.json") for n in names)


def test_reuse_confines_before_sending(monkeypatch, _isolated):
    sent = {}
    monkeypatch.setattr(cdp_control, "open_and_send",
                        lambda task, text, port=9222, **k: sent.update(
                            task=task, had=(_isolated / "profiles" / f"{task}.json").exists())
                        or {"ok": True})
    client = FakeClient({"reuse": True, "emdash_task_id": "cx-payments-9abc", "summary": ""})
    execute.execute_turn(_cfg(), client, "r-1", _restricted())
    assert sent == {"task": "cx-payments-9abc", "had": True}


def test_a_callers_turn_never_runs_in_a_non_cx_session(monkeypatch):
    """If the server ever handed back an admin's session for a caller's thread,
    sending into it would run the caller in the full profile."""
    monkeypatch.setattr(cdp_control, "open_and_send",
                        lambda *a, **k: pytest.fail("must not send into a full-profile session"))
    client = FakeClient({"reuse": True, "emdash_task_id": "c-payments-9abc", "summary": ""})
    assert execute.execute_turn(_cfg(), client, "r-1", _restricted()).startswith("failed:")
    assert "cx- session" in client.failed[0][1]


def test_an_unwritable_profile_fails_the_turn_instead_of_running_it(monkeypatch, _isolated):
    (_isolated / "profiles").write_text("a file where the directory should be")
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: pytest.fail("must not create"))
    client = FakeClient({"reuse": False})
    assert execute.execute_turn(_cfg(), client, "r-1", _restricted()).startswith("failed:")
    assert "not run" in client.failed[0][1]


def test_an_entry_that_needs_a_thread_refuses_a_turn_without_one(monkeypatch):
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: pytest.fail("must not create"))
    t = _restricted(origin_ref={}, caller_context={"profile": "restricted", "capability": CAP,
                                                   "conversation": {}})
    client = FakeClient({"reuse": False})
    assert execute.execute_turn(_cfg(), client, "r-1", t).startswith("failed:")


def test_a_full_turn_is_untouched(monkeypatch, _isolated):
    seen = {}
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda agent, prompt, task_name, port: seen.update(t=task_name, p=prompt) or {"task": task_name})
    t = _restricted(caller_context={"profile": "full", "capability": None})
    execute.execute_turn(_cfg(), FakeClient({"reuse": False}), "r-1", t)
    assert seen["t"].startswith("c-") and seen["p"].startswith("/ace:turn --thread 18c9abc")
    assert not (_isolated / "profiles").exists()


def test_a_restricted_envelope_without_a_capability_is_deny_all():
    t = _restricted(caller_context={"profile": "restricted", "capability": None})
    cap = caller.capability(t)
    assert cap["tools"] == [] and cap["bash"] == []


# --- telling canopy-web whether this box can be given a caller's turn -----------------

def _plugin(tmp_path, guard: str, hooks: str):
    root = tmp_path / "canopy"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "profile_guard.py").write_text(guard)
    (root / "hooks" / "hooks.json").write_text(hooks)
    return root


def test_supported_only_when_the_installed_guard_enforces_it(tmp_path):
    ok = _plugin(tmp_path, "PROFILE_ENFORCEMENT_VERSION = 1\n", '{"x": "profile_guard.py"}')
    assert caller.profiles_supported(plugin_root=ok) == 1


def test_not_supported_without_the_hook_registered(tmp_path):
    root = _plugin(tmp_path, "PROFILE_ENFORCEMENT_VERSION = 1\n", '{"x": "post_tool_use.py"}')
    assert caller.profiles_supported(plugin_root=root) == 0


def test_not_supported_without_the_guard(tmp_path):
    assert caller.profiles_supported(plugin_root=tmp_path / "nothing") == 0


def test_every_heartbeat_reports_it(monkeypatch):
    from canopy_runner.client import Client

    sent = {}
    monkeypatch.setattr(caller, "profiles_supported", lambda: 1)
    c = Client("http://x", "t")
    monkeypatch.setattr(c, "_call", lambda method, path, body: sent.update(body) or (200, {}))
    c.heartbeat("r-1", [])
    assert sent["profiles"] == 1
