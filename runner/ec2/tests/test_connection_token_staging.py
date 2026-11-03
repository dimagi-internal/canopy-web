"""An agent turn's connection tokens (Salesforce, …) are staged per TURN by the canopy CLI,
which opens the sealed token on this box and refreshes it here.

Pinned: the runner asks canopy-web which providers the turn's agent has and calls the CLI
once per provider listed (none for an agent without connections); the CLI is called with
the runner bearer + runner/turn ids; exit 3 (not connected, revoking) is silent and clears
a stale file; exit 4 (waiting for a holder to seal to this box) keeps the hold, so heartbeats
retry, and says so once; other failures warn (never with a token) and the turn runs; a CLI too old for sealed connections costs one warn line and is not run; the
heartbeat re-stages only near expiry, never over a stage already running for that file,
and never at the lease's expense; each file goes when the turn ends.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "_bearer", lambda min_life=0: "runner-bearer")
    monkeypatch.setattr(cloud_runner, "_canopy_cli", lambda: "/bin/canopy")
    monkeypatch.setattr(cloud_runner, "_canopy_cli_version", lambda **k: (0, 2, 9999))
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
    assert kw["env"]["CANOPY_TOKEN"] == "runner-bearer" and kw["timeout"] == 300
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


def test_a_failed_first_stage_removes_a_file_its_sibling_left_to_it(cr, monkeypatch, tmp_path):
    """A holds; B registers; A ends and keeps the file for B; B's CLI then fails. The
    file must not outlive every holder."""
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)

    def run(argv, **kw):                      # B's CLI: A ends while it runs, then it fails
        cr._unstage_connections("A")
        assert _path(tmp_path).exists()       # A saw B's hold and left the file
        return subprocess.CompletedProcess(argv, 1, "", "boom")
    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._stage_connection_token("r1", "B", "eva", "salesforce", now=0)
    assert cr._CONN_HOLDS == {} and not _path(tmp_path).exists()


def test_a_failed_first_stage_keeps_a_file_a_sibling_still_holds(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)
    _cli(cr, monkeypatch, tmp_path, code=1, stderr="boom")
    cr._stage_connection_token("r1", "B", "eva", "salesforce", now=0)
    assert _path(tmp_path).exists() and "A" in cr._CONN_HOLDS and "B" not in cr._CONN_HOLDS


def test_an_adoptable_turn_records_its_providers_and_no_token(wired, monkeypatch, tmp_path):
    mod, sf, seen, _ = wired
    monkeypatch.setattr(mod, "TURN_STATE_ROOT", tmp_path / "turns")
    monkeypatch.setattr(mod, "adoptable_turn", lambda turn: True)
    inner = mod.execute_prompt

    def execute(prompt, turn_id, *a, **k):
        seen["state"] = (tmp_path / "turns" / turn_id / "turn.json").read_text()
        return inner(prompt, turn_id, *a, **k)
    monkeypatch.setattr(mod, "execute_prompt", execute)
    mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    state = json.loads(seen["state"])
    assert state["connections"] == ["salesforce"] and state["confined"] is False
    assert len(sf) == 1 and "SECRET" not in seen["state"] and "cct_x" not in seen["state"]


# ---- the CLI opens the sealed token on this box -----------------------------------------

def test_stage_runs_the_cli_with_min_life_900(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert cr.calls[0][0][-2:] == ["--min-life", "900"]


def test_the_stage_may_outlast_a_rotated_tokens_write_back(cr, monkeypatch, tmp_path):
    """Holding a just-rotated refresh token the CLI retries its write-back for 90 s plus
    per-call timeouts. Killing it there loses the token, so the limit is 300 s."""
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert cr.calls[0][1]["timeout"] == cr.CONNECTION_STAGE_TIMEOUT_SECONDS == 300


def test_the_bearer_outlives_the_whole_stage(cr, monkeypatch, tmp_path):
    """The CLI cannot renew the bearer it was handed: one that lapses mid write-back
    would answer 401 with a rotated token in hand."""
    asked = []
    monkeypatch.setattr(cr, "_bearer", lambda min_life=0: asked.append(min_life) or "runner-bearer")
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert asked and asked[0] > cr.CONNECTION_STAGE_TIMEOUT_SECONDS


def test_waiting_for_a_holder_keeps_the_hold_says_so_once_and_leaves_no_file(cr, monkeypatch, tmp_path):
    _path(tmp_path).parent.mkdir(parents=True)
    _path(tmp_path).write_text(json.dumps({"expires_at": 1}))
    _cli(cr, monkeypatch, tmp_path, code=4, stderr="waiting")
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert cr._CONN_HOLDS == {"t1": _held()} and not _path(tmp_path).exists()
    assert cr.logs == ["connections.waiting agent=eva provider=salesforce turn=t1"]


def test_waiting_then_a_later_heartbeat_stages_the_file(cr, monkeypatch, tmp_path):
    """No file to read counts as needing a stage: heartbeats retry until a keeper seals
    the token to this box, and the waiting line is not repeated."""
    _cli(cr, monkeypatch, tmp_path, code=4)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    cr._restage_connections("t1", now=60)
    cr._restage_connections("t1", now=120)
    assert len(cr.calls) == 3 and len(cr.logs) == 1 and not _path(tmp_path).exists()
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._restage_connections("t1", now=180)
    assert len(cr.calls) == 4 and _path(tmp_path).exists() and cr._CONN_HOLDS == {"t1": _held()}
    cr._restage_connections("t1", now=240)              # staged and fresh: left alone
    assert len(cr.calls) == 4 and len(cr.logs) == 1
    cr._unstage_connections("t1")
    assert not _path(tmp_path).exists()


def test_waiting_keeps_a_file_another_turn_relies_on(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)
    _cli(cr, monkeypatch, tmp_path, code=4)
    cr._stage_connection_token("r1", "B", "eva", "salesforce", now=0)
    assert _path(tmp_path).exists() and set(cr._CONN_HOLDS) == {"A", "B"}


def test_each_waiting_turn_and_provider_says_so_once(cr, monkeypatch, tmp_path):
    _listing(cr, monkeypatch, body={"providers": ["intacct", "salesforce"]})
    _cli(cr, monkeypatch, tmp_path, code=4)
    assert cr._stage_connections("r1", "A", "eva") == {"intacct", "salesforce"}
    cr._stage_connection_token("r1", "B", "eva", "salesforce", now=0)
    assert cr.logs == ["connections.waiting agent=eva provider=intacct turn=A",
                       "connections.waiting agent=eva provider=salesforce turn=A",
                       "connections.waiting agent=eva provider=salesforce turn=B"]


def test_a_turn_end_forgets_it_was_waiting(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, code=4)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    cr._unstage_connections("t1")
    assert cr._CONN_WAITING == set() and cr._CONN_HOLDS == {}


def test_a_turn_that_ended_during_a_waiting_restage_adds_no_hold_or_line(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)

    def run(argv, **kw):
        cr._unstage_connections("t1")
        return subprocess.CompletedProcess(argv, 4, "", "")
    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._restage_connections("t1", now=9_900)
    assert cr._CONN_HOLDS == {} and cr._CONN_WAITING == set() and cr.logs == []


def test_waiting_on_a_restage_keeps_the_hold_in_one_lock_section(cr, monkeypatch, tmp_path):
    lock = _CountingLock()
    monkeypatch.setattr(cr, "_CONN_LOCK", lock)
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)

    def run(argv, **kw):
        lock.n = 0
        return subprocess.CompletedProcess(argv, 4, "", "")
    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._restage_connections("t1", now=9_900)
    assert lock.n == 1 and cr._CONN_HOLDS == {"t1": _held()}


def test_not_connected_still_drops_the_hold_on_a_first_stage(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, code=3)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    assert cr._CONN_HOLDS == {} and cr.logs == []


def test_heartbeat_restage_still_only_refreshes_held_files(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    other = _path(tmp_path, provider="intacct")
    other.write_text(json.dumps({"expires_at": 1}))        # on disk, but not this turn's
    n = len(cr.calls)
    cr._restage_connections("t1", now=9_900)
    assert [argv[3] for argv, _ in cr.calls[n:]] == ["salesforce"]
    assert json.loads(other.read_text())["expires_at"] == 1
    cr._restage_connections("nobody", now=9_900)           # a turn holding nothing
    assert len(cr.calls) == n + 1


# ---- R15: a CLI older than sealed connections --------------------------------------------

def _old_cli(cr, monkeypatch, tmp_path, version=None):
    major, minor, patch = cr.CONNECTIONS_ZK_MIN_CLI
    version = version or (major, minor, patch - 1)
    monkeypatch.setattr(cr, "_canopy_cli_version", lambda **k: version)
    _cli(cr, monkeypatch, tmp_path, expires=10_000)


OLD_CLI_LINE = ('warn: connections.stage_failed agent=eva provider=* turn=t1 '
                'reason="canopy CLI too old for connections"')


def test_old_cli_logs_one_stage_failed_line(cr, monkeypatch, tmp_path):
    _listing(cr, monkeypatch, body={"providers": ["intacct", "salesforce"]})
    _old_cli(cr, monkeypatch, tmp_path)
    assert cr._stage_connections("r1", "t1", "eva") == frozenset()
    assert cr.logs == [OLD_CLI_LINE]               # one line for the turn, not one per provider
    assert cr.calls == [] and cr._CONN_HOLDS == {} # it is not run: it knows only the removed route


def test_an_old_cli_and_an_agent_without_connections_is_silent(cr, monkeypatch, tmp_path):
    _listing(cr, monkeypatch, body={"providers": []})
    _old_cli(cr, monkeypatch, tmp_path)
    cr._stage_connections("r1", "t1", "eva")
    assert cr.logs == [] and cr.calls == []


def test_an_agent_without_connections_never_asks_the_clis_version(cr, monkeypatch, tmp_path):
    _listing(cr, monkeypatch, body={"providers": []})
    asked = []
    monkeypatch.setattr(cr, "_canopy_cli_version", lambda **k: asked.append(1) or (0, 2, 1))
    cr._stage_connections("r1", "t1", "eva")
    assert asked == []


@pytest.mark.parametrize("version", ["the minimum", "newer", "unknown"])
def test_a_new_enough_or_unknown_cli_is_run(cr, monkeypatch, tmp_path, version):
    """Unknown: it could not say. It is not known to be older, and if it cannot work its
    own failure is the line, rather than a version guess costing the turn its token."""
    _listing(cr, monkeypatch, body={"providers": ["salesforce"]})
    major, minor, patch = cr.CONNECTIONS_ZK_MIN_CLI
    monkeypatch.setattr(cr, "_canopy_cli_version", lambda **k: {
        "the minimum": (major, minor, patch), "newer": (major, minor + 1, 0), "unknown": None}[version])
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    assert cr._stage_connections("r1", "t1", "eva") == {"salesforce"}
    assert len(cr.calls) == 1 and cr.logs == []


def test_an_adopted_turn_on_an_old_cli_keeps_no_file(cr, monkeypatch, tmp_path):
    _path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _path(tmp_path).write_text("{}")
    cr._reserve_adopted_connections("r1", "t1", "eva", ["salesforce"])
    _listing(cr, monkeypatch, body={"providers": ["salesforce"]})
    _old_cli(cr, monkeypatch, tmp_path)
    cr._adopt_connections("r1", "t1", "eva")
    assert cr.logs == [OLD_CLI_LINE] and cr.calls == []
    assert cr._CONN_HOLDS == {} and not _path(tmp_path).exists()


# ---- one heartbeat stage at a time, for each file -----------------------------------------

def _blocking_cli(cr, monkeypatch, tmp_path, *, code=0):
    """A CLI that blocks until `release` is set, as one holding a rotated token does."""
    started, release = threading.Event(), threading.Event()

    def run(argv, **kw):
        cr.calls.append((argv, kw))
        started.set()
        assert release.wait(5)
        if code == 0:
            p = _path(tmp_path, provider=argv[3])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"expires_at": 10_000}))
        return subprocess.CompletedProcess(argv, code, "", "")
    monkeypatch.setattr(cr.subprocess, "run", run)
    return started, release


def _in_thread(fn, *a, **k):
    t = threading.Thread(target=fn, args=a, kwargs=k, daemon=True)
    t.start()
    return t


def test_a_heartbeat_does_not_stage_over_the_turns_own_first_stage(cr, monkeypatch, tmp_path):
    """The lease thread starts before the first stage, whose file does not exist yet: the
    next beat would otherwise start a second CLI for the same token."""
    started, release = _blocking_cli(cr, monkeypatch, tmp_path)
    first = _in_thread(cr._stage_connection_token, "r1", "t1", "eva", "salesforce", now=0)
    assert started.wait(5)
    cr._restage_connections("t1", now=0)
    assert len(cr.calls) == 1
    release.set()
    first.join(5)
    assert not first.is_alive() and "t1" in cr._CONN_HOLDS


def test_heartbeats_do_not_stack_on_a_slow_restage(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    n = len(cr.calls)
    started, release = _blocking_cli(cr, monkeypatch, tmp_path)
    slow = _in_thread(cr._restage_connections, "t1", now=9_900)
    assert started.wait(5)
    cr._restage_connections("t1", now=9_900)
    assert len(cr.calls) == n + 1
    release.set()
    slow.join(5)
    assert not slow.is_alive()


def test_a_siblings_heartbeat_does_not_stack_on_the_same_file(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)
    cr._stage_connection_token("r1", "B", "eva", "salesforce", now=0)
    n = len(cr.calls)
    started, release = _blocking_cli(cr, monkeypatch, tmp_path)
    slow = _in_thread(cr._restage_connections, "A", now=9_900)
    assert started.wait(5)
    cr._restage_connections("B", now=9_900)
    assert len(cr.calls) == n + 1
    release.set()
    slow.join(5)


def test_a_slow_stage_of_one_provider_does_not_hold_back_another(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    cr._stage_connection_token("r1", "t1", "eva", "intacct", now=0)
    started, release = _blocking_cli(cr, monkeypatch, tmp_path)
    slow = _in_thread(cr._stage_connection_token, "r1", "t1", "eva", "salesforce", now=9_900, refresh=True)
    assert started.wait(5)
    n = len(cr.calls)
    other = _in_thread(cr._stage_connection_token, "r1", "t1", "eva", "intacct", now=9_900, refresh=True)
    assert _until(lambda: len(cr.calls) == n + 1)      # a different file: it goes ahead
    release.set()
    slow.join(5)
    other.join(5)


def test_a_turns_first_stage_still_asks_while_a_sibling_restages(cr, monkeypatch, tmp_path):
    """A first stage is this turn's check and its issue record. It is never skipped; two
    CLIs for one token settle it between them (one takes the lease, the other waits)."""
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "A", "eva", "salesforce", now=0)
    n = len(cr.calls)
    started, release = _blocking_cli(cr, monkeypatch, tmp_path)
    slow = _in_thread(cr._restage_connections, "A", now=9_900)
    assert started.wait(5)
    second = _in_thread(cr._stage_connection_token, "r1", "B", "eva", "salesforce", now=9_900)
    assert _until(lambda: len(cr.calls) == n + 2)
    release.set()
    slow.join(5)
    second.join(5)


def _until(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.mark.parametrize("how", ["finished", "failed", "not connected", "raised", "fresh file"])
def test_a_finished_stage_lets_the_next_heartbeat_run(cr, monkeypatch, tmp_path, how):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    if how == "fresh file":
        cr._restage_connections("t1", now=0)
    elif how == "raised":
        def run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 300)
        monkeypatch.setattr(cr.subprocess, "run", run)
        cr._restage_connections("t1", now=9_900)
    else:
        _cli(cr, monkeypatch, tmp_path, expires=10_000,
             code={"finished": 0, "failed": 1, "not connected": 3}[how])
        cr._restage_connections("t1", now=9_900)
    _cli(cr, monkeypatch, tmp_path, expires=20_000)
    n = len(cr.calls)
    cr._restage_connections("t1", now=19_900)
    assert len(cr.calls) == n + 1


def test_an_interrupt_lets_the_next_stage_run(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)

    def run(argv, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(cr.subprocess, "run", run)
    with pytest.raises(KeyboardInterrupt):
        cr._restage_connections("t1", now=9_900)
    _cli(cr, monkeypatch, tmp_path, expires=20_000)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    n = len(cr.calls)
    cr._restage_connections("t1", now=19_900)
    assert len(cr.calls) == n + 1


# ---- a slow re-stage never costs the turn its lease ---------------------------------------

def _beating(cr, monkeypatch):
    beats = []

    def api(method, path, body=None, **kw):
        if path.endswith("/heartbeat"):
            beats.append(path)
        return 200, {}
    monkeypatch.setattr(cr, "_api", api)
    monkeypatch.setattr(cr, "LEASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(cr, "_heartbeat_body", lambda ids: {"active_turn_ids": ids})
    monkeypatch.setattr(cr, "_restage_delegated_google", lambda: None)
    return beats


def _no_lease_threads():
    """Nothing still running to reach what a test's teardown is about to restore."""
    return not any(t.name.startswith(("lease-", "restage-")) for t in threading.enumerate())


