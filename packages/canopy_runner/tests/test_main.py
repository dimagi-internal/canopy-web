import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from canopy_runner import emdash
from canopy_runner import main as main_mod
from canopy_runner.client import ClientError
from canopy_runner.config import Config
from canopy_runner.main import _fire_due_schedules, run_once


@pytest.fixture(autouse=True)
def _wake_off(monkeypatch):
    """Keep the RC3 wake listener inert so `main()`'s poll wait falls back to the
    plain time.sleep these loop tests patch to break the loop. Without this, an env
    that HAS websocket-client installed would give `main()` a live wake channel, the
    wait would block on the Event instead of time.sleep, the patched break would
    never fire, and the loop would hang — the exact 6h CI hang, made env-dependent."""
    import threading

    import canopy_runner.wake as wake_mod

    class _InertWaker:
        def __init__(self, *a, **k):
            self.event = threading.Event()

        def start(self) -> bool:
            return False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(wake_mod, "WakeListener", _InertWaker)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    """A minimal emdash DB — just the read surface (tasks + projects). The runner never
    writes it; the surviving tests that take this fixture don't even read it (they
    monkeypatch the reads), they only need a valid emdash_db path."""
    path = tmp_path / "emdash4.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
          archived_at TEXT, last_interacted_at TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP NOT NULL, type TEXT DEFAULT 'task' NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return str(path)


class FakeClient:
    def __init__(self):
        self.heartbeats = []
        self.reported_sessions = []

    def heartbeat(self, runner_id, active_turn_ids, degraded=False, note="", **kw):
        self.heartbeats.append((runner_id, list(active_turn_ids), degraded, note))
        return {"status": "degraded" if degraded else "online"}

    def claim(self, runner_id, paused_agents=None):
        return None

    def report_sessions(self, runner_id, sessions, archived=None):
        self.reported_sessions.append((runner_id, sessions, archived))


def _cfg(db, tmp_path):
    return Config(
        base_url="http://x", token="t", runner_id="r-1", emdash_db=db,
        state_path=str(tmp_path / "state.json"),
    )


# --- scheduled turns: the loop wiring (the decision logic is tests/test_schedules.py) ---


def test_schedule_sync_failure_never_kills_the_loop(tmp_path):
    """The daemon runs unattended under launchd: an unhandled exception in the
    scheduling path would take down claiming and the inbox with it."""
    class Boom:
        def sync_schedules(self, runner_id):
            raise ClientError("500 from server")

    cfg = Config(base_url="http://x", token="t", runner_id="r-1", emdash_db="x",
                 state_path=str(tmp_path / "state.json"))
    _fire_due_schedules(cfg, Boom())  # must not raise


def test_global_pause_sentinel_blocks_schedule_firing(tmp_path, monkeypatch):
    """Paused = no work, no tokens. Firing while paused would queue scheduled turns
    that all execute the instant the runner resumes."""
    cfg_path = tmp_path / "runner.json"
    cfg_path.write_text(json.dumps({
        "base_url": "http://x", "token": "t", "runner_id": "r-1",
        "emdash_db": str(tmp_path / "e.db"), "poll_seconds": 1,
    }))
    (tmp_path / "PAUSED").touch()  # the sentinel main() watches, next to the config

    calls = []

    class RecordingClient:
        def __init__(self, base_url, token):
            pass

        def heartbeat(self, *a, **k):
            calls.append("heartbeat")
            return {}

        def sync_schedules(self, runner_id):
            calls.append("sync_schedules")
            return []

        def claim(self, *a, **k):
            calls.append("claim")
            return None

    class StopLoopError(Exception):
        pass

    def stop(_seconds):
        raise StopLoopError

    monkeypatch.setattr(main_mod, "Client", RecordingClient)
    monkeypatch.setattr(time, "sleep", stop)
    monkeypatch.setattr(sys, "argv", ["canopy-runner", "run", "--config", str(cfg_path)])

    with pytest.raises(StopLoopError):
        main_mod.main()

    assert "heartbeat" in calls  # still alive-but-idle to the control plane
    assert "sync_schedules" not in calls  # ...but fired nothing
    assert "claim" not in calls


