"""Runner main loop (CDP executor).

One iteration (run_once):
  1. preflight emdash's CDP health — unhealthy => degraded heartbeat, skip the
     claim (queued turns wait rather than being claimed-then-burned), still poll
     the inbox + fire schedules so inbound work keeps enqueuing
  2. heartbeat (with the macOS host, for session-reuse ownership)
  3. report the open emdash sessions the phone can continue (throttled)
  4. claim at most one queued turn and route it to an emdash session (reuse or
     create) via execute.execute_turn

Agent/project turns finish synchronously — the runner owns the routing lifecycle;
the work continues in the visible emdash session — so there is NO injection state to
track and NO emdash-DB write. CHAT turns are the exception: they stay EXECUTING while
the agent works and are pumped tick by tick (chat_pump.pump_chat_bridges), because their reply
has to be carried back into the ledger and an agent turn lasts minutes, which is far
longer than a tick may block. The only emdash-DB access is the two READ-ONLY queries in
`emdash.py` (task_state, list_open_sessions), whose column dependencies are
verified out-of-band by `canopy_runner verify-emdash`.

This module is the LOOP and the CLI. Each subsystem it drives owns its own
module, so "where does X live" has one answer:
  sessions.py    reporting the open emdash sessions this box can see
  chat_pump.py   driving a chat turn to completion across ticks
  streams.py     live transcript push for sessions a viewer is watching
  hooks.py       hook tool-events, and answering a blocked agent's dialog
  cancel.py      the shared "stop this turn" set
  failure_log.py repeat-aware logging for the best-effort retry paths
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
from pathlib import Path

from . import chat_bridge, chat_pump, emdash, hooks, inbox_due, sessions, streams
from . import __version__, provenance
from .cancel import CANCELLED_TURNS
from .client import Client, ClientError
from .config import Config

logger = logging.getLogger("canopy_runner")

# CDP-down throttle. The runner is otherwise stateless (no state file — see
# run_once), but the human-facing "emdash is down" WARNING must fire ONCE per
# outage, not per tick, so this small counter lives at module scope for the loop
# process's lifetime. The per-tick machine signal is the degraded heartbeat (a status
# field, not spam); this gates only the one loud log. Emit it after this many
# consecutive unhealthy ticks so a brief emdash restart (a tick or two) doesn't cry wolf.
CDP_DOWN_SIGNAL_TICKS = 3
_cdp_down_ticks = 0
_cdp_down_signalled = False


def _reset_cdp_health_state() -> None:
    """Clear the CDP-down throttle (on recovery, and between tests)."""
    global _cdp_down_ticks, _cdp_down_signalled
    _cdp_down_ticks = 0
    _cdp_down_signalled = False


def _pause_paths(cfg: Config) -> tuple[Path, Path]:
    """(sentinel, mirror). The sentinel is the human-facing control surface the
    menu-bar app toggles; the mirror is what we last knew it to be, so a CHANGE can
    be told from a steady state across restarts."""
    d = Path(cfg.state_path).parent if cfg.state_path else Path.home() / ".canopy"
    return d / "PAUSED", d / ".pause-mirror"


def reconcile_pause(cfg, client, server_paused: bool) -> bool:
    """Keep the local sentinel and the server's `paused` as ONE state, and return
    what is now in force.

    The rule: a local CHANGE is a command, anything else defers to the server.

        local != mirror  ->  the human just toggled the file: push it up.
        otherwise        ->  the server is truth: write the file to match.

    Both directions are needed and neither is symmetric with the other. Without
    the edge, a remote pause would be lifted five seconds later by a box whose
    file happens to be absent — the level-report clobber. Without the mirror, we
    could not tell "the human just deleted the file" (an unpause command) from
    "the file was never there and the pause came from the server" (obey it), and
    the local file could then only ever ADD a pause, which breaks the menu-bar
    toggle's off position.

    The mirror records what was last SUCCESSFULLY SYNCED, never merely what is in
    force. That distinction is load-bearing: if a failed push still advanced the
    mirror, the next tick would see local == mirror, fall into the server-wins
    branch, and DELETE the sentinel the human just dropped — silently undoing a
    pause because one HTTP call failed. Leaving it unmoved makes the edge persist
    and retry on the next tick instead.

    On any error, err toward PAUSED (`local or server_paused`). Pausing is the
    conservative direction here: the cost of wrongly pausing is idleness someone
    notices, and the cost of wrongly running is tokens on an account that must not
    spend them.
    """
    sentinel, mirror = _pause_paths(cfg)
    try:
        local = sentinel.exists()
        synced = mirror.exists()
    except OSError:
        # No readable local signal at all — the server's answer is the only one
        # there is, and it is the copy `claim_next_turn` enforces anyway.
        return server_paused

    if local != synced:
        # A local EDGE — the human toggled the file. That is a command, and it is
        # the newest intent, so it wins this tick even against a disagreeing server.
        try:
            client.set_paused(cfg.runner_id, local,
                              note="paused locally (~/.canopy/PAUSED)" if local else "")
            _write_flag(mirror, local)
            logger.warning("pause: local sentinel %s — pushed to the control plane",
                           "set" if local else "cleared")
            return local
        except Exception:  # noqa: BLE001 — never kill the loop over this
            # Mirror deliberately NOT advanced: retry on the next tick.
            logger.warning("pause: could not push the local change; erring toward "
                           "paused until it lands", exc_info=True)
            return local or server_paused

    if local != server_paused:
        # No local change, so the server is truth — write it back down so the
        # menu-bar app shows the real state rather than a stale toggle.
        try:
            _write_flag(sentinel, server_paused)
            _write_flag(mirror, server_paused)
            logger.info("pause: applied the control plane's state locally (paused=%s)",
                        server_paused)
        except OSError:
            logger.warning("pause: could not mirror the server state to %s", sentinel)
            return local or server_paused
    return server_paused


def _write_flag(path: Path, present: bool) -> None:
    """A boolean stored as a file's existence — the shape the menu-bar app and
    `touch ~/.canopy/PAUSED` already speak."""
    if present:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    else:
        path.unlink(missing_ok=True)


def _paused_agents(cfg: Config) -> set[str]:
    """Per-agent pause: agent slugs with a `PAUSED.<slug>` sentinel next to the state
    file (dropped by the menu-bar app). Distinct from the global `PAUSED` file, which
    halts everything. A paused agent's inbox is skipped and its queued turns are not
    claimed (the server excludes them), so its work simply waits until resumed."""
    d = Path(cfg.state_path).parent if cfg.state_path else Path.home() / ".canopy"
    try:
        return {p.name[len("PAUSED."):] for p in d.glob("PAUSED.*")}
    except OSError:
        return set()


def _maybe_check_inboxes(cfg: Config, client: Client, now_fn=time.time,
                         paused: set[str] | None = None) -> None:
    """Email trigger, on two clocks: the doorbell and the timer.

    A ``check_inbox`` control frame from canopy-web means Gmail just told the
    server that mailbox changed — that mailbox is checked NOW, bypassing the
    timer. Everything else still runs on ``inbox_poll_seconds`` (300s), which is
    no longer the delivery mechanism but remains the AUDITOR: a message the
    timer finds is a message push failed to ring for, and the ``discovered_by``
    tag is what lets the server say so.

    Stamps are per mailbox, so a doorbell for eva never defers hal's timer — the
    quiet mailboxes are exactly where a silently-broken watch would hide.

    Best-effort — a failing inbox (auth expired) logs and is skipped, never
    crashes the loop. Paused agents are skipped so no new email turns are
    enqueued for them.
    """
    if not getattr(cfg, "mailboxes", None):
        return
    stamp = Path(cfg.state_path).with_name("inbox-last.json") if cfg.state_path else Path("inbox-last.json")
    try:
        stamps = json.loads(stamp.read_text())
        if not isinstance(stamps, dict):
            stamps = {}
    except (OSError, ValueError):
        stamps = {}

    rung = inbox_due.take_pending()
    now = now_fn()
    due_slugs = inbox_due.due(
        cfg.mailboxes, stamps, now=now, interval=cfg.inbox_poll_seconds, rung=rung
    )
    if not due_slugs:
        return
    rung_slugs = {
        slug for slug in due_slugs
        if (cfg.mailboxes[slug].get("account") or "").strip().lower() in rung
    }

    from . import inbox as inbox_mod
    cap = getattr(cfg, "inbox_max_threads", 8)
    for agent in due_slugs:
        box = cfg.mailboxes[agent]
        if paused and agent in paused:
            continue
        try:
            res = inbox_mod.check_inbox(
                client, agent, mailbox=box["account"], gog_client=box["client"],
                query=box.get("query", inbox_mod.DEFAULT_QUERY), max_threads=cap,
                discovered_by=inbox_due.discovered_by(agent, rung_slugs),
            )
            n_new, n_seen = len(res["new"]), len(res["seen"])
            n_skip = len(res.get("skipped", []))
            # Log EVERY poll, not just ones that enqueue — otherwise a healthy poll that
            # finds nothing new is silent and you can't tell polling is happening at all.
            # `skipped` = unread threads whose newest message is the agent's own reply
            # (already had the last word), suppressed so a re-marked-unread thread can't
            # manufacture a turn with no new inbound.
            logger.info("inbox[%s]: %s — %d unread (%d NEW -> session, %d already tracked, "
                        "%d skipped: agent's own reply)",
                        agent, "RUNG" if agent in rung_slugs else "polled",
                        n_new + n_seen + n_skip, n_new, n_seen, n_skip)
        except Exception as exc:  # noqa: BLE001 — one bad inbox never kills the loop
            logger.warning("inbox check for %s failed: %s", agent, exc)
        finally:
            # Stamp per mailbox, and stamp even on failure: a mailbox whose auth
            # has expired would otherwise be retried every single tick, turning
            # one broken credential into a subprocess storm.
            stamps[agent] = now_fn()
    try:
        stamp.write_text(json.dumps(stamps))
    except OSError:
        pass


def _maybe_rearm_watches(cfg: Config, client: Client, now_fn=time.time) -> None:
    """Keep each mailbox's Gmail watch armed, and report the expiry.

    Rides the inbox tick because it needs nothing else, and re-arms 24h early
    against a 7-day ceiling — six chances to succeed before push actually lapses.
    NOT the delivery path: arming is a weekly registration call, while delivery is
    Gmail -> Pub/Sub -> canopy-web -> the check_inbox doorbell, in seconds.

    Off unless `gmail_watch_topic` is set, so a box with no Pub/Sub topic
    provisioned behaves exactly as before. Best-effort per mailbox: one failing
    mailbox (revoked grant, missing gog client) is logged and skipped, never
    raised — the server's watch.expired row is what makes it loud.
    """
    if not getattr(cfg, "mailboxes", None):
        return
    from . import gmail_watch

    # Topics come from canopy-web (each workspace's InboundPushConfig), so a
    # tenant configures its topic once in the UI rather than by editing
    # runner.json on every box. The local `gmail_watch_topic` remains as a
    # fallback for a box running against a server that has no config yet.
    local_topic = getattr(cfg, "gmail_watch_topic", "") or ""
    try:
        served = {row.get("address", "").lower(): row.get("watch_topic", "")
                  for row in client.runner_mailboxes()}
    except Exception as exc:  # noqa: BLE001 — a config read never breaks the tick
        logger.debug("runner-mailboxes fetch failed (%s); using local topic", exc)
        served = {}
    if not served and not local_topic:
        return

    state_path = (Path(cfg.state_path).with_name("gmail-watch.json")
                  if cfg.state_path else Path("gmail-watch.json"))
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    now = dt.datetime.now(dt.UTC)
    changed = False
    for agent, box in cfg.mailboxes.items():
        address = box.get("account") or ""
        if not address:
            continue
        try:
            prev = dt.datetime.fromisoformat(state[address]) if state.get(address) else None
        except (TypeError, ValueError):
            prev = None
        if not gmail_watch.due(prev, now=now):
            continue
        topic = served.get(address.lower(), "") or local_topic
        if not topic:
            continue
        try:
            expires = gmail_watch.arm(address, box.get("client") or "canopy", topic)
        except Exception as exc:  # noqa: BLE001 — one mailbox never breaks the tick
            logger.warning("gmail watch re-arm failed for %s: %s", address, exc)
            continue
        state[address] = expires.isoformat()
        changed = True
        logger.info("gmail watch armed for %s -> expires %s", address, expires.isoformat())
        try:
            client.report_watch(address, expires)
        except Exception as exc:  # noqa: BLE001 — reporting is not the arming
            logger.warning("gmail watch report failed for %s: %s", address, exc)
    if changed:
        try:
            state_path.write_text(json.dumps(state))
        except OSError:
            pass


def _fire_due_schedules(cfg: Config, client: Client, paused: set[str] | None = None) -> None:
    """Scheduled-turn trigger: sync the schedules this runner may fire, evaluate each
    cron locally, and report any due slot so the server materializes the turn.

    Unthrottled on purpose — unlike the inbox (a subprocess per mailbox), this is one
    HTTP GET, the same cost class as the claim it rides alongside, and the poll IS the
    tick: throttling it would just add latency to every slot. Best-effort — a failing
    sync (server down, token expired) logs and is skipped, never crashes the loop.

    Only reached when NOT globally paused: main()'s pause sentinel `continue`s before
    run_once, so a paused runner never fires (which would queue turns that all execute
    the instant it resumes). Per-agent pause is honored inside check_schedules.
    """
    now = dt.datetime.now(dt.UTC)
    try:
        # Import INSIDE the guard, not above it: canopy_cron is scheduling's ONLY
        # dependency, and a missing/broken one (an un-synced laptop env, a bad
        # install) must disable scheduling alone — not crash claiming and the inbox
        # with it. The import is the most likely failure, so it has to be caught too.
        from . import schedules as schedules_mod
        schedules_mod.check_schedules(client, cfg.runner_id, now=now, paused=frozenset(paused or ()))
    except Exception as exc:  # noqa: BLE001 — scheduling never kills claiming or the inbox
        logger.warning("scheduling unavailable this tick (claiming + inbox continue): %s", exc)


def _mark_in_flight(cfg: Config, *, extra: int = 0) -> None:
    """Publish how much work this runner is carrying, for the auto-updater.

    Chat turns are bridged ACROSS ticks (chat_bridge.IN_FLIGHT), so the count is
    not derivable from anything the updater can see from outside the process —
    hence a marker file rather than, say, asking the server which turns it thinks
    are executing (that answer lags, and is wrong in exactly the crash cases)."""
    from . import update as update_mod

    update_mod.mark_busy(cfg, len(chat_bridge.IN_FLIGHT) + extra)


def _claim_and_execute(cfg: Config, client: Client, paused: set) -> str:
    """Claim at most one eligible turn and route it to an emdash session. The shared
    core of both the loop's iteration and the single-turn primitive, so they can't
    drift. Returns reused:/created:/failed:<id> or "idle" when nothing is queued."""
    from . import execute, readiness

    try:
        turn = client.claim(cfg.runner_id, paused_agents=sorted(paused))
    except ClientError as exc:
        logger.warning("claim failed: %s", exc)
        return "idle"
    if turn is None:
        return "idle"
    # Hold the in-flight marker across the WHOLE claim→execute window, not just the
    # per-tick count: the auto-updater restarts this daemon, and an agent turn is
    # routed synchronously inside this call. Restarting here would leave the turn
    # EXECUTING with nothing to finish it, waiting out the lease sweep.
    _mark_in_flight(cfg, extra=1)
    try:
        return execute.execute_turn(
            cfg, client, cfg.runner_id, turn,
            cancel_check=lambda tid: tid in CANCELLED_TURNS,
        )
    except Exception as exc:  # noqa: BLE001 — one turn must never kill the loop
        logger.exception("execute_turn crashed for %s", turn.get("id"))
        note = f"runner execute crashed: {exc}"
        readiness.mark_failed(cfg, note)
        try:
            client.fail_turn(turn["id"], note)
        except ClientError:
            pass
        return f"failed:{turn.get('id')}"
    finally:
        # Evict once the turn is done, regardless of outcome — CANCELLED_TURNS is a
        # transient "stop now" signal, not a durable per-turn record; leaving an id in
        # it forever would wrongly mark any FUTURE turn that reused the same id (turn
        # ids aren't reused today, but leaking membership is a latent footgun either
        # way — and it just keeps a module-level set growing unbounded for the life
        # of the process).
        CANCELLED_TURNS.discard(turn["id"])
        _mark_in_flight(cfg)


def run_once(cfg: Config, client: Client) -> str:
    """One loop iteration: preflight emdash's CDP health → heartbeat (with macOS host, for
    reuse ownership) → pump in-flight chat replies → claim one turn → route it to an
    emdash session (reuse or create). Agent/project turns finish synchronously (the
    runner owns the routing lifecycle; work continues in the visible session); a chat
    turn is registered with the pump and finishes on a later tick, when its transcript
    says the agent handed the floor back.

    Self-heal: the runner CONNECTS to emdash, it never launches it, so a closed/crashed
    emdash (or one launched without --remote-debugging-port) can't run work. If we claimed
    anyway, execute would hit the CDP-connect failure and fail the turn — and a failed turn
    is NOT auto-re-claimed, so one outage burned a turn per hit agent (real incident
    2026-07-17: 11 turns). So we PREFLIGHT: an unhealthy CDP skips the claim for this tick,
    leaving queued turns queued to auto-drain when emdash returns. Inbox + schedule polling
    still run (inbound work keeps ENQUEUING); only the claim is gated."""
    from . import readiness
    from .cdp_control import cdp_healthy, host_id

    global _cdp_down_ticks, _cdp_down_signalled
    healthy = cdp_healthy(port=cfg.cdp_port)
    host = host_id()
    if healthy:
        if _cdp_down_signalled:
            logger.info("emdash CDP healthy again on :%s — resuming claims after %d down tick(s)",
                        cfg.cdp_port, _cdp_down_ticks)
        _reset_cdp_health_state()
        _ready, _rnote = readiness.compute(cfg)
        # Report in-flight chat turns so the server renews their lease: a bridged
        # turn now outlives the tick that started it, and an unrenewed lease is swept.
        # Report the repos this box can drive alongside readiness — same question
        # ("what can this runner do right now"), same tick. The degraded and paused
        # heartbeats below deliberately omit it: omission is a no-op server-side,
        # and a runner that isn't claiming has no use for a refreshed list.
        me = client.heartbeat(cfg.runner_id, sorted(chat_bridge.IN_FLIGHT), host=host,
                              ready=_ready, ready_note=_rnote,
                              projects=sessions.reported_projects(cfg))
    else:
        _cdp_down_ticks += 1
        # Degraded heartbeat EVERY unhealthy tick — the machine-readable surface signal the
        # control plane + menu-bar app read ("alive but can't execute"). It's a status field,
        # overwritten each tick, so it is not spam.
        me = client.heartbeat(cfg.runner_id, sorted(chat_bridge.IN_FLIGHT), degraded=True,
                              note=f"emdash CDP unreachable on :{cfg.cdp_port} — not claiming",
                              host=host, ready=False,
                              ready_note=f"emdash CDP unreachable on :{cfg.cdp_port}")
        # ...and ONE loud WARNING after sustained downtime (not per tick), for the human log.
        if _cdp_down_ticks >= CDP_DOWN_SIGNAL_TICKS and not _cdp_down_signalled:
            logger.warning(
                "emdash CDP unreachable on 127.0.0.1:%s for %d consecutive ticks — SKIPPING "
                "the claim so queued turns wait instead of failing. Launch emdash with "
                "--remote-debugging-port=%s; the backlog auto-drains when it returns.",
                cfg.cdp_port, _cdp_down_ticks, cfg.cdp_port)
            _cdp_down_signalled = True

    # Before the reports: an in-flight reply is the freshest thing on this box, and
    # finishing a turn here frees the session for the next message. Runs even while
    # CDP is down — the transcript keeps growing whether or not we can drive emdash.
    hooks.maybe_report_hooks()
    # The hook config is shared by the whole account and written by anything that
    # starts a listener, so it can be pointed away from us after startup. Checked
    # every tick rather than on the report's cadence: the report is gated on hooks
    # ARRIVING, which is the very thing a drifted config stops.
    hooks.ensure_hook_config()
    chat_pump.pump_chat_bridges(cfg, client)
    sessions.maybe_report_sessions(cfg, client)
    streams.sync_session_streams(cfg, client)
    streams.drain_backfills(cfg, client)

    # THE PAUSE. One state, settable from either end: reconcile the local sentinel
    # with the server's `paused` (read off the heartbeat response we just got), and
    # obey whatever is in force.
    #
    # Gating HERE, above the inbox and schedule triggers, is the point. The server
    # already refuses to hand a paused runner any turn, so routing is safe without
    # us — but both of those triggers ENQUEUE turns rather than executing them, so
    # a runner that kept polling while parked would build a backlog it cannot touch
    # and then stampede the instant it resumes. That is the same hazard the
    # per-agent pause names in `check_schedules`.
    #
    # It sits AFTER the in-flight reporting above, deliberately: a pause stops
    # STARTING work, it never abandons work already running.
    if reconcile_pause(cfg, client, bool((me or {}).get("paused"))):
        return "paused"

    paused = _paused_agents(cfg)
    # Inbound triggers run whether or not CDP is up, so inbound work still ENQUEUES while
    # emdash is down (it just waits, queued, until emdash is back). Only the claim is gated.
    _maybe_check_inboxes(cfg, client, paused=paused)
    # Keep the Gmail watches armed so push keeps being DELIVERED. Rides the same
    # tick; a no-op unless a topic is configured, and internally throttled to the
    # 24h-before-expiry window rather than firing every cycle.
    _maybe_rearm_watches(cfg, client)
    # Fleet-audit review ingestion was removed when Ada moved to Items: approving
    # an Item dispatches its work server-side (in the decide transaction), so there
    # is no resolved review for the runner to poll. DDD findings reviews are applied
    # by the DDD orchestrator, never here.
    _fire_due_schedules(cfg, client, paused=paused)
    if not healthy:
        return "cdp_down"  # nothing claimed -> nothing burned; queued turns stay queued
    return _claim_and_execute(cfg, client, paused)


def drain_one(cfg: Config, client: Client) -> str:
    """Take exactly ONE queued turn, then exit — the "take a single turn" primitive.

    Unlike --once (a full loop iteration), this does NOT poll the inbox or fire
    schedules, so it can only run a turn that is ALREADY queued (dispatch one from the
    composer/API first); it never enqueues or spawns work you didn't ask for. It also
    runs while the daemon is paused — the global PAUSED sentinel gates main()'s loop, not
    this — so you can take one turn with the fleet otherwise off. Per-agent pauses ARE
    honoured (the claim skips a paused agent's turns)."""
    from . import readiness
    from .cdp_control import cdp_healthy, host_id

    # Same self-heal as the loop: claiming with emdash down would immediately fail (=burn)
    # the turn. Refuse instead — the caller re-runs once emdash is back on its debug port.
    if not cdp_healthy(port=cfg.cdp_port):
        logger.warning("emdash CDP unreachable on :%s — refusing to claim a turn (it would "
                       "immediately fail). Launch emdash with --remote-debugging-port=%s.",
                       cfg.cdp_port, cfg.cdp_port)
        client.heartbeat(cfg.runner_id, [], degraded=True,
                         note=f"emdash CDP unreachable on :{cfg.cdp_port}", host=host_id(),
                         ready=False, ready_note=f"emdash CDP unreachable on :{cfg.cdp_port}")
        return "cdp_down"
    _ready, _rnote = readiness.compute(cfg)
    # Report here too: this path claims, and claiming against a stale project list
    # is exactly the failure this reporting exists to remove.
    client.heartbeat(cfg.runner_id, [], host=host_id(), ready=_ready, ready_note=_rnote,
                     projects=sessions.reported_projects(cfg))
    action = _claim_and_execute(cfg, client, _paused_agents(cfg))
    chat_pump.drain_chat_bridges(cfg, client)  # one-shot: pump the reply out before exiting
    return action


def verify_emdash(cfg_path: Path) -> int:
    """Read-only check that emdash's DB still has the columns the CDP-path reads
    depend on. Exit 0 = intact; 1 = drifted (names each missing column); 2 = the
    DB itself couldn't be read.

    This is the ONE emdash assumption that fails SILENTLY. task_state() and
    list_open_sessions() swallow sqlite errors (a read failure must never be mistaken
    for "session gone"), so a renamed tasks/projects column doesn't crash — it quietly
    degrades the runner into spawning duplicate sessions and blanking the supervisor,
    with nothing in the log. Everything else we assume about emdash fails LOUDLY and is
    obvious within a tick (emdash not installed → won't launch; CDP down → degraded
    heartbeat + a WARNING; transcripts unreadable → visible). So this verifies the quiet
    one. Run it after an emdash update.
    """
    raw = json.loads(Path(cfg_path).read_text())
    db = raw.get("emdash_db")
    if not db:
        print(f"✗ no 'emdash_db' in {cfg_path}"); return 2
    try:
        problems = emdash.check_read_schema(db)
    except emdash.SchemaCheckError as exc:
        print(f"✗ {exc}"); return 2
    if problems:
        print("✗ emdash read schema drifted — the CDP-path reads would SILENTLY degrade:")
        for p in problems:
            print(f"    - {p}")
        print("  fix: reconcile task_state()/list_open_sessions() in canopy_runner/emdash.py")
        print("       against emdash's new schema, then update READ_SCHEMA to match.")
        return 1
    n = sum(len(c) for c in emdash.READ_SCHEMA.values())
    print(f"✓ emdash read schema intact — all {n} columns across "
          f"{', '.join(emdash.READ_SCHEMA)} present in {db}")
    return 0


def update_check(cfg_path: Path) -> int:
    """Print `<status> <expected_sha>` for install-runner.sh --if-stale.

    One line, machine-first, so the shell can branch without parsing prose.
    Always exits 0: this runs on a timer, and a non-zero exit for the ordinary
    "nothing to do" case would make launchd's log a wall of false failures."""
    from . import update as update_mod

    cfg = Config.load(cfg_path)
    client = Client(cfg.base_url, cfg.token)
    status, expected = update_mod.update_status(cfg, client)
    print(f"{status} {expected or '-'}")
    return 0


def install_sidecar() -> int:
    """Provision the CDP sidecar's node deps. Exit code, for the CLI."""
    from . import cdp_control as cdp
    if cdp.sidecar_deps_installed():
        print(f"✓ CDP sidecar deps already present at {cdp.SIDECAR.parent}")
        return 0
    try:
        cdp.ensure_sidecar_deps()
    except cdp.CDPError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ installed CDP sidecar deps at {cdp.SIDECAR.parent}")
    return 0


def _provision_sidecar_or_warn() -> None:
    """Startup provisioning: a freshly installed runner has the sidecar but not its
    node_modules. WARN rather than exit — launchd's KeepAlive would turn a fatal
    startup into a restart loop, and a runner that can still heartbeat (reporting
    itself not-ready once CDP fails) is far more debuggable than one that flaps."""
    from . import cdp_control as cdp
    if cdp.sidecar_deps_installed():
        return
    logger.info("CDP sidecar deps missing at %s — installing (first run after an "
                "install; this takes a moment)", cdp.SIDECAR.parent)
    try:
        cdp.ensure_sidecar_deps()
        logger.info("CDP sidecar deps installed.")
    except cdp.CDPError as exc:
        logger.warning("could not install CDP sidecar deps: %s — turns needing emdash will "
                       "fail until `canopy-runner install-sidecar` succeeds", exc)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="canopy runner (emdash adapter)")
    # `--version` prints what the heartbeat reports, so "which runner is this box
    # actually on?" is answerable at the shell without reading the supervisor.
    parser.add_argument("--version", action="version",
                        version=f"canopy-runner {__version__} (rev {provenance.code_sha()[:12] or 'unknown'})")
    # Top-level --config/--once keep the bare invocation (no subcommand) working —
    # the launchd plist invokes `-m canopy_runner.main --config ...` with no
    # subcommand, and that must keep behaving like `run`.
    parser.add_argument("--config", help="path to runner.json")
    parser.add_argument("--once", action="store_true", help="single iteration (for cron/tests)")
    parser.add_argument("--drain-one", action="store_true",
                        help="claim + run exactly ONE queued turn, then exit (no inbox poll, "
                             "no schedules; runs even while paused)")

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="run the main loop (default)")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--once", action="store_true", help="single iteration (for cron/tests)")
    run_parser.add_argument("--drain-one", action="store_true",
                            help="claim + run exactly ONE queued turn, then exit")

    update_parser = subparsers.add_parser(
        "update-check",
        help="print '<current|stale|busy|unknown> <expected_sha>' — whether this box "
             "should install a newer runner right now (read-only; never heartbeats)",
    )
    update_parser.add_argument("--config", required=True)

    subparsers.add_parser(
        "install-sidecar",
        help="install the CDP sidecar's node deps next to the installed sidecar "
             "(idempotent; run after every `uv tool install` of this package)",
    )

    verify_parser = subparsers.add_parser(
        "verify-emdash",
        help="read-only check that emdash's DB still has the columns the CDP-path "
             "reads depend on (run after an emdash update)",
    )
    verify_parser.add_argument("--config", required=True)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    command = args.command or "run"

    if command == "update-check":
        raise SystemExit(update_check(Path(args.config)))

    if command == "install-sidecar":
        raise SystemExit(install_sidecar())

    if command == "verify-emdash":
        if not args.config:
            parser.error("verify-emdash requires --config")
        raise SystemExit(verify_emdash(Path(args.config)))

    # command == "run" (explicit "run" subcommand, or the bare/default invocation)
    if not args.config:
        parser.error("--config is required")
    cfg = Config.load(Path(args.config))
    _provision_sidecar_or_warn()
    client = Client(cfg.base_url, cfg.token)
    if getattr(args, "drain_one", False):
        print(drain_one(cfg, client))
        return
    if args.once:
        action = run_once(cfg, client)
        chat_pump.drain_chat_bridges(cfg, client)  # one-shot: don't exit mid-reply
        print(action)
        return

    # Startup banner — the log opens with exactly what this runner is configured to
    # do, so `~/.canopy/runner.log` is self-explaining.
    try:
        from .cdp_control import host_id
        host = host_id()
    except Exception:  # noqa: BLE001
        host = "?"
    logger.info("canopy-runner starting | runner=%s host=%s cdp_port=%s",
                cfg.runner_id, host, cfg.cdp_port)
    logger.info("  poll: claim every %ss | inbox every %ss | mailboxes=%s",
                cfg.poll_seconds, cfg.inbox_poll_seconds,
                ",".join(sorted(getattr(cfg, "mailboxes", {}))) or "(none)")
    logger.info("  COST note: idle cycles + inbox polls are ~free (HTTP only); a 'CREATE' "
                "line = one NEW claude session (tokens), 'REUSE' = none. grep the log for CREATE.")

    # Pause sentinel: the menu-bar app (or `touch ~/.canopy/PAUSED`) drops this file
    # to halt ALL token-spending work instantly without killing the process or fighting
    # launchd's KeepAlive. Paused = we still heartbeat (so the control plane sees the
    # runner alive-but-idle, not dead) but claim nothing, poll no inbox, spawn nothing.
    pause_file = Path(args.config).with_name("PAUSED")
    logger.info("  pause: drop %s to halt work (menu-bar app toggles this); remove to resume",
                pause_file)

    # Liveness heartbeat file: touched EVERY cycle (even idle/paused). The menu-bar app
    # reads its mtime to tell "running" from "stale" — the log alone is a bad signal
    # because idle cycles are deliberately quiet (~15 min between lines), which would
    # otherwise show a healthy idle runner as "stale".
    hb_file = Path(args.config).with_name("heartbeat")

    def _beat() -> None:
        try:
            hb_file.write_text(str(time.time()))
        except OSError:
            pass
        # Separate file, not a richer heartbeat: the menu-bar app parses that one
        # as a bare float, so changing its shape would break a shipped Swift binary.
        _mark_in_flight(cfg)

    # RC3: a WS wake-listener lets the loop claim the INSTANT a turn is enqueued
    # instead of waiting out poll_seconds. Additive + best-effort — polling stays the
    # fallback and still owns heartbeat/claim/execute; off if websocket-client is absent.
    from .wake import WakeListener

    def _on_control(msg: dict) -> None:
        if msg.get("type") == "cancel" and msg.get("turn_id"):
            CANCELLED_TURNS.add(str(msg["turn_id"]))
        elif msg.get("type") == "check_inbox" and msg.get("mailbox"):
            # THE DOORBELL. Gmail told canopy-web this mailbox changed; check it
            # on the next tick instead of waiting out inbox_poll_seconds. Only
            # marks it due — the read itself stays on the poll thread, because a
            # `gog` subprocess on the wake-listener thread would block the socket
            # that also carries cancel and wake.
            inbox_due.ring(str(msg["mailbox"]))
        elif msg.get("type") == "menu_answer" and msg.get("session_key"):
            # A human answered, from the web, the dialog an agent is blocked on.
            # Runs on the wake-listener thread and must never raise: this socket
            # also carries cancel and wake, and losing it would cost the runner
            # its liveness for a keystroke.
            try:
                hooks.answer_menu(str(msg["session_key"]), msg.get("option"),
                             cdp_port=cfg.cdp_port)
            except Exception:  # noqa: BLE001
                logger.warning("menu answer failed for %s", msg.get("session_key"),
                               exc_info=True)

    waker = WakeListener(cfg.base_url, cfg.token, cfg.runner_id, on_control=_on_control)
    wake_on = waker.start()
    if wake_on:
        logger.info("  wake: WS control channel connected — claims fire on enqueue, not just poll")
    hooks.start_hook_listener(cfg, client)

    def _wait(seconds: float) -> None:
        # With a live wake channel, block until a nudge OR the poll interval,
        # whichever comes first. Without one (websocket-client absent — the
        # poll-only laptop, the cloud REST fallback, the test env), fall back to
        # the exact prior behavior: a plain time.sleep. Routing the wait through
        # the Event unconditionally would swallow the time.sleep the loop tests
        # patch to break the loop — an infinite hang.
        if not wake_on:
            time.sleep(seconds)
            return
        if waker.event.wait(seconds):  # returns early on a wake nudge
            waker.event.clear()

    idle_streak = 0
    paused = False
    while True:
        _beat()
        # The pause is NOT short-circuited here any more, and that is deliberate.
        # This branch used to `continue` before run_once, which meant a local
        # sentinel could never be pushed up — the local file and the server's
        # `paused` were two states with no path between them. run_once now
        # heartbeats, pumps in-flight replies, reconciles the two, and returns
        # "paused" without starting anything, which is everything this branch did
        # plus the sync. One place owns the rule.
        try:
            result = run_once(cfg, client)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.exception("run_once crashed; continuing")
            result = "crashed"
        # Announce the pause TRANSITIONS loudly and exactly once each. A parked
        # runner is indistinguishable from a dead one in the log otherwise, and
        # "nobody remembered to unpause it" is the way this feature hurts you.
        if result == "paused" and not paused:
            logger.warning("PAUSED — skipping all work (no claim, no inbox, no schedules, "
                           "no tokens). Resume via the menu-bar app, `canopy runner unpause`, "
                           "or by removing %s.", pause_file)
            paused = True
        elif result != "paused" and paused:
            logger.info("RESUMED — back to normal polling")
            paused = False
            idle_streak = 0
        # One scannable line per cycle. Idle is quiet (a heartbeat every ~15 min so the
        # log shows the runner is alive without flooding); everything else logs at INFO.
        # "cdp_down" is quiet like "idle" — the throttled WARNING in run_once and the
        # degraded heartbeat already carry the reason, so logging it every tick would be the
        # per-tick spam the preflight is meant to avoid. "paused" is quiet for the same
        # reason: its transition is already a WARNING just above.
        if result in ("idle", "cdp_down", "paused"):
            idle_streak += 1
            if idle_streak % max(1, (900 // max(cfg.poll_seconds, 1))) == 0:
                logger.info("cycle: %s (x%d) — runner alive, nothing claimed", result, idle_streak)
        else:
            if idle_streak:
                logger.info("cycle: %s (after %d idle)", result, idle_streak)
            else:
                logger.info("cycle: %s", result)
            idle_streak = 0
        _wait(cfg.poll_seconds)  # wake-aware: claims fire on enqueue, not just poll


if __name__ == "__main__":
    main()
