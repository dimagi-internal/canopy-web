"""An agent turn's connection tokens (Salesforce, …) are staged per TURN by the canopy CLI.

Pinned: the runner asks canopy-web which providers the turn's agent has and calls the CLI
once per provider listed (none for an agent without connections); the CLI is called with
the runner bearer + runner/turn ids; exit 3 (no connection) is silent and clears a stale
file; other failures warn (never with a token) and the turn runs; the heartbeat re-stages
only near expiry; each file goes when the turn ends.
"""
from __future__ import annotations

import json
import subprocess

import pytest


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "_bearer", lambda min_life=0: "runner-bearer")
    monkeypatch.setattr(cloud_runner, "_canopy_cli", lambda: "/bin/canopy")
    monkeypatch.setattr(cloud_runner, "_CONN_HOLDS", {})
    logs = []
    monkeypatch.setattr(cloud_runner, "_log", lambda m, *a, **k: logs.append(m))
    cloud_runner.calls = []
    cloud_runner.logs = logs
    return cloud_runner


def _path(tmp_path, slug="eva", provider="salesforce"):
    return tmp_path / ".canopy" / "connections" / slug / f"{provider}.json"


def _held(*providers, slug="eva", runner="r1"):
    return (slug, runner, frozenset(providers or ("salesforce",)))


def _cli(cr, monkeypatch, tmp_path, *, code=0, expires=None, stderr=""):
    def run(argv, **kw):
        cr.calls.append((argv, kw))
        if code == 0:
            p = _path(tmp_path, provider=argv[3])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"access_token": "SECRET", "expires_at": expires}))
        return subprocess.CompletedProcess(argv, code, "", stderr)
    monkeypatch.setattr(cr.subprocess, "run", run)


def test_stages_through_the_cli_with_the_runner_bearer(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    argv, kw = cr.calls[0]
    assert argv == ["/bin/canopy", "cred", "connection-token", "salesforce", "--runner", "--agent", "eva",
                    "--runner-id", "r1", "--turn", "t1", "--min-life", "900"]
    assert kw["env"]["CANOPY_TOKEN"] == "runner-bearer" and kw["timeout"] == 120
    assert _path(tmp_path).exists() and "SECRET" not in " ".join(cr.logs)


def test_exit_3_is_silent_and_removes_a_stale_file(cr, monkeypatch, tmp_path):
    _path(tmp_path).parent.mkdir(parents=True)
    _path(tmp_path).write_text(json.dumps({"expires_at": 1}))
    _cli(cr, monkeypatch, tmp_path, code=3, stderr="not_connected")
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=100)
    assert not _path(tmp_path).exists() and cr.logs == []


def test_other_failure_warns_and_the_turn_runs(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, code=1, stderr="boom")
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert any("warn" in m and "boom" in m for m in cr.logs)