def test_the_lease_keeps_beating_through_a_slow_restage(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=1)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    n = len(cr.calls)
    started, release = _blocking_cli(cr, monkeypatch, tmp_path)
    beats = _beating(cr, monkeypatch)
    passes = []
    real = cr._restage_connections
    monkeypatch.setattr(cr, "_restage_connections", lambda t: passes.append(t) or real(t))
    stop = cr._start_lease_renewal("r1", "t1")
    try:
        assert started.wait(5)
        seen = len(beats)
        assert _until(lambda: len(beats) >= seen + 3)   # the CLI is still blocked
        assert len(cr.calls) == n + 1                   # and only one was started
        assert passes == ["t1"]                         # by the one pass still running
    finally:
        stop.set()
        release.set()
        assert _until(_no_lease_threads)


def test_the_lease_restages_again_after_a_restage_ends(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=1)
    cr._stage_connection_token("r1", "t1", "eva", "salesforce", now=0)
    n = len(cr.calls)
    _beating(cr, monkeypatch)
    stop = cr._start_lease_renewal("r1", "t1")
    try:
        assert _until(lambda: len(cr.calls) >= n + 2)   # the file stays near expiry
    finally:
        stop.set()
        assert _until(_no_lease_threads)


def test_no_thread_for_a_restage_does_not_stop_the_lease(cr, monkeypatch):
    """Thread exhaustion: `start()` raises RuntimeError, on a thread that must keep beating."""
    real = threading.Thread

    class Starved(real):
        def start(self):
            if self.name.startswith("restage-"):
                raise RuntimeError("can't start new thread")
            super().start()
    monkeypatch.setattr(cr.threading, "Thread", Starved)
    beats = _beating(cr, monkeypatch)
    stop = cr._start_lease_renewal("r1", "t1")
    try:
        assert _until(lambda: len(beats) >= 3)
        assert any("re-stage" in m and "can't start new thread" in m for m in cr.logs)
    finally:
        stop.set()
        assert _until(_no_lease_threads)