def test_unpaused_loop_fires_schedules_every_poll(tmp_path, monkeypatch):
    """The counterpart to the pause test: prove the firing is actually wired into
    the poll (the pause assertion alone would pass if it were wired nowhere)."""
    cfg_path = tmp_path / "runner.json"
    cfg_path.write_text(json.dumps({
        "base_url": "http://x", "token": "t", "runner_id": "r-1",
        "emdash_db": str(tmp_path / "e.db"), "poll_seconds": 1,
    }))
    calls = []

    class RecordingClient:
        def __init__(self, base_url, token):
            pass

        def heartbeat(self, *a, **k):
            return {}

        def sync_schedules(self, runner_id):
            calls.append(runner_id)
            return []

        def claim(self, *a, **k):
            return None

    class StopLoopError(Exception):
        pass

    monkeypatch.setattr(main_mod, "Client", RecordingClient)
    monkeypatch.setattr(time, "sleep", lambda _s: (_ for _ in ()).throw(StopLoopError()))
    monkeypatch.setattr(sys, "argv", ["canopy-runner", "run", "--config", str(cfg_path)])

    with pytest.raises(StopLoopError):
        main_mod.main()

    assert calls == ["r-1"]  # synced on the poll — the poll IS the tick


def test_broken_scheduling_does_not_crash_the_tick(db, tmp_path, monkeypatch, caplog):
    """A scheduling dependency failure — e.g. canopy_cron not installed in the
    laptop daemon's env — must disable ONLY scheduling. Claiming and the inbox
    keep running. The `from . import schedules` lives INSIDE _fire_due_schedules'
    guard for exactly this reason: the import is the likeliest failure, and it
    must not escape and take down the whole run_once cycle."""
    import sys

    monkeypatch.setitem(sys.modules, "canopy_runner.schedules", None)
    caplog.set_level("WARNING")

    _fire_due_schedules(_cfg(db, tmp_path), FakeClient(), paused=set())  # must NOT raise

    assert "scheduling unavailable" in caplog.text.lower()


# --------------------------------------------------------------------------------------
# drain_one — the "take a single turn" primitive
# --------------------------------------------------------------------------------------

def _cdp_cfg(tmp_path):
    return Config(
        base_url="http://x", token="t", runner_id="r-1",
        emdash_db=str(tmp_path / "e.db"),
    )


class _CdpClient:
    def __init__(self, turn):
        self._turn = turn
        self.beats = 0
        self.claims = 0

    def heartbeat(self, runner_id, active, host="", **kw):
        self.beats += 1

    def claim(self, runner_id, paused_agents=None):
        self.claims += 1
        return self._turn


def test_drain_one_runs_exactly_one_turn_without_polling(monkeypatch, tmp_path):
    """The single-turn primitive: heartbeat → claim ONE → execute, and NEVER poll the
    inbox or fire schedules — those side effects are exactly what --once has and
    --drain-one deliberately avoids, so a single turn can't spawn work you didn't ask for."""
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: True)
    monkeypatch.setattr("canopy_runner.cdp_control.host_id", lambda: "u@h")
    monkeypatch.setattr(main_mod, "_paused_agents", lambda c: set())
    monkeypatch.setattr(main_mod, "_maybe_check_inboxes",
                        lambda *a, **k: pytest.fail("drain_one must NOT poll the inbox"))
    monkeypatch.setattr(main_mod, "_fire_due_schedules",
                        lambda *a, **k: pytest.fail("drain_one must NOT fire schedules"))
    seen = {}
    monkeypatch.setattr("canopy_runner.execute.execute_turn",
                        lambda cfg, client, rid, turn: seen.update(turn=turn) or f"reused:{turn['id']}")

    client = _CdpClient({"id": "t-9", "agent_slug": "eva"})
    assert main_mod.drain_one(_cdp_cfg(tmp_path), client) == "reused:t-9"
    assert seen["turn"]["id"] == "t-9"
    assert client.beats == 1 and client.claims == 1


def test_drain_one_idle_when_nothing_queued(monkeypatch, tmp_path):
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: True)
    monkeypatch.setattr("canopy_runner.cdp_control.host_id", lambda: "u@h")
    monkeypatch.setattr(main_mod, "_paused_agents", lambda c: set())
    monkeypatch.setattr("canopy_runner.execute.execute_turn",
                        lambda *a, **k: pytest.fail("nothing queued — must not execute"))
    assert main_mod.drain_one(_cdp_cfg(tmp_path), _CdpClient(None)) == "idle"


def test_drain_one_honours_per_agent_pause_via_claim(monkeypatch, tmp_path):
    """Per-agent pauses ARE respected — the paused set is passed to claim, which the
    server uses to exclude that agent's turns."""
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: True)
    monkeypatch.setattr("canopy_runner.cdp_control.host_id", lambda: "u@h")
    monkeypatch.setattr(main_mod, "_paused_agents", lambda c: {"eva"})
    passed = {}

    class C(_CdpClient):
        def claim(self, runner_id, paused_agents=None):
            passed["paused"] = paused_agents
            return None

    main_mod.drain_one(_cdp_cfg(tmp_path), C(None))
    assert passed["paused"] == ["eva"]