def test_heartbeat_restages_only_near_expiry(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    cr._restage_connections("t1", now=9_500)            # 500s of life left: restage
    assert len(cr.calls) == 2
    cr._restage_connections("t1", now=5_000)            # 5000s left: leave it
    assert len(cr.calls) == 2


def test_file_removed_at_turn_end(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    cr._unstage_connections("t1")
    assert not _path(tmp_path).exists()


def test_no_agent_stages_nothing(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path)
    cr._stage_connections("r1", "t1", "")
    assert cr.calls == []


def test_sensitive_paths_hide_the_connections_dir():
    from canopy_runner import native_permissions as np
    assert "~/.canopy/connections/**" in np.SENSITIVE_PATHS


def test_two_turns_of_one_agent_share_the_file(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)
    cr._stage_connection_token("r1", "B", "eva", "salesforce", now=0)
    cr._unstage_connections("A")
    assert _path(tmp_path).exists()          # B still holds it
    cr._unstage_connections("B")
    assert not _path(tmp_path).exists()


def test_a_turn_holds_before_the_cli_runs(cr, monkeypatch, tmp_path):
    seen = {}

    def run(argv, **kw):
        seen["held"] = dict(cr._CONN_HOLDS)
        return subprocess.CompletedProcess(argv, 1, "", "x")
    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert "t1" in seen["held"] and cr._CONN_HOLDS == {}     # dropped after the failure


def test_startup_clears_leftovers(cr, tmp_path):
    f = _path(tmp_path)
    f.parent.mkdir(parents=True)
    f.write_text("{}")
    cr._clear_stale_connection_tokens()
    assert not f.exists()


# ---- the wiring: which turns get a token --------------------------------------------

# The turn harness is test_confined_turns' (as tests/test_chat_attachments.py borrows it).
from test_confined_turns import _turn  # noqa: E402
from test_confined_turns import cr as turn_cr  # noqa: E402,F401


@pytest.fixture()
def wired(turn_cr, monkeypatch, tmp_path):  # noqa: F811 - the fixture imported above
    mod, calls = turn_cr
    monkeypatch.setattr(mod.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(mod, "_bearer", lambda min_life=0: "b")
    monkeypatch.setattr(mod, "_canopy_cli", lambda: "/bin/canopy")
    monkeypatch.setattr(mod, "_CONN_HOLDS", {})
    sf = []
    seen = {}
    real_exec = mod.execute_prompt
    real_api = mod._api

    def api(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/connections"):
            seen.setdefault("listed", []).append(path)
            return 200, {"providers": ["salesforce"]}
        return real_api(method, path, body, **kw)
    monkeypatch.setattr(mod, "_api", api)

    def run(argv, **kw):
        if "connection-token" in argv:
            sf.append(argv)
            p = _path(tmp_path, "ace")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"expires_at": 9e12}))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(mod.subprocess, "run", run)

    def execute(*a, **k):
        seen["file_during_turn"] = _path(tmp_path, "ace").exists()
        return real_exec(*a, **k)
    monkeypatch.setattr(mod, "execute_prompt", execute)
    return mod, sf, seen, tmp_path


def test_a_confined_turn_is_never_staged(wired):
    mod, sf, seen, _ = wired
    mod._run_turn("r-1", _turn())
    assert sf == [] and seen["file_during_turn"] is False and "listed" not in seen


def test_an_ordinary_agent_turn_is_staged_and_cleaned_up(wired):
    mod, sf, seen, tmp_path = wired
    mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    assert len(sf) == 1 and sf[0][-6:-2] == ["--runner-id", "r-1", "--turn", sf[0][-3]]
    assert sf[0][-2:] == ["--min-life", "900"]
    assert seen["file_during_turn"] is True
    assert not _path(tmp_path, "ace").exists()


def test_an_abrupt_exit_after_staging_still_unstages(wired, monkeypatch):
    mod, sf, _, tmp_path = wired

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(mod, "execute_prompt", boom)
    with pytest.raises(KeyboardInterrupt):
        mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    assert len(sf) == 1 and not _path(tmp_path, "ace").exists() and mod._CONN_HOLDS == {}


def test_a_restage_in_flight_when_the_turn_ends_adds_no_holder(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    real = cr.subprocess.run

    def run(argv, **kw):                      # the turn ends while the CLI runs
        out = real(argv, **kw)
        cr._unstage_connections("t1")
        _path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _path(tmp_path).write_text("{}")      # the CLI's write lands after the unstage
        return out
    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._restage_connections("t1", now=9_900)
    assert "t1" not in cr._CONN_HOLDS and not _path(tmp_path).exists()


def test_a_restage_after_the_turn_ended_does_nothing(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    cr._unstage_connections("t1")
    n = len(cr.calls)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=9_900, refresh=True)
    assert len(cr.calls) == n and cr._CONN_HOLDS == {}


def test_an_interrupt_during_staging_releases_the_hold(cr, monkeypatch, tmp_path):
    def run(argv, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(cr.subprocess, "run", run)
    with pytest.raises(KeyboardInterrupt):
        cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert cr._CONN_HOLDS == {}


def test_main_clears_leftovers_at_startup(cr, monkeypatch):
    order = []

    class StopMainError(Exception):
        pass
    monkeypatch.setattr(cr, "BASE_URL", "https://x")
    monkeypatch.setattr(cr, "pair_or_load", lambda: "r1")
    monkeypatch.setattr(cr, "sweep_turn_envs", lambda: order.append("sweep") or 0)

    def clear():
        order.append("clear")
        raise StopMainError
    monkeypatch.setattr(cr, "_clear_stale_connection_tokens", clear)
    with pytest.raises(StopMainError):
        cr.main()
    assert order == ["sweep", "clear"]


# ---- M1: a turn's first stage always asks canopy-web ----------------------------------

def test_a_first_stage_ignores_a_fresh_file_another_turn_left(cr, monkeypatch, tmp_path):
    """A fresh-looking file is reused only by a heartbeat re-stage: a new turn's first
    stage calls the CLI, so canopy-web checks THIS turn and records its token issue."""
    _path(tmp_path).parent.mkdir(parents=True)
    _path(tmp_path).write_text(json.dumps({"access_token": "OLD", "expires_at": 10_000}))
    _cli(cr, monkeypatch, tmp_path, expires=20_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert len(cr.calls) == 1
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0, refresh=True)     # heartbeat: fresh, kept
    assert len(cr.calls) == 1


# ---- T7b: exit 3 on a heartbeat re-stage, in one lock section -------------------------

class _CountingLock:
    def __init__(self):
        import threading
        self._l = threading.Lock()
        self.n = 0

    def __enter__(self):
        self._l.acquire()
        self.n += 1
        return self

    def __exit__(self, *exc):
        self._l.release()


def test_exit_3_on_a_restage_keeps_the_hold_in_one_lock_section(cr, monkeypatch, tmp_path):
    lock = _CountingLock()
    monkeypatch.setattr(cr, "_CONN_LOCK", lock)
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)

    def run(argv, **kw):                       # the connection went away mid-turn
        lock.n = 0
        return subprocess.CompletedProcess(argv, 3, "", "not_connected")
    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._restage_connections("t1", now=9_900)
    assert lock.n == 1                          # handled in ONE section, no drop/re-add gap
    assert cr._CONN_HOLDS == {"t1": _held()} and not _path(tmp_path).exists()


def test_exit_3_on_a_restage_keeps_a_siblings_file(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)
    cr._stage_connection_token("r1", "B", "eva", "salesforce", now=0)
    monkeypatch.setattr(cr.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", ""))
    cr._restage_connections("A", now=9_900)
    assert set(cr._CONN_HOLDS) == {"A", "B"} and _path(tmp_path).exists()


# ---- which providers: only those canopy-web lists for the turn's agent -----------------

def _listing(cr, monkeypatch, status=200, body=None):
    asked = []

    def api(method, path, body_=None, **kw):
        asked.append((method, path))
        return status, body
    monkeypatch.setattr(cr, "_api", api)
    return asked


def test_an_agent_with_no_connections_costs_one_call_and_no_cli(cr, monkeypatch, tmp_path):
    asked = _listing(cr, monkeypatch, body={"providers": []})
    _cli(cr, monkeypatch, tmp_path)
    cr._stage_connections("r1", "t1", "eva")
    assert asked == [("GET", "/runners/r1/turns/t1/connections")]
    assert cr.calls == [] and cr.logs == [] and cr._CONN_HOLDS == {}


def test_each_listed_provider_is_staged_held_and_removed(cr, monkeypatch, tmp_path):
    _listing(cr, monkeypatch, body={"providers": ["intacct", "salesforce"]})
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connections("r1", "t1", "eva")
    assert [argv[3] for argv, _ in cr.calls] == ["intacct", "salesforce"]
    assert _path(tmp_path, provider="intacct").exists() and _path(tmp_path).exists()
    assert cr._CONN_HOLDS == {"t1": _held("intacct", "salesforce")}
    cr._restage_connections("t1", now=9_500)          # both near expiry: both re-staged
    assert [argv[3] for argv, _ in cr.calls[2:]] == ["intacct", "salesforce"]
    cr._unstage_connections("t1")
    assert not _path(tmp_path, provider="intacct").exists() and not _path(tmp_path).exists()


@pytest.mark.parametrize("status,reason", [(503, "listing -> 503"), (0, "listing: canopy-web unreachable")])
def test_a_listing_failure_warns_and_the_turn_runs(cr, monkeypatch, tmp_path, status, reason):
    _listing(cr, monkeypatch, status=status)
    _cli(cr, monkeypatch, tmp_path)
    cr._stage_connections("r1", "t1", "eva")
    assert cr.calls == []
    assert cr.logs == [f'warn: connections.stage_failed agent=eva provider=* turn=t1 reason="{reason}"']


def test_a_canopy_web_without_the_listing_route_is_silent(cr, monkeypatch, tmp_path):
    _listing(cr, monkeypatch, status=404)
    _cli(cr, monkeypatch, tmp_path)
    cr._stage_connections("r1", "t1", "eva")
    assert cr.calls == [] and cr.logs == []


def test_an_unusable_provider_key_never_becomes_a_path(cr, monkeypatch, tmp_path):
    _listing(cr, monkeypatch, body={"providers": ["../../etc", "Sales Force"]})
    _cli(cr, monkeypatch, tmp_path)
    cr._stage_connections("r1", "t1", "eva")
    assert cr.calls == [] and len(cr.logs) == 2 and cr._CONN_HOLDS == {}


def test_a_sibling_holds_only_its_own_providers_files(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)
    cr._stage_connection_token("r1", "B", "eva", "intacct", now=0)
    cr._unstage_connections("A")
    assert not _path(tmp_path).exists() and _path(tmp_path, provider="intacct").exists()


def test_a_failure_logs_the_connections_vocabulary_without_the_token(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, code=1, stderr="Salesforce refused the request.")
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert cr.logs == ['warn: connections.stage_failed agent=eva provider=salesforce turn=t1 '
                       'reason="Salesforce refused the request."']


# ---- through the real `_api`: one line per failure, none for a 404 ---------------------

@pytest.mark.parametrize("failure,lines", [
    (404, []),
    (503, ['warn: connections.stage_failed agent=eva provider=* turn=t1 reason="listing -> 503"']),
    ("down", ['warn: connections.stage_failed agent=eva provider=* turn=t1 '
              'reason="listing: canopy-web unreachable"']),
])
def test_the_listing_logs_once_through_the_real_api(cr, monkeypatch, tmp_path, failure, lines):
    import io
    import urllib.error

    def urlopen(req, timeout=None):
        if failure == "down":
            raise urllib.error.URLError("connection refused")
        raise urllib.error.HTTPError(req.full_url, failure, "x", {}, io.BytesIO(b'{"detail": "x"}'))
    monkeypatch.setattr(cr, "BASE_URL", "https://canopy.test")
    monkeypatch.setattr(cr.urllib.request, "urlopen", urlopen)
    _cli(cr, monkeypatch, tmp_path)
    cr._stage_connections("r1", "t1", "eva")
    assert cr.calls == [] and cr.logs == lines


def test_other_calls_through_the_real_api_still_log_their_failures(cr, monkeypatch):
    import io
    import urllib.error

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b"busy"))
    monkeypatch.setattr(cr, "BASE_URL", "https://canopy.test")
    monkeypatch.setattr(cr.urllib.request, "urlopen", urlopen)
    assert cr._api("GET", "/x")[0] == 503
    assert cr.logs == ["GET /x -> 503: b'busy'"]


# ---- adoption: a new daemon's turn keeps what the old daemon staged for it -----------

def test_the_startup_clear_spares_only_what_an_adopted_turn_holds(cr, tmp_path):
    for slug, provider in (("eva", "salesforce"), ("eva", "gmail"), ("ace", "salesforce")):
        _path(tmp_path, slug, provider).parent.mkdir(parents=True, exist_ok=True)
        _path(tmp_path, slug, provider).write_text("{}")
    cr._reserve_adopted_connections("r1", "t1", "eva", ["salesforce", "../x"])
    cr._clear_stale_connection_tokens()
    assert _path(tmp_path).exists()
    assert not _path(tmp_path, "eva", "gmail").exists() and not _path(tmp_path, "ace").exists()


def test_adoption_lets_go_of_a_provider_no_longer_listed(cr, monkeypatch, tmp_path):
    for provider in ("salesforce", "gmail"):
        _path(tmp_path, provider=provider).parent.mkdir(parents=True, exist_ok=True)
        _path(tmp_path, provider=provider).write_text("{}")
    cr._reserve_adopted_connections("r1", "t1", "eva", ["salesforce", "gmail"])
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {"providers": ["salesforce"]}))
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._adopt_connections("r1", "t1", "eva")
    assert len(cr.calls) == 1                               # its first stage asks canopy-web
    assert cr._CONN_HOLDS["t1"][2] == {"salesforce"}
    assert _path(tmp_path).exists() and not _path(tmp_path, provider="gmail").exists()


def test_adoption_whose_stage_fails_leaves_no_file(cr, monkeypatch, tmp_path):
    _path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _path(tmp_path).write_text("{}")
    cr._reserve_adopted_connections("r1", "t1", "eva", ["salesforce"])
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {"providers": ["salesforce"]}))
    _cli(cr, monkeypatch, tmp_path, code=1, stderr="boom")
    cr._adopt_connections("r1", "t1", "eva")
    assert cr._CONN_HOLDS == {} and not _path(tmp_path).exists()