def test_drain_one_refuses_to_claim_when_cdp_down(monkeypatch, tmp_path):
    """The self-heal reaches the single-turn primitive too: claiming with emdash down
    would immediately fail (burn) the turn, so drain_one refuses instead. It must ALSO
    report ready=False on this heartbeat — otherwise the server keeps showing the runner
    as "ready" while it can't actually execute anything."""
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: False)
    monkeypatch.setattr("canopy_runner.cdp_control.host_id", lambda: "u@h")
    monkeypatch.setattr(main_mod, "_paused_agents", lambda c: set())

    class C(_CdpClient):
        def __init__(self):
            super().__init__(None)
            self.degraded = None
            self.ready = None
            self.ready_note = None

        def heartbeat(self, runner_id, active, degraded=False, note="", host="",
                      ready=True, ready_note="", code_branch=""):
            self.beats += 1
            self.degraded = degraded
            self.ready = ready
            self.ready_note = ready_note

        def claim(self, runner_id, paused_agents=None):
            pytest.fail("must NOT claim a turn while CDP is down")

    c = C()
    assert main_mod.drain_one(_cdp_cfg(tmp_path), c) == "cdp_down"
    assert c.degraded is True  # surfaced as degraded to the control plane
    assert c.ready is False  # must NOT default to True while CDP is unreachable
    assert c.ready_note  # a human-readable reason accompanies it


# --------------------------------------------------------------------------------------
# CDP preflight — don't claim (and burn) a turn when emdash's CDP is unreachable
# --------------------------------------------------------------------------------------


class _CdpLoopClient:
    """A CDP-path fake: records heartbeats (degraded flag) and how many times claim ran."""

    def __init__(self, turns=None):
        self.turns = list(turns or [])
        self.claims = 0
        self.heartbeats = []

    def heartbeat(self, runner_id, active, degraded=False, note="", host="",
                  ready=True, ready_note="", code_branch=""):
        self.heartbeats.append({"degraded": degraded, "note": note,
                                "ready": ready, "ready_note": ready_note})

    def report_sessions(self, runner_id, sessions, archived=None):
        pass

    def sync_schedules(self, runner_id):
        return []

    def claim(self, runner_id, paused_agents=None):
        self.claims += 1
        return self.turns.pop(0) if self.turns else None


def _cdp_loop_cfg(tmp_path):
    # no mailboxes so the inbox poll is a no-op.
    return Config(
        base_url="http://x", token="t", runner_id="r-1",
        emdash_db=str(tmp_path / "e.db"),
    )


@pytest.fixture(autouse=True)
def _reset_cdp_throttle():
    """The CDP-down throttle is module-level (one WARNING per outage over the loop's
    lifetime) — reset it around every test so ordering can't leak signalled state."""
    main_mod._reset_cdp_health_state()
    yield
    main_mod._reset_cdp_health_state()


def _stub_cdp(monkeypatch, healthy):
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: healthy)
    monkeypatch.setattr("canopy_runner.cdp_control.host_id", lambda: "u@h")
    monkeypatch.setattr(main_mod.emdash, "list_open_sessions", lambda db: [])


def test_cdp_healthy_claims_and_executes(monkeypatch, tmp_path):
    _stub_cdp(monkeypatch, healthy=True)
    monkeypatch.setattr("canopy_runner.execute.execute_turn",
                        lambda cfg, client, rid, turn: f"created:{turn['id']}:task")
    client = _CdpLoopClient(turns=[{"id": "t-1", "agent_slug": "eva"}])
    assert run_once(_cdp_loop_cfg(tmp_path), client) == "created:t-1:task"
    assert client.claims == 1
    assert client.heartbeats[-1]["degraded"] is False  # healthy heartbeat


def test_cdp_unhealthy_skips_claim_and_burns_nothing(monkeypatch, tmp_path):
    """The core acceptance criterion: with emdash down a tick claims ZERO turns and
    produces ZERO failed turns — queued turns stay queued."""
    _stub_cdp(monkeypatch, healthy=False)
    monkeypatch.setattr("canopy_runner.execute.execute_turn",
                        lambda *a, **k: pytest.fail("must NOT execute while CDP is down"))
    client = _CdpLoopClient(turns=[{"id": "t-1", "agent_slug": "eva"}])
    assert run_once(_cdp_loop_cfg(tmp_path), client) == "cdp_down"
    assert client.claims == 0  # nothing claimed -> nothing burned
    assert client.heartbeats[-1]["degraded"] is True  # surfaced as degraded, every tick


def test_cdp_unhealthy_heartbeat_reports_not_ready(monkeypatch, tmp_path):
    """The proactive case this whole feature exists for: a CDP-down runner must read as
    NOT ready on the server, not just "degraded"."""
    _stub_cdp(monkeypatch, healthy=False)
    client = _CdpLoopClient()
    run_once(_cdp_loop_cfg(tmp_path), client)
    hb = client.heartbeats[-1]
    assert hb["ready"] is False
    assert hb["ready_note"]  # a human-readable reason accompanies it


def test_cdp_down_still_polls_inbox_and_schedules(monkeypatch, tmp_path):
    """Inbound work must keep ENQUEUING while CDP is down — only the claim is gated."""
    _stub_cdp(monkeypatch, healthy=False)
    ran = {}
    monkeypatch.setattr(main_mod, "_maybe_check_inboxes",
                        lambda *a, **k: ran.__setitem__("inbox", True))
    monkeypatch.setattr(main_mod, "_fire_due_schedules",
                        lambda *a, **k: ran.__setitem__("sched", True))
    run_once(_cdp_loop_cfg(tmp_path), _CdpLoopClient())
    assert ran == {"inbox": True, "sched": True}


def test_cdp_recovery_drains_the_backlog(monkeypatch, tmp_path):
    """When emdash comes back, the next tick claims + drains the queued turn normally."""
    _stub_cdp(monkeypatch, healthy=False)
    monkeypatch.setattr("canopy_runner.execute.execute_turn",
                        lambda cfg, client, rid, turn: f"reused:{turn['id']}")
    cfg = _cdp_loop_cfg(tmp_path)
    client = _CdpLoopClient(turns=[{"id": "t-1", "agent_slug": "eva"}])

    assert run_once(cfg, client) == "cdp_down"  # down: the queued turn waits
    assert client.claims == 0

    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: True)  # emdash returns
    assert run_once(cfg, client) == "reused:t-1"  # back: drains the backlog
    assert client.claims == 1


def test_cdp_down_warning_is_throttled_to_one(monkeypatch, tmp_path, caplog):
    """A single throttled surface signal after sustained downtime — NOT per-tick spam.
    The degraded heartbeat still fires every tick (machine signal); the WARNING fires once."""
    _stub_cdp(monkeypatch, healthy=False)
    cfg = _cdp_loop_cfg(tmp_path)
    client = _CdpLoopClient()
    caplog.set_level("WARNING", logger="canopy_runner")
    ticks = main_mod.CDP_DOWN_SIGNAL_TICKS + 5
    for _ in range(ticks):
        run_once(cfg, client)
    warns = [r for r in caplog.records if "CDP unreachable" in r.getMessage()]
    assert len(warns) == 1  # ONE loud log despite many down ticks
    assert len(client.heartbeats) == ticks  # ...but a degraded heartbeat every tick
    assert all(h["degraded"] for h in client.heartbeats)


def test_cdp_recovery_logs_once_and_rearms(monkeypatch, tmp_path, caplog):
    """After recovery the throttle re-arms, so a SECOND outage warns again (not muted forever)."""
    cfg = _cdp_loop_cfg(tmp_path)
    client = _CdpLoopClient()
    caplog.set_level("INFO", logger="canopy_runner")

    _stub_cdp(monkeypatch, healthy=False)
    for _ in range(main_mod.CDP_DOWN_SIGNAL_TICKS):
        run_once(cfg, client)
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: True)
    run_once(cfg, client)  # recovery — logs "healthy again" and re-arms the throttle
    assert any("healthy again" in r.getMessage() for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: False)
    for _ in range(main_mod.CDP_DOWN_SIGNAL_TICKS):
        run_once(cfg, client)
    assert sum("CDP unreachable" in r.getMessage() for r in caplog.records) == 1  # warns again


def test_maybe_report_sessions_throttled(db, tmp_path, monkeypatch):
    """When nothing changes, the EXPENSIVE session report (up to session_tail_count
    transcript reads + POST) runs at most every session_report_seconds — the cheap
    change-check (list_open_sessions) runs every tick, but the report is heartbeat-only
    absent a real change. (A transcript that GREW would report immediately — that's the
    live path, covered in test_session_report_live.)"""
    from canopy_runner import main as m
    from canopy_runner import transcript
    cfg = _cfg(db, tmp_path)  # session_report_seconds defaults to 10
    calls = []
    monkeypatch.setattr(emdash, "list_open_sessions", lambda p, limit=30: [])  # no sessions -> no change
    monkeypatch.setattr(transcript, "attach_recent_tail", lambda *a, **k: calls.append(1))
    m._last_session_report = 0.0
    clock = [1000.0]
    now = lambda: clock[0]
    client = FakeClient()
    m._maybe_report_sessions(cfg, client, now_fn=now)   # first tick -> reports
    m._maybe_report_sessions(cfg, client, now_fn=now)   # within window -> skipped
    clock[0] += cfg.session_report_seconds + 1
    m._maybe_report_sessions(cfg, client, now_fn=now)   # window elapsed -> reports
    assert len(calls) == 2
