#!/usr/bin/env python3
"""Canopy cloud runner — a headless `kind=cloud` executor for EC2.

Self-contained (stdlib only) on purpose: the emdash-coupled runner/canopy_runner
drives a GUI over CDP, which is wrong for a headless box. This pairs a cloud runner,
claims harness Turns, runs `claude -p` (stream-json) on the turn's prompt, streams
the assistant/tool output into the TurnEvent ledger, and finishes the turn.

SESSION-CAPABLE, OPT-IN (RC/run-convergence PR2): this runner CAN declare
`capabilities.sessions` (RUNNER_SESSIONS=1; default OFF) to become eligible to claim
chat/session-targeted Turns — see Runner.session_capable() in apps/harness/models.py.
It is off by default because this runner has no durable-record path for a chat
session yet (see the RUNNER_SESSIONS comment at its declaration for the full trace);
turning it on today means a real conversation's history can be silently lost. Every
raw stream-json line the CLI emits is ALSO forwarded verbatim to
POST /turns/{id}/transcript (batched by bytes; see runner/ec2/README.md), in
addition to the reduced TurnEvents. And a session turn's CLI session id is captured
from the stream and round-tripped through the existing resolve-session/record-session
RPCs so a later turn on the same canopy Session can `--resume` it instead of
cold-starting — see _session_resume_plan / _record_session_resume below for exactly
which field carries that id and why.

Config comes from the environment (see runner/ec2/README.md):
  CANOPY_BASE_URL   e.g. https://labs.connect.dimagi.com/canopy
  CANOPY_TOKEN      a canopy-web Personal Access Token (Bearer)
  RUNNER_NAME       display name (default: this hostname)
  RUNNER_PROJECTS   comma-separated repo names this runner may drive (e.g. canopy-web)
  RUNNER_AGENTS     comma-separated agent slugs this runner may drive (e.g. echo,ada)
  RUNNER_SESSIONS   whether this runner claims chat/session turns (default: OFF —
                     opt-in; see the RUNNER_SESSIONS declaration for why)
  RUNNER_HOST       stable identity for session-reuse gating (default: RUNNER_NAME).
                     Analogous to emdash's per-macOS-account host, but for a headless
                     box it just needs to be STABLE across process restarts on the
                     SAME instance — a new EC2 instance getting a new value is the
                     correct behavior (see _session_resume_plan).
  RUNNER_WORKSPACE  optional workspace slug (defaults to the token's default)
  CLAUDE_BIN        path to the claude binary (default: claude)
  WORK_DIR          scratch dir for project/session turns and clone-less agents
                     (default: /tmp/canopy-runner-work)
  AGENT_ROOT         where bootstrapped agent clones live (default: /opt/agents);
                     an agent turn with a clone here runs IN it, not WORK_DIR
  CANOPY_WEB_REPO_DIR/CANOPY_WEB_REPO_URL  where bootstrap_agent_fleet() clones/
                     pulls canopy-web from, to run its bootstrap_agents.sh
  POLL_SECONDS      idle poll interval (default: 15)
  STATE_FILE        runner-id cache (default: ~/.canopy-cloud-runner.json)
  RUNNER_HOME       where this runner's bytes + its two auto-update files live
                     (default: /opt/canopy-runner) — `build-info.json` says what
                     code is installed, `in-flight` says whether now is a safe
                     moment to replace it. Both are written here and read by
                     runner/ec2/update_runner.sh; see spec 2026-07-30.
`claude` authenticates from CLAUDE_CODE_OAUTH_TOKEN (a dedicated setup-token from
Secrets Manager, staged into the service env by cloud-init). AGENT_SLUGS /
AGENT_REPO_ORG / GITHUB_TOKEN / OP_SERVICE_ACCOUNT_TOKEN are consumed by
bootstrap_agents.sh (see runner/ec2/README.md), not this file directly.
"""
from __future__ import annotations

import json
import os
import fcntl
import pathlib
import pty
import re
import select
import signal
import struct
import socket
import shutil
import subprocess
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("CANOPY_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("CANOPY_TOKEN", "")
RUNNER_NAME = os.environ.get("RUNNER_NAME") or f"cloud-{socket.gethostname()}"


def _csv(name: str) -> list[str]:
    return [x.strip() for x in (os.environ.get(name, "") or "").split(",") if x.strip()]


def _bool_env(name: str, default: bool) -> bool:
    """A tri-state env flag: unset -> `default`; anything else -> its truthiness,
    with the obvious falsy spellings ("0"/"false"/"no"/"") honored regardless of
    case. Same env-var-only philosophy as _csv (no JSON in the env file)."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "")


# Capabilities as plain comma-separated env vars — no JSON in the env file, which
# bash `source` and systemd EnvironmentFile both mangle (they strip the quotes).
RUNNER_CAPS: dict[str, object] = {}
if _csv("RUNNER_PROJECTS"):
    RUNNER_CAPS["projects"] = _csv("RUNNER_PROJECTS")
if _csv("RUNNER_AGENTS"):
    RUNNER_CAPS["agents"] = _csv("RUNNER_AGENTS")
# The durable-record precondition below is now SATISFIED, and verified end to end
# on cloud-ec2-1 (2026-07-28): a session turn ships its transcript rows via
# POST /runners/{id}/session-stream -> services.persist_transcript_rows
# (`_ship_transcript_rows`), lands ordinal-keyed Message rows, and resumes its
# CLI session on the next turn. RUNNER_SESSIONS therefore defaults ON in
# runner.cfn.yaml; this env default stays OFF so a runner started by hand,
# outside the template, opts in deliberately.
#
# It took longer to be true than it looked, and both causes were invisible from
# here — worth recording, because the original note blamed the wrong layer:
#   * the WS claim frame (apps/realtime/consumers._serialize_turn) omitted
#     origin_ref, so `_chat_session_id` came back "" and BOTH the stable
#     per-session cwd and `_ship_transcript_rows` silently no-opped;
#   * `canopy_transcript` was installed by a method Ubuntu 24.04 refuses
#     (PEP 668 + no pip), so `_transcript_core()` returned None regardless.
# The original risk was real: a runner that streams a perfect-looking live reply
# and then loses the conversation is worse than one that never claims. That was
# the OBSERVED behaviour here until both were fixed.
#
# /runners/{id}/streams + /backfills are implemented too, as of 2026-07-28
# (_sync_session_streams / _drain_backfills), so an attached viewer gets a live
# transcript tail rather than the reduced TurnEvent stream, and a
# server-requested backfill rebuilds history. That closes the last gap against
# runner/canopy_runner for session work.
RUNNER_SESSIONS = _bool_env("RUNNER_SESSIONS", False)
if RUNNER_SESSIONS:
    RUNNER_CAPS["sessions"] = True
RUNNER_WORKSPACE = os.environ.get("RUNNER_WORKSPACE", "")
# A stable identity for THIS process/instance, load-bearing for session-reuse
# gating (RunnerBinding.reusable_by requires host to match — see
# _session_resume_plan). Unlike emdash's per-macOS-account host, a headless box
# has no account concept; RUNNER_NAME (which defaults to the hostname) is a fine
# stand-in as long as it is stable across restarts of the SAME instance.
RUNNER_HOST = os.environ.get("RUNNER_HOST") or RUNNER_NAME
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/canopy-runner-work")
# Agent-fleet bootstrap (runner/ec2/bootstrap_agents.sh) — see
# bootstrap_agent_fleet() below for why this runs from here and not cloud-init.
AGENT_ROOT = os.environ.get("AGENT_ROOT", "/opt/agents")
CANOPY_WEB_REPO_DIR = os.environ.get("CANOPY_WEB_REPO_DIR", "/opt/canopy-web")
CANOPY_WEB_REPO_URL = os.environ.get("CANOPY_WEB_REPO_URL", "https://github.com/dimagi-internal/canopy-web.git")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
# App-level heartbeat cadence (keeps the lease + status fresh).
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "20"))
# Short recv poll so the WS loop regains control regularly and drives the heartbeat
# on a wall clock. MUST stay below uvicorn's --ws-ping-interval (5s): the server
# pings every 5s and websocket-client auto-pongs and keeps recv() looping, so a
# heartbeat gated on recv() timing out would never fire and the runner would go
# stale while still connected.
WS_POLL_TIMEOUT = float(os.environ.get("WS_POLL_TIMEOUT", "3"))
# How often to tail the transcripts of sessions a viewer is attached to. This is
# the live view's latency floor, so it is deliberately far tighter than
# POLL_SECONDS (the turn-claim clock): at 15s a watcher sees a streaming reply
# arrive in 15-second lumps. Costs one small GET /streams per tick when idle.
STREAM_POLL_SECONDS = float(os.environ.get("STREAM_POLL_SECONDS", "3"))
# How often the lease-renewal thread heartbeats WHILE a turn is executing (both
# loops block inside run_claude() for the whole turn, so nothing else heartbeats
# during that window). Must stay comfortably under DEFAULT_LEASE_SECONDS (900s,
# apps/harness/services.py) or a long turn gets swept LOST mid-execution.
LEASE_HEARTBEAT_SECONDS = int(os.environ.get("LEASE_HEARTBEAT_SECONDS", "60"))
STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", str(pathlib.Path.home() / ".canopy-cloud-runner.json")))

# Where this runner's bytes live, and the two files the auto-updater
# (runner/ec2/update_runner.sh, spec 2026-07-30) shares with it:
#   build-info.json — what code is installed, stamped by whoever installed it.
#   in-flight       — how many turns this box is carrying right now.
RUNNER_HOME = pathlib.Path(os.environ.get("RUNNER_HOME", "/opt/canopy-runner"))
BUILD_INFO_FILE = pathlib.Path(os.environ.get("BUILD_INFO_FILE", str(RUNNER_HOME / "build-info.json")))
IN_FLIGHT_FILE = pathlib.Path(os.environ.get("IN_FLIGHT_FILE", str(RUNNER_HOME / "in-flight")))

# Bound on reaping the claude subprocess in run_claude's cleanup (review N1) —
# a bare, untimed proc.wait() can hang the RUNNER forever if the child is still
# alive with an undrained stdout pipe (a mid-loop exception leaves exactly that
# state). Always kill/close before waiting, and never wait unboundedly.
PROC_REAP_TIMEOUT_SECONDS = 15.0

# The server caps a single POST /turns/{id}/transcript body at 1 MiB of line bytes
# (apps/harness/api.py::TRANSCRIPT_APPEND_MAX_BYTES) and 422s over it — batch well
# under that so this runner never has to handle (or silently drop on) that error.
TRANSCRIPT_APPEND_MAX_BYTES = 900 * 1024
# Flush the accumulated raw-line buffer once it reaches this many bytes...
TRANSCRIPT_FLUSH_BYTES = 512 * 1024
# ...or this many seconds have passed since the last flush, whichever comes first —
# a quiet turn (waiting on a long tool call) must not sit on unflushed lines forever.
TRANSCRIPT_FLUSH_SECONDS = 10.0

_stop = False


def _log(msg: str) -> None:
    print(f"[cloud-runner] {msg}", flush=True)


def _api(method: str, path: str, body: dict | None = None, *,
         prefix: str = "/api/harness") -> tuple[int, dict | None]:
    """Call canopy-web. `prefix` defaults to the harness router this runner lives
    on; pass another (e.g. `/api/events`) for the few cross-router calls."""
    url = f"{BASE_URL}{prefix}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        _log(f"{method} {path} -> {exc.code}: {raw[:300]!r}")
        return exc.code, None
    except urllib.error.URLError as exc:
        _log(f"{method} {path} -> URLError {exc.reason}")
        return 0, None


# ── code provenance + the busy marker (auto-update's two shared files) ──────
_BUILD_INFO: dict | None = None


def build_info(*, refresh: bool = False) -> dict:
    """What code is this box running — `{"sha": str, "committed_at": int}`.

    Stamped into a file by whoever INSTALLED the bytes (canopy-fetch-env for the
    first-boot seed, update_runner.sh for every update since), because there is no
    git history at /opt/canopy-runner to derive it from. The laptop runner has the
    same two provenances and the same answer: `_build_info.py`, stamped at build
    time by install-runner.sh.

    Missing, unreadable, or malformed yields empty/0 — which every consumer treats
    as UNKNOWN and stays silent about. A staleness alert fired on partial
    information is worse than no alert.

    Cached for the process's lifetime ON PURPOSE, exactly as `provenance.code_sha`
    is: this answers "which bytes did I START with". The updater rewrites the stamp
    moments before restarting the service, so re-reading it live would report the
    new sha while still executing the old code — clearing the staleness banner for
    the box that is still stale.
    """
    global _BUILD_INFO
    if _BUILD_INFO is not None and not refresh:
        return _BUILD_INFO
    info = {"sha": "", "committed_at": 0}
    try:
        raw = json.loads(BUILD_INFO_FILE.read_text())
        info["sha"] = str(raw.get("sha") or "").strip()
        info["committed_at"] = int(raw.get("committed_at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    _BUILD_INFO = info
    return info


def _mark_in_flight(count: int) -> None:
    """Publish how many turns this box is carrying, for the auto-updater.

    An update restarts the service, so it must not land mid-turn. Same file shape
    and same semantics as the laptop's `update.mark_busy` — including that a marker
    older than 120s means the daemon has stopped writing it (stopped, wedged,
    crash-looping) and therefore must NOT read as busy: that is the case
    auto-update exists to rescue.

    Best-effort: a failure here can never affect a turn.
    """
    try:
        IN_FLIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
        IN_FLIGHT_FILE.write_text(json.dumps({"count": int(count), "at": time.time()}))
    except OSError:
        pass


# --- the update doorbell -----------------------------------------------------
#
# The server rings `update_available` down the WS the moment a heartbeat reports
# a sha that differs from the deployed expectation — on EVERY stale beat, so a
# missed frame costs one beat, not one timer cycle. The daemon owns the throttle.
UPDATE_NUDGE_MIN_SECONDS = 600.0
_last_update_nudge = 0.0


def _start_update_unit() -> None:
    # `systemctl start`, never running update_runner.sh as a child: the updater
    # restarts canopy-runner.service, and a child of this process would be
    # killed in the daemon's own cgroup mid-install by that restart. Handing
    # the work to systemd puts it in the unit's cgroup, where the restart it
    # performs cannot reach it. `--no-block` because the oneshot takes ~a
    # minute and this thread carries wake and heartbeat. The scoped sudoers
    # line ships in runner.cfn.yaml.
    subprocess.run(
        ["sudo", "-n", "systemctl", "start", "--no-block", "canopy-runner-update.service"],
        capture_output=True, timeout=15, check=True,
    )


def _nudge_updater(expected_sha: str, *, now: float | None = None) -> bool:
    """The `update_available` doorbell: start the SEPARATE update unit now
    instead of waiting out its 30-minute timer.

    Never installs in-process — the unit re-checks staleness and the in-flight
    marker itself, so busy deferral and the crash-loop rescue are inherited,
    not re-implemented. Skips when the frame raced an install that already
    happened. Never raises: the WS loop it runs on also carries wake,
    heartbeat and claim."""
    global _last_update_nudge
    now = time.time() if now is None else now
    expected = (expected_sha or "").strip()
    installed = build_info()["sha"]
    # Empty on either side is UNKNOWN, never "stale" — the fleet provenance rule.
    if not expected or not installed or expected == installed:
        return False
    if now - _last_update_nudge < UPDATE_NUDGE_MIN_SECONDS:
        return False
    # Advance the throttle on the ATTEMPT, not the success: a broken systemctl
    # would otherwise warn on every ~20s beat, and the timer rescues that case.
    _last_update_nudge = now
    try:
        _start_update_unit()
    except Exception as exc:  # noqa: BLE001
        _log(f"update nudge: could not start canopy-runner-update.service: {exc}")
        return False
    _log(f"update nudge: started canopy-runner-update.service "
         f"(expected {expected[:12]}, installed {installed[:12]})")
    return True


def _heartbeat_body(active_turn_ids: list[str], **extra) -> dict:
    """THE heartbeat payload — one stamping point, for all four call sites.

    `services.heartbeat` assigns code_sha/code_committed_at unconditionally, so any
    call site that omits them RESETS the fields. This runner heartbeats from four
    places (pairing, the idle REST loop, the WS beat, the per-turn lease renewer),
    and the laptop already paid for the version of this bug where four of six sites
    silently cleared `code_branch` (see provenance.py). Building the body in one
    function is what makes that unrepresentable rather than merely fixed.

    `code_branch` is deliberately absent: /opt/canopy-runner is not a checkout, and
    reporting a branch would be inventing one. Writing the busy marker here too
    means it is refreshed by whatever heartbeat is live — including the lease
    renewer, which is the only one beating while a long turn runs.
    """
    info = build_info()
    _mark_in_flight(len(active_turn_ids))
    return {
        "active_turn_ids": active_turn_ids,
        "host": RUNNER_HOST,
        "code_sha": info["sha"],
        "code_committed_at": info["committed_at"],
        **extra,
    }


def _chunk_transcript_lines(
    lines: list[str], max_bytes: int = TRANSCRIPT_APPEND_MAX_BYTES
) -> list[list[str]]:
    """Split raw JSONL lines into batches whose UTF-8-encoded total stays at or
    under `max_bytes` — the server enforces a hard per-request byte cap, not a
    line count, so a single giant tool-result line would blow past a count-based
    batch.

    A single line that itself exceeds `max_bytes` can never fit even alone: the
    server 422s the WHOLE request when total_bytes > its cap
    (apps/harness/api.py), before `append_transcript`'s separate 100MB per-turn
    ceiling is ever consulted — that ceiling is a different mechanism and is not
    a backstop for this case. Shipping it "alone" would still fail, silently
    dropping the line with no record. Instead it is replaced with a synthetic
    `canopy_runner_line_dropped` marker line, so a cost/structure aggregator
    reading the transcript can at least SEE the gap instead of it vanishing."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for line in lines:
        n = len(line.encode("utf-8"))
        if n > max_bytes:
            if current:
                batches.append(current)
                current = []
                current_bytes = 0
            marker = json.dumps({
                "type": "canopy_runner_line_dropped",
                "reason": "line exceeds the per-request transcript byte cap",
                "bytes": n,
            })
            batches.append([marker])
            continue
        if current and current_bytes + n > max_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(line)
        current_bytes += n
    if current:
        batches.append(current)
    return batches


def _claude_cmd(prompt: str, resume_session_id: str | None = None) -> list[str]:
    """The `claude -p` argv for one invocation. Split out from `run_claude` so the
    `--resume` wiring is unit-testable without touching subprocess.

    A FRESH turn names its session id explicitly, and that is a safety property
    rather than tidiness. canopy's duplicate/sibling check (`live-turns.sh`)
    enumerates live sessions out of argv — `--session-id|--resume <uuid>` — which
    is the only handle a process has on which session it is. A bare `claude -p`
    matches neither, so on this box the check found NO sessions, could not even
    see itself, and exited 2 ("could not enumerate") on every single turn.

    That is the check behaving correctly — it must never render "I could not
    look" as "nobody is there" — but it means the fleet's protection against two
    turns working the same thread simply did not exist on a cloud runner. The
    fix belongs here, not in the check: make the box visible, rather than teach
    the check to guess. Found by an ACE turn on cloud-ec2-1, 2026-09-09, which
    verified it against its own /proc/<pid>/cmdline.

    Not set when resuming: `--resume` already names the session, and passing
    both asks the CLI to be two sessions at once.
    """
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    else:
        cmd += ["--session-id", str(uuid.uuid4())]
    return cmd


def _start_lease_renewal(runner_id: str, turn_id: str) -> threading.Event:
    """Renew this turn's claim lease for the duration of execution.

    Both `_claim_and_run_once` (WS) and `run_over_rest`'s turn body block inside
    `run_claude()` for the entire turn — no heartbeat happens while that call
    is running, and the idle heartbeats both loops send elsewhere carry
    `active_turn_ids: []`, which renews nothing (apps/harness/services.py::
    heartbeat only renews leases for ids in that list). Without this, any
    turn running longer than DEFAULT_LEASE_SECONDS (900s) gets swept LOST
    out from under a runner that is still actively working it.

    Runs on its own daemon thread and heartbeats over plain REST via `_api`,
    which opens a fresh HTTPS connection per call — deliberately, so this is
    safe to run concurrently with the WS loop's own socket use. The thread
    must NEVER touch `ws` (send/recv/close): the websocket-client `WebSocket`
    object is not safe to share across threads, and the caller's loop is
    already reading/writing it. Caller stops the thread (`.set()`) in a
    `finally` once the turn ends, whether it succeeded, failed, or raised.
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(LEASE_HEARTBEAT_SECONDS):
            _api("POST", f"/runners/{runner_id}/heartbeat", _heartbeat_body([turn_id]))

    threading.Thread(target=_loop, daemon=True, name=f"lease-{turn_id[:8]}").start()
    return stop


def pair_or_load() -> str:
    if STATE_FILE.exists():
        rid = json.loads(STATE_FILE.read_text()).get("runner_id")
        if rid:
            # Confirm it still exists (a heartbeat 404 means it was retired).
            # `projects` rides the heartbeat, not the PATCH below: the server
            # treats that key as REPORTED (spec 2026-07-28) and 422s a hand-written
            # one. This box has no emdash to observe, so its configured list IS the
            # observation — but it still has to be reported, or a redeploy that
            # changes RUNNER_PROJECTS would never take effect. Safe to send [] here
            # where a laptop must not: this comes from env, so there is no read to
            # fail and no way to mistake "cannot tell" for "have none".
            status, _ = _api("POST", f"/runners/{rid}/heartbeat",
                             _heartbeat_body([], projects=list(RUNNER_CAPS.get("projects") or [])))
            if status == 200:
                _log(f"reusing runner {rid}")
                # Capabilities were historically fixed at pairing time; re-pairing
                # to pick up a changed env var (e.g. RUNNER_SESSIONS flipped on for
                # a box paired before this feature existed) would mint a NEW runner
                # id and orphan this one's RunnerBindings. PATCH in place instead
                # (apps/harness/api.py::update_runner_capabilities) so a redeploy
                # with a new env always reflects the CURRENT declared capabilities.
                #
                # MINUS `projects`, which the heartbeat above just reported: that
                # route 422s a hand-written `projects` now, and the failure would
                # take the agents/sessions sync down with it — they share one call.
                _api("PATCH", f"/runners/{rid}",
                     {"capabilities": {k: v for k, v in RUNNER_CAPS.items() if k != "projects"}})
                return rid
    body = {"name": RUNNER_NAME, "kind": "cloud", "capabilities": RUNNER_CAPS, "host": RUNNER_HOST}
    if RUNNER_WORKSPACE:
        body["workspace"] = RUNNER_WORKSPACE
    status, payload = _api("POST", "/runners/", body)
    if status != 201 or not payload:
        _log(f"FATAL: could not pair runner (status={status}). Check CANOPY_BASE_URL/CANOPY_TOKEN.")
        sys.exit(1)
    rid = payload["id"]
    STATE_FILE.write_text(json.dumps({"runner_id": rid}))
    _log(f"paired new runner {rid} ({RUNNER_NAME}, caps={RUNNER_CAPS})")
    return rid


# Session-identity markers Claude Code exports into its own child processes.
# They must NOT reach a `claude` this runner spawns.
#
# Measured 2026-07-28 by spawning `claude` from inside a Claude Code session: the
# child came up in the PARENT'S permission mode (`auto`, self-approving) and
# announced "Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION
# marker". Transcript saving being off is the serious half — the transcript is
# canopy's DURABLE RECORD, so a turn would run, finish, report success, and
# leave nothing to persist or reset from.
#
# Normally the runner is a service and has none of these. It inherits them the
# moment someone starts it from inside an agent session — exactly the debugging
# situation where you would least suspect the record.
#
# CLAUDE_CODE_OAUTH_TOKEN IS DELIBERATELY NOT HERE: it is how the box
# authenticates (see the module docstring). A blanket CLAUDE_CODE* strip is the
# obvious-looking version of this fix and it silently breaks every turn.
INHERITED_SESSION_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)


def _child_safe_env() -> dict:
    """`os.environ` with the parent's Claude session identity removed.

    Credentials are preserved; only the markers that make a spawned agent behave
    as a CHILD of this process are dropped.
    """
    env = os.environ.copy()
    for marker in INHERITED_SESSION_MARKERS:
        env.pop(marker, None)
    return env


def _agent_env(slug: str | None) -> dict:
    """The turn environment, with the agent's OWN `~/.<slug>/.env` layered on top.

    `bootstrap_agents.sh` materializes that file with `op inject` (the fleet's
    provisioning standard), but writing a file is not the same as exporting it:
    nothing here ever sourced it, so an agent's own credentials — notably
    CANOPY_WEB_PAT, which `canopy_web.resolve_pat()` reads FIRST — never reached
    `claude -p`, and every agent silently fell back to the runner's ambient
    CANOPY_TOKEN (a human's PAT). Loading it here is what makes per-agent
    identity real rather than merely provisioned.

    Deliberately layered UNDER the runner's own environment for the few keys the
    runner must control (CANOPY_BASE_URL, CANOPY_TOKEN as a fallback, PATH), and
    OVER it for everything else the agent declares. Parsing is intentionally
    minimal — `op inject` emits plain `KEY=value` lines; anything exotic (export
    prefixes, shell interpolation) is not part of the format and is skipped
    rather than guessed at.
    """
    env = _child_safe_env()
    if not slug:
        return env
    env_file = pathlib.Path.home() / f".{slug}" / ".env"
    try:
        raw = env_file.read_text()
    except OSError:
        return env  # not provisioned (yet) — the turn still runs
    loaded = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip().strip('"').strip("'")
        env[key] = value
        loaded += 1
    if loaded:
        _log(f"loaded {loaded} vars from {env_file}")
    return env


# Bounded retry for a transient transcript-POST failure (5xx/timeout/URLError).
# Small and short: this runs INLINE in the turn's own execution path (see
# run_claude's flush_transcript), so it must not itself become the thing that
# stalls a live turn for a long time.
TRANSCRIPT_POST_RETRIES = 3
TRANSCRIPT_POST_RETRY_SLEEP_SECONDS = 1.0


def _post_transcript_batch(turn_id: str, attempt_id: str, seq: int, lines: list[str]) -> bool:
    """Ship one already-byte-bounded batch, retrying a transient failure a few
    times with the SAME `batch_id` (never fabricating a new one per attempt —
    that would defeat the server's last-batch dedup, apps/harness/services.py,
    which is exactly what makes a same-id retry safe rather than a duplicate).

    Returns False iff the SERVER reports this turn's transcript as `truncated`
    (its per-turn size ceiling latched) — the caller should stop posting for
    the rest of THIS turn, since every further byte would be silently dropped
    server-side anyway. Returns True in every other case, including after
    exhausting retries on a genuinely failing batch: per the transcript
    contract (runner/ec2/README.md), a transcript failure must never
    fail the TURN, so a batch that never lands is logged and abandoned rather
    than raised, but posting continues for whatever comes next.

    `batch_id` is scoped to (turn, attempt, seq) rather than just (turn, seq):
    a resume-fallback retry (see `run_claude`) re-invokes this whole function
    with seq restarting at 1, and reusing a bare `f"{turn_id}:{seq}"` id would
    collide with the FIRST (failed-resume) attempt's batch 1 — the server's
    dedup treats a repeated batch_id as a lost-ack retry and silently no-ops
    it, which would drop the fresh attempt's transcript rather than the
    intended duplicate."""
    if not lines:
        return True
    batch_id = f"{turn_id}:{attempt_id}:{seq}"
    body = {"lines": lines, "batch_id": batch_id}
    for attempt in range(1, TRANSCRIPT_POST_RETRIES + 1):
        try:
            status, payload = _api("POST", f"/turns/{turn_id}/transcript", body)
        except Exception as exc:  # noqa: BLE001 — a transcript hiccup must never fail the turn
            status, payload = 0, None
            _log(f"transcript POST turn={turn_id[:8]} batch={seq} attempt={attempt} raised: {exc}")
        if status == 200:
            if payload and payload.get("truncated"):
                _log(f"transcript POST turn={turn_id[:8]}: server reports truncated; "
                     "no further posts for this turn")
                return False
            return True
        if attempt < TRANSCRIPT_POST_RETRIES:
            _log(f"transcript POST turn={turn_id[:8]} batch={seq} -> {status}; "
                 f"retry {attempt}/{TRANSCRIPT_POST_RETRIES}")
            time.sleep(TRANSCRIPT_POST_RETRY_SLEEP_SECONDS)
    _log(f"transcript POST turn={turn_id[:8]} batch={seq} failed after "
         f"{TRANSCRIPT_POST_RETRIES} attempts; this slice is lost, continuing")
    return True


# --------------------------------------------------------------------------
# ACP executor
# --------------------------------------------------------------------------
# `claude -p` + stream-json parsing is canopy's own protocol, hand-rolled. ACP
# is the standard that already specifies it — tool-call lifecycle with a status,
# streamed reply text, thinking, tokens, rate limits — and it is what emdash
# itself runs (`@agentclientprotocol/claude-agent-acp` wrapping the Agent SDK).
#
# Adopted HERE and not on the laptop because the cloud box has no emdash to
# supervise it: a canopy-spawned ACP session on a laptop would be invisible to
# emdash, which costs the jump-in that makes the laptop worth running. See
# docs/superpowers/specs/2026-07-27-acp-adoption-design.md.
#
# Default OFF. `claude -p` is the proven path and stays the default until this
# has run real turns on a real box; flip with RUNNER_EXECUTOR=acp.
RUNNER_EXECUTOR = os.environ.get("RUNNER_EXECUTOR", "cli").strip().lower()

# A hard ceiling on ONE ACP turn. `claude -p` has no equivalent because the
# subprocess exiting ends the turn; an ACP prompt is a request that could in
# principle never resolve, and a turn that never returns holds its lease alive
# forever (the lease renewer keeps beating), so the sweep never rescues it.
# Generous — real turns run for many minutes — because this is a wedge guard,
# not a policy.
ACP_TURN_TIMEOUT_SECONDS = float(os.environ.get("ACP_TURN_TIMEOUT_SECONDS", "5400"))

_ACP_CORE: object = False  # False = not yet attempted


def _acp_core():
    """canopy_acp, imported lazily and cached — same reason as _transcript_core:
    the package lives in the repo this module clones, so it does not exist at
    import time. Returns None when unavailable, and the caller falls back to
    `claude -p` rather than failing the turn."""
    global _ACP_CORE
    if _ACP_CORE is not False:
        return _ACP_CORE
    try:
        import canopy_acp  # noqa: PLC0415
        _ACP_CORE = canopy_acp
    except Exception as exc:  # noqa: BLE001
        _log(f"canopy_acp unavailable ({exc}); ACP executor disabled")
        _ACP_CORE = None
    return _ACP_CORE


def run_acp(prompt: str, turn_id: str, emit, cwd: pathlib.Path | None = None,
            agent_slug: str | None = None, resume_session_id: str | None = None
            ) -> tuple[bool, str, str]:
    """Run one turn over ACP. Same contract as `run_claude`:
    (ok, final_text, cli_session_id).

    Deliberately identical in signature so the two are interchangeable at the
    call site and the switch is one line — the point is to prove ACP against the
    SAME harness, not to grow a second turn lifecycle.

    Events are emitted in the shapes the ledger already carries (`assistant`,
    `tool_start`, `tool_end`), so the client needs no change to render an
    ACP-executed turn. `tool_start.id` is ACP's `toolCallId`, which IS the
    transcript's `tool_use.id` — the same key `pairToolMessages` pairs on.

    The durable transcript is untouched: an ACP session writes a normal
    `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`, so `_ship_transcript_rows`
    works unchanged against the returned session id.
    """
    core = _acp_core()
    if core is None:
        _log(f"turn {turn_id[:8]}: ACP requested but unavailable — using claude -p")
        return run_claude(prompt, turn_id, emit, cwd=cwd, agent_slug=agent_slug,
                          resume_session_id=resume_session_id)

    workdir = cwd if cwd is not None else pathlib.Path(WORK_DIR) / turn_id[:8]
    workdir.mkdir(parents=True, exist_ok=True)

    reducer = core.UpdateReducer()
    # Guards the batch against the reader thread: updates arrive on the ACP
    # reader, the flush happens on this one.
    lock = threading.Lock()
    batch: list = []
    started: set = set()
    seen_complete: set = set()
    # `session/load` REPLAYS the entire prior conversation as ordinary updates.
    # Emitting those would append the whole history to the ledger again on every
    # resumed turn, and fold it into this turn's result text. Observed directly:
    # a resumed turn re-emitted the previous turn's tool call and reply.
    state = {"replaying": False}

    def _emit_tool_start(call_id, call):
        """One tool_start per call, carrying real arguments.

        `tool_call` opens with `rawInput: {}` — the arguments arrive on a later
        patch — so emitting on the opener ships a Bash row with no command.
        Wait for the first update that HAS input, or for the call to finish
        (a genuinely argument-less tool), whichever comes first. Ordering is
        still guaranteed: on completion the start is appended before the end.
        """
        if call_id in started:
            return
        if not call.raw_input and not call.is_complete:
            return
        started.add(call_id)
        batch.append({"kind": "tool_start", "payload": {
            "id": call_id,
            "name": call.tool_name or call.kind,
            "input": call.raw_input,
        }})

    def on_update(_session_id, update):
        if state["replaying"]:
            return
        kind = reducer.apply(update)
        if kind is None:
            return
        with lock:
            if kind == "agent_message_chunk":
                text = _content_text_of(update)
                if text:
                    batch.append({"kind": "assistant", "payload": {"text": text}})
            elif kind in ("tool_call", "tool_call_update"):
                call_id = update.get("toolCallId") or ""
                call = reducer.tool_call(call_id)
                if call is None:
                    return
                _emit_tool_start(call_id, call)
                # Emit tool_end ONCE, when the call actually reaches a terminal
                # status — `tool_call_update` is a sparse patch and several
                # arrive per call (see canopy_acp.updates).
                if call.is_complete and call_id not in seen_complete:
                    seen_complete.add(call_id)
                    batch.append({"kind": "tool_end", "payload": {
                        "tool_use_id": call_id,
                        "is_error": call.is_error,
                        "content": call.result_text,
                    }})

    def flush():
        with lock:
            if not batch:
                return
            pending, batch[:] = list(batch), []
        try:
            emit(pending)
        except Exception as exc:  # noqa: BLE001 — the live stream may never cost a turn
            _log(f"warn: could not emit ACP events for {turn_id[:8]}: {exc}")

    agent = None
    try:
        agent = core.AcpAgent(cwd=workdir, env=_agent_env(agent_slug), on_update=on_update)
        agent.start()
        # Reachable by the WS thread from here on — see `steer_turn`.
        _acp_register(turn_id, agent)
        session_id = ""
        if resume_session_id and _resume_target_exists(workdir, resume_session_id):
            state["replaying"] = True
            try:
                agent.load_session(resume_session_id)
                session_id = resume_session_id
            except Exception as exc:  # noqa: BLE001
                _log(f"turn {turn_id[:8]}: session/load failed ({exc}); starting fresh")
            finally:
                state["replaying"] = False
                # Belt and braces: a replay update that lands just after the
                # load reply would otherwise seed this turn's reply text with
                # the last one's.
                reducer.reset_stream_state()
        if not session_id:
            session_id = agent.new_session()
        _log(f"exec: acp (turn {turn_id[:8]}) in {workdir} session={session_id[:8]}")

        pending = agent.prompt(prompt)
        deadline = time.time() + ACP_TURN_TIMEOUT_SECONDS
        result = None
        while True:
            try:
                result = pending.result(timeout=TRANSCRIPT_FLUSH_SECONDS)
                break
            except TimeoutError:
                flush()          # stream while the turn is still running
                if time.time() > deadline:
                    agent.cancel()
                    raise RuntimeError(
                        f"ACP turn exceeded {ACP_TURN_TIMEOUT_SECONDS}s") from None
        flush()

        stop = (result or {}).get("stopReason", "")
        ok = stop in ("end_turn", "max_tokens")
        final_text = reducer.assistant_text.strip() or f"turn ended: {stop or 'unknown'}"
        if reducer.rate_limit:
            _log(f"turn {turn_id[:8]}: rate limit {reducer.rate_limit.get('status')} "
                 f"({reducer.rate_limit.get('rateLimitType')})")
        return ok, final_text, session_id
    except Exception as exc:  # noqa: BLE001 — a turn must fail, never crash the runner
        flush()
        _log(f"turn {turn_id[:8]}: ACP executor failed: {exc}")
        return False, f"runner error (acp): {exc}", (agent.session_id if agent else "")
    finally:
        # Unregister BEFORE close: a steer that arrives in the gap would
        # otherwise reach a closed connection and raise inside the WS thread.
        _acp_unregister(turn_id)
        if agent is not None:
            try:
                agent.close()
            except Exception:  # noqa: BLE001
                pass


def _content_text_of(update: dict) -> str:
    """The text of an `agent_message_chunk`, tolerant of both block shapes."""
    content = update.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _execute_once(prompt: str, turn_id: str, emit, cwd=None, agent_slug=None,
                  resume_session_id=None) -> tuple[bool, str, str]:
    """The one place a turn picks an executor."""
    if RUNNER_EXECUTOR == "acp":
        return run_acp(prompt, turn_id, emit, cwd=cwd, agent_slug=agent_slug,
                       resume_session_id=resume_session_id)
    return run_claude(prompt, turn_id, emit, cwd=cwd, agent_slug=agent_slug,
                      resume_session_id=resume_session_id)


def execute_prompt(prompt: str, turn_id: str, emit, cwd=None, agent_slug=None,
                   resume_session_id=None) -> tuple[bool, str, str]:
    """Run a turn, failing over down the Claude credential cascade on a usage cap.

    A subscription's weekly cap takes out EVERY agent on this box at once, and an
    unattended agent has no one to tell — on 2026-08-01 the whole fleet sat dead
    on one exhausted login while its drills reported a bare "You've hit your
    weekly limit". So a capped credential is not a turn failure: it is a signal to
    move to the next one and run the turn again.

    Retries are bounded by the number of credentials configured, and only a
    USAGE-CAP failure advances — a turn that failed on its own merits must not be
    re-run against every credential in turn (that would triple the blast radius of
    an ordinary bug and spend real money doing it).
    """
    attempted: list[str] = []
    saw_dead_credential = False
    while True:
        ok, text, session_id = _execute_once(
            prompt, turn_id, emit, cwd=cwd, agent_slug=agent_slug,
            resume_session_id=resume_session_id)
        attempted.append(_claude_cred_label())
        capped, dead = _is_usage_cap(text), _is_auth_required(text)
        if ok or not (capped or dead):
            return ok, text, session_id
        if dead:
            # An unusable credential either way, so it advances the same as a cap
            # — but the REASON is carried to the terminal message below, because
            # "wait for the reset" is wrong advice for a token that has none.
            saw_dead_credential = True
            _log(f"turn {turn_id[:8]}: {_claude_cred_label()} is not signed in")
            _notify_auth_required(_claude_cred_label(), turn_id)
        else:
            _log(f"turn {turn_id[:8]}: {_claude_cred_label()} is at its usage cap")
        if not _advance_claude_credential(turn_id=turn_id):
            # Before giving up: re-read the bundle. An operator who just ran
            # `canopy runner credential` to rescue a stuck box should not also
            # have to restart the service — the credentials are staged into this
            # process at start-up, so without this the fix sits on canopy-web,
            # invisible, until something bounces the runner.
            if _reload_claude_credentials():
                _log(f"turn {turn_id[:8]}: picked up new credentials — retrying")
                resume_session_id = None
                continue
            # Nothing left to fail over to. Say so in the turn's own text: the
            # bare cap message names a reset time but never says the fleet has
            # run out of credentials entirely, which is the thing a human has to
            # act on.
            if saw_dead_credential:
                # Deliberately does NOT mention waiting: this is the state that
                # never clears on its own, and telling an operator a cap will
                # reset is how a box sits dead for a day.
                note = (f"[runner] this box is not signed in to Claude (tried: "
                        f"{', '.join(attempted)}). This will NOT clear on its own — "
                        f"someone has to re-authenticate the subscription and set the "
                        f"token (`canopy runner credential`).")
            else:
                note = (f"[runner] every Claude credential on this box is exhausted "
                        f"(tried: {', '.join(attempted)}). Turns will keep failing "
                        f"until a cap resets or a new credential is set "
                        f"(`canopy runner credential`).")
            return ok, f"{text}\n\n{note}", session_id
        # `--resume` is deliberately dropped on the retry: the failed attempt may
        # have written a partial session, and resuming it under a different
        # credential is not a state we want to debug at 2am.
        resume_session_id = None


def run_claude(prompt: str, turn_id: str, emit, cwd: pathlib.Path | None = None,
               agent_slug: str | None = None, resume_session_id: str | None = None,
               _resume_retried: bool = False) -> tuple[bool, str, str]:
    """Run `claude -p` on the prompt, streaming stream-json events via `emit`
    (a callable taking a list of event dicts — WS or REST). Returns
    (ok, final_text, cli_session_id).

    `cwd` lets the caller run this IN an agent's real clone (see `_turn_cwd`)
    instead of a throwaway scratch dir; None keeps the original scratch-dir
    behavior (project/session turns, or an agent with no bootstrapped clone).
    `agent_slug` layers that agent's provisioned env on top (see `_agent_env`).

    Every raw stream-json line the CLI emits — regardless of whether it parses —
    is ALSO forwarded verbatim to POST /turns/{id}/transcript, batched by bytes
    (never held entirely in memory) and flushed periodically and at the end; see
    `_post_transcript_batch` / TRANSCRIPT_FLUSH_*. This is IN ADDITION to the
    reduced TurnEvents `emit` carries for the live UI — the transcript is the
    durable, re-derivable artifact (cost/structure), the ledger stays the live
    stream (docs/superpowers/specs/2026-07-26-run-execution-convergence-design.md).

    `cli_session_id` is the CLI's own session id, captured from the first event
    that carries one (normally `system`/`init`, fired before any other output) —
    the caller round-trips it through record-session so a LATER turn on the same
    canopy Session can pass it back as `resume_session_id` here.

    `resume_session_id`, if given, is verified FIRST against the local
    filesystem (`_resume_target_exists` — Claude Code resolves `--resume` by
    cwd-derived project dir, so a session captured under a different cwd is
    invisible here regardless of the id) and dropped to a fresh spawn
    immediately if that fails, rather than ever invoking a doomed `--resume`.
    As a second-layer safety net — if the file existed but the CLI still
    yields NOTHING (exits non-zero having emitted no stream-json lines at
    all) — this retries ONCE as a fresh spawn (`_resume_retried` guards
    against looping), mirroring the reuse-then-fall-back-to-create pattern
    runner/canopy_runner/execute.py already uses for emdash sessions: never
    assume continuity works, always have a cold-start fallback.
    """
    workdir = cwd if cwd is not None else pathlib.Path(WORK_DIR) / turn_id[:8]
    workdir.mkdir(parents=True, exist_ok=True)
    if resume_session_id and not _resume_target_exists(workdir, resume_session_id):
        _log(f"resume target {resume_session_id!r} not found under {workdir} "
             f"(turn {turn_id[:8]}); treating as a fresh spawn")
        resume_session_id = None
    cmd = _claude_cmd(prompt, resume_session_id)
    _log(f"exec: claude -p (turn {turn_id[:8]}) in {workdir}"
         + (f" --resume {resume_session_id}" if resume_session_id else ""))
    # stderr goes to a file, not PIPE: a chatty claude can fill the 64KB pipe buffer
    # while we're only reading stdout, deadlocking the process. A file has no such
    # limit; we tail it for the failure path below.
    # A resume-fallback retry (_resume_retried=True) writes to a DIFFERENT file
    # than the original attempt, so a failed --resume's stderr — the only
    # evidence of why it failed — survives the fresh-spawn retry instead of
    # being truncated by its `.open("w")` (review finding M2).
    stderr_path = workdir / ("stderr.resume-retry.log" if _resume_retried else "stderr.log")
    stderr_file = stderr_path.open("w")
    proc = subprocess.Popen(
        cmd, cwd=str(workdir), stdout=subprocess.PIPE, stderr=stderr_file, text=True,
        env=_agent_env(agent_slug),
    )
    final_text = ""
    cli_session_id = ""
    ok = True
    lines_seen = 0
    batch: list[dict] = []
    attempt_id = uuid.uuid4().hex[:8]
    transcript_buf: list[str] = []
    transcript_bytes = 0
    transcript_seq = 0
    transcript_stopped = False  # latched True once the server reports `truncated`
    # Guards transcript_buf/transcript_bytes/transcript_seq/transcript_stopped —
    # held only for FAST, non-blocking mutations (list swap, int increment).
    # Never held across network I/O (review N2): the read loop's per-line
    # append takes this lock on every single line, so if it also covered the
    # POST it would stall stdout-draining for as long as canopy-web is slow —
    # the same full-pipe-stall failure mode N1 fixes, just caused by a slow
    # server instead of a raised exception.
    transcript_lock = threading.Lock()
    # Separate lock serializing actual POSTING order across the two producers
    # (the read loop's own byte-triggered flush, and the periodic thread
    # below) — held ACROSS the network call, unlike transcript_lock. Without
    # this, two concurrent flushes could swap out their content in order but
    # POST in the opposite order if the earlier one is slower (e.g. retrying
    # a 500), corrupting the transcript's byte ordering. Acquired for the
    # whole of one flush_transcript() call (swap included), so at most one
    # flush is ever "in flight".
    #
    # Documented residual (measured, review N2 round 2): this fixes the
    # COMMON case — a turn that stays under TRANSCRIPT_FLUSH_BYTES is never
    # stalled by a slow/failing canopy-web, because the periodic thread is
    # the only thing ever waiting on a POST and the read loop's per-line
    # append only ever touches the fast `transcript_lock`. But if a turn
    # CROSSES the byte trigger WHILE the periodic thread's flush is already
    # mid-POST, the read loop's OWN inline `flush_transcript()` call (below)
    # blocks acquiring `transcript_post_lock` for that entire POST (up to
    # ~182s across retries) before it can even swap its new content out —
    # i.e. the read loop genuinely stalls in that narrower case, not just
    # "another flush". Bounded and rarer than the pre-fix behavior (which
    # stalled on EVERY line, not just a threshold-crossing one), and
    # correctness (never posting out of order) is worth that residual — but
    # it is a real stall on the read loop, not merely a flush-vs-flush one.
    transcript_post_lock = threading.Lock()
    transcript_flush_stop = threading.Event()

    def flush():
        # Swap BEFORE calling emit (same pattern as flush_transcript's swap-
        # before-post): if `emit` raises, `batch` must already be empty, or
        # the `finally` block's own `flush()` call (see below) would re-emit
        # the identical batch a second time — a duplicate-events bug this
        # test suite caught while building a genuine mid-loop-exception case
        # for N1 (a raising `emit` left `batch` non-empty under the old
        # emit-then-clear order).
        nonlocal batch
        if not batch:
            return
        pending = batch
        batch = []
        emit(pending)

    def flush_transcript():
        # transcript_post_lock is held for the WHOLE call (swap through the
        # last POST) so at most one flush is ever in flight and posts land in
        # swap order — but transcript_lock (the one the read loop's per-line
        # append also needs) is only ever held for the swap itself and for
        # each seq allocation, never across `_post_transcript_batch`'s network
        # call (review N2 — see the lock declarations above for why).
        nonlocal transcript_buf, transcript_bytes, transcript_seq, transcript_stopped
        with transcript_post_lock:
            with transcript_lock:
                if not transcript_buf or transcript_stopped:
                    transcript_buf = []
                    transcript_bytes = 0
                    return
                pending = transcript_buf
                transcript_buf = []
                transcript_bytes = 0
            for chunk in _chunk_transcript_lines(pending):
                with transcript_lock:
                    if transcript_stopped:
                        return
                    transcript_seq += 1
                    seq = transcript_seq
                if not _post_transcript_batch(turn_id, attempt_id, seq, chunk):
                    with transcript_lock:
                        transcript_stopped = True
                    return

    def _periodic_flush() -> None:
        # The byte-size flush trigger below only runs when a NEW line arrives,
        # so a long-quiet stdout (a multi-minute tool call — CLAUDE.md's own
        # worked example is 296s) would otherwise hold buffered lines in RAM
        # indefinitely, lost to any crash/SIGTERM/instance-stop in the
        # meantime (review finding I2). This thread is what makes
        # TRANSCRIPT_FLUSH_SECONDS actually periodic rather than "on the next
        # line after N seconds".
        while not transcript_flush_stop.wait(TRANSCRIPT_FLUSH_SECONDS):
            flush_transcript()

    flusher = threading.Thread(
        target=_periodic_flush, daemon=True, name=f"transcript-flush-{turn_id[:8]}",
    )
    flusher.start()

    loop_error: Exception | None = None
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            lines_seen += 1
            should_flush = False
            with transcript_lock:
                transcript_buf.append(line)
                transcript_bytes += len(line.encode("utf-8"))
                should_flush = transcript_bytes >= TRANSCRIPT_FLUSH_BYTES
            # `flush_transcript()` is called AFTER the `with` block exits, on
            # purpose: this is the one line the whole deadlock-freedom
            # argument rests on. flush_transcript() itself acquires
            # transcript_post_lock THEN transcript_lock (L2 -> L1) — calling
            # it while still holding transcript_lock here would attempt the
            # inverse order (L1 already held, then try to take L2 inside),
            # which is exactly the shape of an AB/BA deadlock the moment two
            # threads do it in opposite orders. Releasing L1 first keeps
            # every acquisition in this file on the single L2 -> L1 order.
            if should_flush:
                flush_transcript()
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("session_id"):
                cli_session_id = evt["session_id"]
            etype = evt.get("type")
            if etype == "assistant":
                for block in (evt.get("message", {}).get("content") or []):
                    if block.get("type") == "text" and block.get("text"):
                        batch.append({"kind": "assistant", "payload": {"text": block["text"]}})
                    elif block.get("type") == "tool_use":
                        # `id` here is the tool_use block's own id — the SAME value the
                        # matching tool_result block calls `tool_use_id`. Carrying it on
                        # both events (RC/run-convergence PR3) is what lets the client
                        # pair a tool_end to its tool_start by id instead of by stream
                        # order, which is ambiguous the moment two tool calls overlap
                        # (parallel tool_use) or share a name. Field name is deliberately
                        # "id" here (not "tool_use_id") to match the raw Anthropic
                        # tool_use block shape the frontend's pairing already reads
                        # (content.id / content.tool_use_id — see pairToolMessages.ts).
                        batch.append({
                            "kind": "tool_start",
                            "payload": {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input": block.get("input") or {},
                            },
                        })
            elif etype == "user":
                for block in (evt.get("message", {}).get("content") or []):
                    if block.get("type") == "tool_result":
                        batch.append({
                            "kind": "tool_end",
                            "payload": {
                                "tool_use_id": block.get("tool_use_id", ""),
                                "is_error": bool(block.get("is_error", False)),
                                "content": block.get("content"),
                            },
                        })
            elif etype == "result":
                final_text = evt.get("result", "") or ""
                ok = not evt.get("is_error", False)
            if len(batch) >= 10:
                flush()
    except Exception as exc:  # noqa: BLE001 — must not lose buffered events/transcript
        loop_error = exc
    finally:
        # Everything here runs whether the loop finished cleanly, raised, or
        # the process is still alive (review finding I3: the ORIGINAL code had
        # no try/finally at all, so an exception mid-loop — e.g. the WS `emit`
        # breaking on a dropped socket — dropped every buffered line/event).
        transcript_flush_stop.set()
        # CLOSE STDOUT BEFORE REAPING, unconditionally, regardless of whether
        # the loop reached EOF (review N1). On a mid-loop exception `claude`
        # can still be RUNNING with its stdout pipe no longer drained; once
        # the 64KB pipe buffer fills, the child blocks on write and a bare
        # `proc.wait()` NEVER RETURNS. That doesn't just fail this turn — it
        # means run_claude() itself never returns, so the caller's own
        # `finally: lease_stop.set()` never runs, the lease-renewal thread
        # keeps heartbeating this turn EXECUTING forever, and
        # one_executing_turn_per_agent wedges the whole agent permanently
        # (the exact "wedged-but-heartbeating runner" pathology CLAUDE.md
        # documents) — worse than the turn simply failing. Closing the read
        # end delivers EPIPE/SIGPIPE to a child still blocked writing, which
        # is what actually unblocks it — kill() alone would not (a blocked
        # write doesn't unblock just because the process received SIGKILL any
        # sooner than it would have anyway; closing the pipe is what matters).
        #
        # KILL ONLY IF A BOUNDED WAIT TIMES OUT (review N4) — never
        # unconditionally. `claude` finishing normally often does a little
        # work AFTER its last stream-json line (whatever it takes to close
        # out its own `~/.claude/projects/<enc>/<id>.jsonl` transcript — the
        # very file this PR's `--resume`/I1/C2 depend on); an unconditional
        # SIGKILL the instant stdout EOFs was measured to hit that window and
        # both flip a clean turn's outcome to `ok=False` AND risk truncating
        # that transcript file. `Popen.kill()` on an already-exited process is
        # in fact safe on its own terms (`send_signal` polls `self.poll()`
        # first and no-ops on a dead process, Python 3.9+/bpo-38630) — but
        # "safe to call" was never the point; calling it on a process that
        # was about to exit cleanly, before giving it the chance to, is the
        # bug. Wait first; escalate only on a genuine timeout.
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — best-effort; still try to wait below
            pass
        try:
            proc.wait(timeout=PROC_REAP_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — subprocess.TimeoutExpired or worse: escalate
            try:
                proc.kill()
                proc.wait(timeout=PROC_REAP_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 — truly never block cleanup on this
                _log(f"warn: turn {turn_id[:8]}: could not reap the claude "
                     "subprocess (pid may be leaked) — continuing rather than "
                     "blocking the runner")
        stderr_file.close()
        try:
            flush()  # calls the caller-supplied `emit` — can itself raise (e.g. a dead WS)
        except Exception as exc:  # noqa: BLE001 — cleanup must not lose flush_transcript below
            if loop_error is None:
                loop_error = exc
        flush_transcript()  # never raises — _post_transcript_batch swallows everything
    if loop_error is not None:
        ok = False
        final_text = final_text or f"runner error while streaming claude output: {loop_error}"
    elif proc.returncode != 0 and not final_text:
        ok = False
        try:
            tail = stderr_path.read_text(errors="replace")
        except OSError:
            tail = ""
        final_text = tail[-500:]
    if (resume_session_id and not _resume_retried and lines_seen == 0
            and loop_error is None and proc.returncode != 0):
        # The CLI emitted NOTHING at all before exiting even though the
        # transcript file existed at start (already verified above) — some
        # other resume failure (a corrupt/incompatible file, a CLI version
        # mismatch). Fall back to a fresh spawn rather than surfacing a cold
        # "resume failed" as the turn's result. A failure AFTER real output
        # happened is a genuine task failure and must not retry (that would
        # silently duplicate work/tokens).
        _log(f"resume of session {resume_session_id!r} yielded nothing "
             f"(turn {turn_id[:8]}); falling back to a fresh spawn")
        return run_claude(prompt, turn_id, emit, cwd=cwd, agent_slug=agent_slug,
                           resume_session_id=None, _resume_retried=True)
    return ok, final_text, cli_session_id


def _stage_github_token(token: str) -> None:
    """Make the staged GitHub token usable by BOTH `git` and `gh`.

    The credential helper only teaches `git` — `gh` ignores it entirely and looks
    for its own login or GH_TOKEN. So every agent had working clone/fetch/push but
    `gh auth status` reported "not logged into any GitHub hosts", which blocks the
    PR-based shipping flow the operating model is built on. Readiness drills called
    it out as the single blocking failure for hal after everything else was green.

    Exporting GH_TOKEN is the fix rather than running `gh auth login`: it needs no
    interactive step on a headless box, it is scoped to this process tree (so it is
    inherited by agent turns via `_agent_env`, which copies os.environ), and it
    leaves nothing on disk to go stale.
    """
    os.environ["GH_TOKEN"] = token
    try:
        subprocess.run(["git", "config", "--global", "credential.helper", "store"],
                       check=False, capture_output=True)
        creds = pathlib.Path.home() / ".git-credentials"
        line = f"https://x-access-token:{token}@github.com\n"
        creds.write_text(line)
        creds.chmod(0o600)
    except OSError as exc:
        _log(f"warn: could not stage github token: {exc}")


def _chat_session_id(turn: dict) -> str:
    """The canopy Session id (origin_ref.chat_session_id) for a SESSION-targeted
    turn, or "" for an agent/project turn. This is the one identity that is
    INVARIANT for the life of a conversation — unlike `thread_key` (see
    `_session_thread_key`), which can take other forms (e.g. "emdash:<task>" for
    a runner-discovered binding) — so it is what `_turn_cwd` keys a session's
    on-disk workdir on, not the turn id and not the thread key."""
    return (turn.get("origin_ref") or {}).get("chat_session_id") or ""


def _turn_agent_slug(turn: dict) -> str:
    """The agent this turn runs AS, or "" — the single place that decision is made.

    A chat turn surfaces agent_slug (you chat WITH an agent) but carries its
    session id in origin_ref; that, not a top-level field, is the session signal
    on TurnOut. A live chat is bridged, never run from a checkout, so it is not
    an agent-identity turn. Shared by `_turn_cwd` (which clone to run in) and
    `_agent_env` (whose credentials to load) so the two can never disagree about
    what counts as an agent turn.
    """
    if _chat_session_id(turn):
        return ""
    return turn.get("agent_slug") or ""


def _safe_session_dirname(session_id: str) -> str:
    """A filesystem-safe basename for a session workdir. `session_id` is normally
    a canopy Session UUID (server-controlled at creation, but a turn payload is
    still data off the wire) — this never trusts it enough to join onto a path
    unsanitized, mirroring runner/canopy_runner/canopy_runner/execute.py's
    `_safe_name` for the same reason."""
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in session_id).strip(".-")
    return cleaned[:80] or "unknown-session"


def _turn_cwd(turn: dict, turn_id: str) -> pathlib.Path:
    """Where claude should run for this turn (runner/ec2 design spec §2:
    'agent turns execute in the agent's clone'). An AGENT turn whose slug has a
    bootstrapped clone under AGENT_ROOT (bootstrap_agents.sh, run once per
    service start — see bootstrap_agent_fleet) runs IN that clone, freshly
    `git pull`ed here at claim, so it sees the agent's real repo — config,
    skills, state — not an empty scratch dir. Best-effort: a pull failure logs
    and still uses the clone as-is (stale beats absent).

    A SESSION turn gets a STABLE per-canopy-Session directory
    (WORK_DIR/sessions/<chat_session_id>), checked BEFORE the agent-clone branch
    and never the turn-id scratch dir: Claude Code resolves a `--resume` target
    by the cwd-derived project directory
    (~/.claude/projects/<cwd with '/','.' -> '-'>/<session-id>.jsonl), so a
    session id captured under one cwd is invisible under a different one. Every
    turn on the same conversation MUST share one cwd or `--resume` can never
    resolve — keying on the turn id (the pre-fix behavior) handed every turn on
    a session its own directory and made resume permanently unresolvable.
    A brand-new-per-turn scratch dir remains correct for project/agent turns
    (each is its own unit of work), just not for a session's ongoing thread.

    Everything else (project turns, or an agent bootstrap hasn't reached yet)
    keeps the original scratch-dir behavior."""
    session_id = _chat_session_id(turn)
    if session_id:
        return pathlib.Path(WORK_DIR) / "sessions" / _safe_session_dirname(session_id)
    slug = _turn_agent_slug(turn)
    if slug:
        agent_dir = pathlib.Path(AGENT_ROOT) / slug
        if (agent_dir / ".git").is_dir():
            try:
                subprocess.run(
                    ["git", "-C", str(agent_dir), "pull", "--ff-only"],
                    check=False, capture_output=True, timeout=60,
                )
            except Exception as exc:
                _log(f"warn: git pull in {agent_dir} failed (using clone as-is): {exc}")
            return agent_dir
    return pathlib.Path(WORK_DIR) / turn_id[:8]


def clone_or_pull_canopy_web() -> bool:
    """canopy-web is PUBLIC (github.com/dimagi-internal/canopy-web) — this needs no
    credential, but it still runs from bootstrap_agent_fleet (after credential
    staging), not cloud-init, purely to keep the whole bootstrap sequence in
    one place with one log stream."""
    repo_dir = pathlib.Path(CANOPY_WEB_REPO_DIR)
    try:
        if (repo_dir / ".git").is_dir():
            subprocess.run(["git", "-C", str(repo_dir), "pull", "--ff-only"], check=True, timeout=120)
        else:
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", CANOPY_WEB_REPO_URL, str(repo_dir)],
                check=True, timeout=180,
            )
        _install_transcript_core(repo_dir)
        return True
    except Exception as exc:
        _log(f"warn: could not clone/pull canopy-web ({CANOPY_WEB_REPO_URL}) for bootstrap: {exc}")
        return False


def _expose_repo_package(
    repo_dir: pathlib.Path, name: str, purpose: str, *, parent: str = "packages"
) -> None:
    """Put one in-repo package on `sys.path`, straight from the freshly-cloned repo.

    Taken from the clone rather than an index so it is always the same commit as
    the rest of this deploy.

    This used to `uv pip install --system` (falling back to `python3 -m pip`).
    Both are dead on this AMI, and silently so — the failure was swallowed into
    one `warn:` line while every dependent feature turned itself off:

      * Ubuntu 24.04's system Python is PEP 668 externally-managed, so
        `uv pip install --system` REFUSES ("hint: Virtual environments were not
        considered due to the `--system` flag").
      * `python3 -m pip` does not exist — the AMI ships no pip, and cloud-init's
        apt list never adds python3-pip.

    Observed 2026-07-28: `canopy_transcript` had never been importable on
    cloud-ec2-1, so every session turn it ran wrote no durable rows at all.

    `sys.path` rather than a venv or `--target` because these packages are
    stdlib-only BY CONTRACT — canopy_transcript declares `dependencies = []`
    with a comment explaining that this box has to resolve every dep it adds at
    boot, and canopy_acp depends on nothing but canopy_transcript. So there is
    nothing to resolve, and an installer is machinery in front of a path append.
    A package here that grows a third-party dependency needs a deliberate
    install strategy, and will announce itself as an ImportError in the named
    degradation below rather than failing quietly.

    Best-effort by design: every caller degrades to a named disabled feature if
    the import later fails, and the turn still runs and still finishes. A
    bootstrap step that could brick execution is worse than a missing feature.
    """
    pkg = repo_dir / parent / name
    if not (pkg / name).is_dir():
        _log(f"warn: {pkg} not in the clone; {purpose} disabled")
        return
    entry = str(pkg)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    _log(f"exposed {name} from {pkg}")


def _install_transcript_core(repo_dir: pathlib.Path) -> None:
    """The packages this runner imports off the clone.

    `canopy_acp` is exposed unconditionally rather than only when
    RUNNER_EXECUTOR=acp, so flipping the executor is an env change and a restart
    rather than a redeploy — the point of a switch you can actually use.
    """
    _expose_repo_package(repo_dir, "canopy_transcript", "transcript rows")
    # canopy_acp lives beside the runner programs (runner/canopy_acp), not in
    # packages/ — looking for it there disabled the ACP executor on every boot.
    _expose_repo_package(repo_dir, "canopy_acp", "the ACP executor", parent="runner")
    _install_acp_adapter()


#: Turn id -> the live AcpAgent driving it, for the WS thread to reach into.
#:
#: A turn runs on a worker thread blocked inside `run_acp`'s pump; control frames
#: (`interject`, cancel) arrive on the WS thread. Steering is the one thing that
#: MUST cross those threads — waiting for the pump would deliver the message
#: after the turn it was meant to change. `AcpConnection._send` is lock-guarded,
#: so a prompt from another thread is safe by construction.
_ACP_LIVE: dict = {}
_ACP_LIVE_LOCK = threading.Lock()


def _acp_register(turn_id: str, agent) -> None:
    with _ACP_LIVE_LOCK:
        _ACP_LIVE[turn_id] = agent


def _acp_unregister(turn_id: str) -> None:
    with _ACP_LIVE_LOCK:
        _ACP_LIVE.pop(turn_id, None)


def _acp_agent_for(turn_id: str):
    with _ACP_LIVE_LOCK:
        return _ACP_LIVE.get(turn_id)


def steer_turn(turn_id: str, message: str) -> bool:
    """Deliver a human's message INTO a turn that is already running.

    Returns whether it was delivered, which the caller logs — a message that
    silently goes nowhere is the failure this exists to prevent, and canopy-web
    has already told a person their message was sent.

    `claude -p` cannot do this at all: it is one process, one prompt, stdin
    closed. ACP can — `session/prompt` is a request the agent accepts while a
    previous one is still running, reported as `_meta.steering.supported` and
    `promptQueueing` in `initialize`. Verified on cloud-ec2-1 2026-09-09 against
    adapter 0.75.1: interjected mid-`sleep`, the agent abandoned its loop and
    answered the new instruction, emitting none of the remaining output.

    The returned Pending is deliberately dropped. The interjection's reply
    arrives as ordinary `session/update` traffic, which the reducer already
    turns into ledger rows — the same path the turn's own output takes. Nothing
    needs to await it, and awaiting it here would block the WS thread.
    """
    agent = _acp_agent_for(turn_id)
    if agent is None:
        return False
    try:
        agent.prompt(message)
        return True
    except Exception as exc:  # noqa: BLE001 — a failed steer must not kill the socket
        _log(f"steer turn={turn_id[:8]} failed: {exc}")
        return False


def stop_turn(turn_id: str) -> bool:
    """`session/cancel` — the Escape equivalent. Verified on cloud-ec2-1:
    stopReason `cancelled`, same second."""
    agent = _acp_agent_for(turn_id)
    if agent is None:
        return False
    try:
        agent.cancel()
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"stop turn={turn_id[:8]} failed: {exc}")
        return False


#: The adapter's binary and package names. `find_adapter` in canopy_acp resolves
#: the binary off PATH first, so having it there is the whole success condition.
ACP_ADAPTER_BIN = "claude-agent-acp"
ACP_ADAPTER_PACKAGE = "@agentclientprotocol/claude-agent-acp"


def _install_acp_adapter() -> None:
    """Put the Node ACP adapter on PATH, if it is not already there.

    Skipped entirely unless RUNNER_EXECUTOR=acp: it costs an npm install and a
    Node dependency on every boot, and the `claude -p` path needs neither.
    `run_acp` falls back to `claude -p` when the adapter is missing, so a failure
    here degrades rather than breaks.

    Two things this got wrong, both measured on cloud-ec2-1 on 2026-09-09, and
    both already solved a few hundred lines away for `tsx` in
    bootstrap_agents.sh's `ensure_plugin_runtime`:

    * **A bare `npm install -g` cannot work here.** This runs as the SERVICE user
      (ubuntu), which cannot write /usr/lib/node_modules, so npm exits 243
      (EACCES). `$HOME/.local/bin` is already first on the unit's PATH
      (runner.cfn.yaml), so `--prefix "$HOME/.local"` installs somewhere the
      adapter is actually found.
    * **It never asked whether the adapter was already installed.** With one
      present at /usr/bin/claude-agent-acp it still ran the doomed install and
      logged "could not install the ACP adapter … the ACP executor will fall
      back to claude -p" — on a boot that then executed every turn over ACP
      perfectly well. A warning that names the wrong outcome is worse than
      silence: it sends the next person debugging in the opposite direction.
    """
    if RUNNER_EXECUTOR != "acp":
        return
    existing = shutil.which(ACP_ADAPTER_BIN)
    if existing:
        _log(f"ACP adapter already on PATH ({existing})")
        return
    if shutil.which("node") is None or shutil.which("npm") is None:
        _log("warn: node/npm not on PATH; the ACP executor will fall back to claude -p")
        return
    prefix = str(pathlib.Path.home() / ".local")
    try:
        subprocess.run(["npm", "install", "-g", "--prefix", prefix, ACP_ADAPTER_PACKAGE],
                       check=True, timeout=300,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _log(f"installed {ACP_ADAPTER_PACKAGE} into {prefix}")
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: could not install the ACP adapter ({exc}); "
             "the ACP executor will fall back to claude -p")


def bootstrap_agent_fleet() -> None:
    """Clone/pull canopy-web to CANOPY_WEB_REPO_DIR and run its
    runner/ec2/bootstrap_agents.sh — the agent-fleet provisioning step
    (design spec §2). Runs once per service start, here in main(), and
    DELIBERATELY NOT from the systemd unit's ExecStartPre / cloud-init:

    bootstrap_agents.sh clones PRIVATE per-agent repos (needs the GITHUB_TOKEN
    this process just staged into the git credential store) and runs `canopy
    provision` (needs OP_SERVICE_ACCOUNT_TOKEN, which fetch_and_stage_credential
    just put in os.environ). Neither exists until an operator has staged this
    runner's credential bundle via wire.sh — which can only happen AFTER the
    runner has paired and appeared in the fleet. An ExecStartPre fires on
    EVERY service start, including the very first one (no credential yet), so
    it would either wedge the unit waiting on a chicken-and-egg secret or
    silently skip cloning the private repos — this call site is the earliest
    point at which the credentials are guaranteed to exist.

    Best-effort end to end: a failure here (network hiccup, one agent's
    manifest broken) is logged loudly and the runner proceeds to claim turns
    anyway — whatever DID bootstrap clean (canopy-web itself, other agents,
    or just project/session turns with no agent clone at all) still works;
    see bootstrap_agents.sh step 5 for the same policy one level down.
    """
    if not clone_or_pull_canopy_web():
        return
    script = pathlib.Path(CANOPY_WEB_REPO_DIR) / "runner" / "ec2" / "bootstrap_agents.sh"
    if not script.exists():
        # One release of fallback: a box whose clone predates the runner/ move
        # (or whose cloud_runner.py outlives it) still finds the script.
        legacy = pathlib.Path(CANOPY_WEB_REPO_DIR) / "deploy" / "ec2-runner" / "bootstrap_agents.sh"
        if legacy.exists():
            script = legacy
        else:
            _log(f"warn: {script} not found — skipping agent bootstrap")
            return
    env = dict(os.environ)
    env.setdefault("AGENT_ROOT", AGENT_ROOT)
    _log(f"running {script}")
    try:
        # Inherits stdout/stderr (no PIPE capture) so its OK/WARN/FAIL lines land
        # straight in `journalctl -u canopy-runner` alongside everything else.
        proc = subprocess.run(["bash", str(script)], env=env, timeout=900)
        _log(f"bootstrap_agents.sh exited {proc.returncode}")
    except Exception as exc:
        _log(f"warn: bootstrap_agents.sh failed to run: {exc}")


# ── Claude credential cascade ───────────────────────────────────────────────
# Ordered: the subscriptions first (included in what we already pay for), the API
# key last (metered — every token spends money, so reaching it notifies a human).
# Each entry is (label, env_var, value). The two auth env vars are MUTUALLY
# EXCLUSIVE: claude prefers CLAUDE_CODE_OAUTH_TOKEN when both are set, so
# selecting a credential must CLEAR the other or the API key can never take over.
_CLAUDE_CREDS: list[tuple[str, str, str]] = []
_CLAUDE_CRED_I = 0
_CLAUDE_CRED_RUNNER_ID = ""   # set at staging; lets an exhausted cascade re-read
_CLAUDE_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

#: A usage cap, matched by SHAPE rather than by phrase.
#:
#: Anthropic ships session (5-hour), daily and weekly caps and words each one a
#: little differently — "hit your session limit", "reached your weekly usage
#: limit" — and adds to the set over time. The original list enumerated the
#: phrasings it knew, which is precisely how the session cap went unmatched
#: until 2026-09-08: a turn on cloud-ec2-1 re-queued three times against the
#: same capped credential and reported a bare failure. So match the shape a cap
#: message always has — a hit/reached verb, then "limit" close behind — and keep
#: an explicit list only for the phrasings that never say "limit".
_USAGE_CAP_RE = re.compile(
    r"\b(?:hit|reached)\b[^.\n]{0,40}?\blimits?\b"
    r"|\blimits?\s+reached\b",
    re.IGNORECASE,
)

_USAGE_CAP_MARKERS = (
    "out of usage",
    "rate limit exceeded",
    "insufficient credit",
    "credit balance is too low",
)

#: Substrings that mean "this credential is DEAD and a human must sign in".
#:
#: A cap and an expired token both stop the box, and the runner used to have a
#: name for only one of them. The difference is the whole point: a cap fixes
#: itself when the clock runs out, a subscription token never does. Read out of
#: the shipped `claude` binary; deliberately narrow, because turn text is AGENT
#: output and an agent that merely writes about `/login` must not mark the fleet
#: dead. Unlike the caps there is no shape to match here — these are fixed
#: sentences, not a family that grows a new adjective each release.
_AUTH_REQUIRED_MARKERS = (
    "not logged in",
    "please run /login",
    "auth token expired or invalid",
    "refresh token expired",
    "invalid api key",
)


def _is_usage_cap(text: str) -> bool:
    low = (text or "").lower()
    return bool(_USAGE_CAP_RE.search(low)) or any(m in low for m in _USAGE_CAP_MARKERS)


def _is_auth_required(text: str) -> bool:
    """True when the credential needs a human to sign in again.

    Checked ALONGSIDE `_is_usage_cap`, never instead of it — the two states are
    disjoint and want opposite responses (wait vs. fetch a person).
    """
    low = (text or "").lower()
    return any(m in low for m in _AUTH_REQUIRED_MARKERS)


def _claude_cred_label() -> str:
    if _CLAUDE_CRED_I < len(_CLAUDE_CREDS):
        return _CLAUDE_CREDS[_CLAUDE_CRED_I][0]
    return "none"


def _apply_claude_credential(index: int) -> None:
    """Point the environment at credential `index`, clearing the other auth var."""
    global _CLAUDE_CRED_I
    _CLAUDE_CRED_I = index
    label, var, value = _CLAUDE_CREDS[index]
    for other in _CLAUDE_AUTH_VARS:
        os.environ.pop(other, None)
    os.environ[var] = value
    _log(f"claude credential: using {label} ({var})")


def _advance_claude_credential(*, turn_id: str = "") -> bool:
    """Move to the next credential. False when the cascade is exhausted.

    Notifies on the step INTO the API key, because that one bills per token —
    the operator asked to be told before we start spending, not after.
    """
    nxt = _CLAUDE_CRED_I + 1
    if nxt >= len(_CLAUDE_CREDS):
        return False
    label, var, _ = _CLAUDE_CREDS[nxt]
    _apply_claude_credential(nxt)
    if var == "ANTHROPIC_API_KEY":
        _notify_api_key_fallback(label, turn_id)
    return True


def _notify_api_key_fallback(label: str, turn_id: str) -> None:
    """Tell canopy-web we have fallen back to metered billing.

    Best-effort by design (a failed notify must never take down the turn that
    triggered it) but NOT silent: a failure to notify is logged, because the
    whole point of this call is that a human finds out money is being spent.
    """
    body = {
        "items": [{
            "source": "runner.credential",
            "kind": "claude_api_key_fallback",
            "level": "warn",
            "summary": (f"Both Claude subscriptions on {RUNNER_NAME or 'this runner'} are "
                        f"at their usage cap — falling back to the metered API key "
                        f"({label}). Turns now bill per token until a cap resets."),
            # Coalescing key: one row per runner per day, not one per turn. The
            # fallback is a STATE a human needs to know about once, not a stream.
            "key": f"api-key-fallback:{RUNNER_NAME}",
        }],
    }
    try:
        status, _ = _api("POST", "/", body, prefix="/api/events")
        if status not in (200, 201):
            _log(f"warn: API-key fallback notify returned {status} — a human may not know "
                 "that metered billing has started")
    except Exception as exc:  # noqa: BLE001 — notifying must never break the turn
        _log(f"warn: could not notify about API-key fallback ({exc}) — metered billing "
             "has started and nobody has been told")


def _notify_auth_required(label: str, turn_id: str) -> None:
    """Tell canopy-web that a credential needs a human to sign in again.

    The sibling of `_notify_api_key_fallback`, and the more urgent of the two:
    a fallback means money is being spent, this means nothing is running at all
    and no amount of waiting will change that. Best-effort like its sibling, and
    logged when it fails for the same reason — the entire point is that a person
    finds out.
    """
    body = {
        "items": [{
            "source": "runner.credential",
            "kind": "claude_auth_required",
            "level": "error",
            "summary": (f"The Claude credential ({label}) on "
                        f"{RUNNER_NAME or 'this runner'} is no longer signed in. "
                        f"Turns will keep failing until someone re-authenticates it "
                        f"— this does NOT reset on its own like a usage cap."),
            # One row per runner per day: a dead token is a STATE a human acts on
            # once, not a stream of one row per failed turn.
            "key": f"auth-required:{RUNNER_NAME}",
        }],
    }
    try:
        status, _ = _api("POST", "/", body, prefix="/api/events")
        if status not in (200, 201):
            _log(f"warn: auth-required notify returned {status} — a human may not know "
                 "that this box needs to be signed in again")
    except Exception as exc:  # noqa: BLE001 — notifying must never break the turn
        _log(f"warn: could not notify about the dead credential ({exc}) — the box is "
             "unauthenticated and nobody has been told")


def _reload_claude_credentials() -> bool:
    """Re-read the credential bundle mid-run. True when it yielded something new.

    Only called once the in-memory cascade is spent, so the cost is paid exactly
    when a box is otherwise dead. Compares VALUES, not just count: rotating a
    capped subscription in place is the common rescue, and that leaves the length
    unchanged.
    """
    if not _CLAUDE_CRED_RUNNER_ID:
        return False
    before = [c[2] for c in _CLAUDE_CREDS]
    try:
        status, cred = _api("GET", f"/runners/{_CLAUDE_CRED_RUNNER_ID}/credential")
    except Exception as exc:  # noqa: BLE001 — a failed reload must not mask the cap
        _log(f"warn: could not re-read credentials ({exc})")
        return False
    if status != 200 or not cred:
        return False
    fresh = [(label, var, cred[key])
             for label, var, key in (
                 ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "claude_token"),
                 ("subscription-2", "CLAUDE_CODE_OAUTH_TOKEN", "claude_token_secondary"),
                 ("api-key", "ANTHROPIC_API_KEY", "claude_api_key"))
             if cred.get(key)]
    if not fresh or [c[2] for c in fresh] == before:
        return False
    _CLAUDE_CREDS.clear()
    _CLAUDE_CREDS.extend(fresh)
    _apply_claude_credential(0)
    _log("re-read credential bundle: " + ", ".join(c[0] for c in _CLAUDE_CREDS))
    return True


def fetch_and_stage_credential(runner_id: str) -> bool:
    """A CLOUD runner owns no secrets at boot beyond its PAT — it fetches its
    credential bundle from canopy-web (the per-runner hub) and stages it into the
    environment. Blocks (polling) until the Claude token is set, so the operator can
    provision the runner AFTER it has paired and appeared in the fleet. A laptop
    runner never does this — it uses emdash's ambient auth.
    """
    while not _stop:
        status, cred = _api("GET", f"/runners/{runner_id}/credential")
        if status == 200 and cred and (cred.get("claude_token") or cred.get("claude_api_key")):
            global _CLAUDE_CRED_RUNNER_ID
            _CLAUDE_CRED_RUNNER_ID = runner_id
            _CLAUDE_CREDS.clear()
            for label, var, key in (
                ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "claude_token"),
                ("subscription-2", "CLAUDE_CODE_OAUTH_TOKEN", "claude_token_secondary"),
                ("api-key", "ANTHROPIC_API_KEY", "claude_api_key"),
            ):
                if cred.get(key):
                    _CLAUDE_CREDS.append((label, var, cred[key]))
            _apply_claude_credential(0)
            _log(f"claude credential cascade: {', '.join(c[0] for c in _CLAUDE_CREDS)}")
            if len(_CLAUDE_CREDS) == 1:
                _log("warn: only ONE Claude credential is set — a usage cap will stop "
                     "every agent on this box with nothing to fail over to "
                     "(`canopy runner credential` adds a fallback)")
            if cred.get("op_sa_token"):
                os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = cred["op_sa_token"]
            if cred.get("github_token"):
                _stage_github_token(cred["github_token"])
            _log("staged credential bundle from canopy-web (claude"
                 f"{'+op' if cred.get('op_sa_token') else ''}"
                 f"{'+github' if cred.get('github_token') else ''})")
            return True
        _log("waiting for this runner's credential bundle to be set on canopy-web…")
        time.sleep(POLL_SECONDS)
    return False


# ── browser-driven re-authentication (`claude setup-token` under a pty) ─────
# A cloud runner's subscription token expires often, and when one does the box
# does not slow down — it stops, and stays stopped until a person signs in again.
# That used to need a terminal on the box.
#
# It cannot be done in canopy-web: `setup-token` runs PKCE with a verifier
# generated by whoever starts it, so canopy-web cannot exchange a code a browser
# collected — it is not the OAuth client and must not pretend to be. So the REAL
# CLI runs here, under a pty, and canopy-web relays only a URL out to a human and
# a code back. `claude` stays the OAuth client; the verifier never leaves the box.
#
# INLINE, and that is not a style choice: the deploy ships cloud_runner.py as a
# SINGLE file (up.sh publishes one gzip blob; update_runner.sh copies one path),
# so a sibling module is never delivered at all. This first shipped as
# runner/ec2/claude_mint.py in #700 and would have been dead code on every box.
#
# `setup-token` is an Ink TUI: with no tty it prints nothing and waits forever,
# so it cannot be driven with pipes. It does render the URL on purpose ("Browser
# didn't open? Use the url below to sign in"), which makes the URL a supported
# affordance rather than something prised out of a redraw.

#: The CLI wraps the URL in an OSC-8 hyperlink, which puts the whole thing in
#: the terminal's *escape* stream — twice, and split by the wrap. Strip these
#: before anything else or the URL is recovered with control bytes embedded.
_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: `claude.com/cai/oauth/authorize`, NOT `claude.ai/oauth/...`. Worth stating:
#: the binary contains `https://claude.ai/oauth/...` strings too, and matching
#: those finds nothing at runtime.
_AUTHORIZE = re.compile(r"https://claude\.com/cai/oauth/authorize\?[^\s\x00-\x1f\"'<>]+")

#: The minted credential. The PREFIX IS NOT OURS TO PIN: the token is chosen by
#: Anthropic's token endpoint and merely displayed by the CLI (`children: S.token`
#: in the setup-token view), so `\bsk-ant-oat…` made this runner depend on a
#: taxonomy nobody promised us. On 2026-09-08 a sign-in COMPLETED — the CLI
#: printed "Long-lived authentication token created successfully!" — and the
#: runner discarded the credential and reported failure, because it did not
#: start with `oat`. The human did everything right and was told it had failed.
#:
#: So the CLI's OWN ANNOUNCEMENT is the anchor, not a guess at the format. Note
#: the whitespace-tolerance: the TUI positions text with cursor moves rather than
#: spaces, so after `strip_terminal` the banner reads "YourOAuthtoken(validfor1year):".
_TOKEN_BANNER = re.compile(
    r"Your\s*OAuth\s*token.{0,120}?(sk-ant-[A-Za-z0-9_\-]{16,})", re.S | re.I)
#: The CLI's own declaration that the exchange COMPLETED. Whitespace-tolerant
#: for the same reason as the banner: the TUI positions with cursor moves.
_TOKEN_ANNOUNCED = re.compile(
    r"Long-lived\s*authentication\s*token\s*created|Your\s*OAuth\s*token", re.I)
#: Preferred when it is present — a known-good historical format.
_TOKEN_OAT = re.compile(r"sk-ant-oat[A-Za-z0-9_\-]{8,}")
#: Last resort: any credential that is NOT one of the kinds we know are wrong.
#: `api…`/`admin…` are different credentials that would be staged into the wrong
#: env var and fail confusingly.
_TOKEN_ANY = re.compile(r"sk-ant-(?!api|admin)[A-Za-z0-9_\-]{16,}")


def strip_terminal(raw: bytes) -> str:
    """Terminal bytes -> the text a human would see, plus the escaped URL.

    Deliberately keeps OSC-8 *payloads* rather than dropping them: the URL lives
    inside the hyperlink escape, so a naive "remove all escapes" pass throws away
    the one thing worth reading. Carriage returns go because the TUI redraws with
    them, which otherwise splices unrelated frames into one line.
    """
    text = _CSI.sub(b"", raw)
    # OSC-8 opens as ESC ] 8 ; id=… ; <URL> BEL — keep everything after the last
    # ';' in the introducer, which is the URL itself.
    def _keep_url(m: "re.Match[bytes]") -> bytes:
        body = m.group(0)
        url = body.rsplit(b";", 1)[-1].rstrip(b"\x07\x1b\\")
        # Newline-delimited, and that is load-bearing rather than tidiness. The
        # hyperlink escape sits flush against the visible copy the TUI also
        # prints, so without a separator the two run together and the URL regex
        # — which cannot stop at "https" — happily matches one-and-a-bit copies.
        # The result starts correctly, ends in garbage, and fails only in a
        # browser. Caught by the real capture; a hand-written fixture would not
        # have had the adjacency that causes it.
        return b"\n" + url + b"\n"
    text = _OSC.sub(_keep_url, text)
    return text.decode("utf-8", "replace").replace("\r", "\n")


#: Every parameter the authorize request needs to be usable. Acceptance is
#: SEMANTIC rather than "does it look like a URL", because the failure this
#: prevents parses perfectly as one: the first live run stored 80 characters —
#: exactly the terminal width — because `_pump` answers on the first match and
#: the visible copy's first WRAPPED line reaches the buffer before the OSC-8
#: payload carrying the whole URL. The human got "Invalid OAuth Request /
#: Missing redirect_uri parameter". A fixture of the finished output cannot
#: catch that; a stream is not a buffer.
_REQUIRED_AUTHORIZE_PARAMS = (
    "client_id=", "response_type=", "redirect_uri=",
    "code_challenge=", "code_challenge_method=", "state=",
)


def extract_authorize_url(raw: bytes) -> str | None:
    """A COMPLETE URL to put in front of a human, or None if it is not all here.

    None means "not yet", and the caller keeps reading — so a partial render can
    only ever delay the answer, never corrupt it.
    """
    # The TUI prints the URL twice — inside the hyperlink escape, and as visible
    # text it wraps at the terminal width. Longest first, then completeness: the
    # wrapped copy is a truncation, and a truncated authorize URL fails on an
    # invalid-request page rather than on anything that looks like our bug.
    for url in sorted(_AUTHORIZE.findall(strip_terminal(raw)), key=len, reverse=True):
        if all(p in url for p in _REQUIRED_AUTHORIZE_PARAMS):
            return url
    return None


def extract_token(raw: bytes) -> str | None:
    """The minted long-lived token, once the exchange has completed.

    Three passes, most-grounded first: what the CLI SAID is its OAuth token,
    then the known `oat` format, then anything credential-shaped that is not a
    kind we know is wrong. A miss here is not cosmetic — it throws away a
    credential the human has already successfully created.

    THE BANNER OUTRANKS OUR PREFIX OPINION, and that ordering is deliberate:
    under "Your OAuth token (valid for 1 year):" the CLI is naming the value,
    and pass 1 therefore does NOT apply the api/admin exclusion that pass 3
    does. Adding it there would re-create the very bug this function exists to
    fix — our taxonomy overruling a direct statement from the tool that owns
    the credential. The exclusion belongs on the UNANCHORED pass, where we are
    guessing, and nowhere else.
    """
    text = strip_terminal(raw)
    m = _TOKEN_BANNER.search(text)
    if m:
        return m.group(1)
    m = _TOKEN_OAT.search(text) or _TOKEN_ANY.search(text)
    return m.group(0) if m else None


#: Anything token-shaped is stripped before the CLI's own words are reported.
#: The transcript is diagnostic, not a place to spill a credential.
_TOKENISH = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def _diagnostic_tail(raw: bytes, limit: int = 600) -> str:
    """The last thing `setup-token` actually said, fit to be shown to a human.

    This exists because the first two live failures were debugged by GUESSING.
    The runner knew exactly what the CLI printed — "invalid code", "expired", a
    prompt still waiting — and threw it away, reporting only that no token had
    appeared. Two wrong theories and two wasted sign-in attempts later: report
    what it said.
    """
    text = strip_terminal(raw)
    text = _TOKENISH.sub("<redacted>", text)
    # Collapse the TUI's redraw frames — spinner rows carry no information and
    # would otherwise be the entire tail.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    seen, newest_first = set(), []
    for ln in reversed(lines):
        if ln in seen:
            continue
        seen.add(ln)
        newest_first.append(ln)
        if sum(len(k) for k in newest_first) > limit:
            break
    # Trim from the OLD end. This used to join oldest-first and then slice
    # `[:limit]`, which deletes the NEWEST line — the CLI's verdict, and the
    # entire reason this function exists. Seen live on 2026-09-08: a tail whose
    # final segment was the single character "O", the decapitated head of
    # "OAuth error: Request failed with status code 400". It reads as "the CLI
    # said nothing", which is the exact wrong conclusion, and it was reached.
    chosen: list[str] = []
    total = 0
    for ln in newest_first:
        cost = len(ln) + (3 if chosen else 0)
        if chosen and total + cost > limit:
            break
        chosen.append(ln)
        total += cost
    # The slice now bites only when a SINGLE line is over budget; for one line
    # the head is the informative half, so keeping it is right.
    return " | ".join(reversed(chosen))[:limit]


#: How long to let a pasted code settle before sending Enter as a separate
#: keypress. Generous on purpose: it is paid once per sign-in, against a human
#: who has just spent a minute in a browser, and the failure it prevents costs
#: them the whole attempt plus 90 seconds of watching a spinner.
_PASTE_SETTLE_SECONDS = 0.5


class MintTimeout(RuntimeError):
    """The CLI never reached the expected step. Always fatal to the attempt:
    a half-driven TUI is not a state worth resuming."""


class MintUnreadableToken(MintTimeout):
    """The sign-in SUCCEEDED and we could not read the credential out.

    A different fault from "no token appeared", and collapsing the two is what
    made 2026-09-08 expensive: a human signed in correctly, Claude issued a
    year-long token, the runner failed to match its prefix, and the operator was
    told "setup-token never printed a token". That sentence sent the debugging
    at the sign-in — which had worked perfectly — instead of at the six-line
    parser, and cost a second attempt to notice.

    Distinguished because the two need OPPOSITE responses: this one is a runner
    bug to fix and never to retry blindly, while a real failure is worth another
    go. It is also the more urgent of the two, since a credential was minted and
    then lost."""


class MintSession:
    """One `claude setup-token` run, held open across two HTTP round-trips.

    The session is deliberately short-lived and single-use. A mint that is
    started and never finished holds a pty and a child process, so `close()` is
    unconditional and every wait is bounded — an operator who wanders off must
    not strand a process on the box.
    """

    def __init__(self, argv: tuple[str, ...] = ("claude", "setup-token"),
                 env: dict[str, str] | None = None) -> None:
        self._argv = argv
        self._env = env
        self._pid: int | None = None
        self._fd: int | None = None
        self._buf = b""

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self, timeout: float = 60.0) -> str:
        """Spawn the CLI and return the URL a human must open."""
        env = dict(os.environ if self._env is None else self._env)
        # The box has no display. Left alone, `claude` tries to open a browser
        # and (on a headless host) can block or emit noise; the URL we want is
        # printed either way, so the launch is simply neutralised.
        env["BROWSER"] = "/usr/bin/true"
        pid, fd = pty.fork()
        if pid != 0:
            # A pty defaults to 80 columns, which is what made the TUI wrap the
            # authorize URL and hand us an 80-char prefix. Belt to the parser's
            # braces: a wide terminal means the visible copy is not chopped at
            # all, so the two defences fail independently rather than together.
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 50, 400, 0, 0))
            except OSError:
                pass  # a narrower terminal still works — the parser gates it
        if pid == 0:  # pragma: no cover — the child never returns
            os.execvpe(self._argv[0], list(self._argv), env)
            os._exit(1)
        self._pid, self._fd = pid, fd
        url = self._pump(timeout, extract_authorize_url)
        if url is None:
            self.close()
            raise MintTimeout("setup-token never printed an authorize URL")
        return url

    def submit_code(self, code: str, timeout: float = 90.0) -> str:
        """Type the code the human pasted, and return the minted token.

        The RETURN IS ITS OWN WRITE, and the settle between the two is the whole
        fix rather than caution. Sent as one chunk — `code + b"\\r"` — an older
        CLI reads the arrival as a PASTE and takes the trailing carriage return
        as the last CHARACTER of the pasted text, not as Enter. The code lands
        in the input, nothing submits, and the CLI says nothing at all: the pump
        then burns its full timeout and reports "never printed a token" for what
        is really "never asked".

        Measured on the box (`claude` 2.1.197, 2026-09-08), all three variants
        against a live `setup-token`:

            code + CR, one write   -> silent, no submit      <- the bug
            code, settle, then CR  -> submits, CLI answers in 0.6s
            code, settle, then LF  -> silent, no submit

        The render is the tell: the one-write case masks the WHOLE field, the
        split case reveals its last six characters — because in the first the
        carriage return is sitting in the value. Newer CLIs (2.1.266) submit
        either way, which is why this was invisible in development and cost a
        human four sign-in attempts against a box one npm-install behind.
        """
        if self._fd is None:
            raise MintTimeout("this mint session is not running")
        os.write(self._fd, code.strip().encode())
        time.sleep(_PASTE_SETTLE_SECONDS)
        os.write(self._fd, b"\r")
        token = self._pump(timeout, extract_token)
        tail = _diagnostic_tail(self._buf)
        # Ask BEFORE close(): the buffer is the only witness that the exchange
        # completed, and the redaction in `tail` deliberately removes the token
        # itself, so this is the last moment the distinction can be drawn.
        announced = bool(_TOKEN_ANNOUNCED.search(strip_terminal(self._buf)))
        self.close()
        if token is None and announced:
            raise MintUnreadableToken(
                "the sign-in SUCCEEDED — Claude issued a token — but this runner "
                "could not read it out of the CLI's output, so the credential is "
                "lost and a new sign-in is needed. That is a bug in the runner, "
                "not anything you did. What it said: " + (tail or "(nothing)"))
        if token is None:
            raise MintTimeout(
                "setup-token never printed a token. What it said: " + (tail or "(nothing)"))
        return token

    def close(self) -> None:
        """Idempotent: called on every exit path, including the happy one."""
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGKILL)
                os.waitpid(self._pid, 0)
            except (ProcessLookupError, ChildProcessError, OSError):
                pass
            self._pid = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    # ── internals ──────────────────────────────────────────────────────────
    def _pump(self, timeout, extract):
        """Read until `extract` finds something, or time runs out.

        Reads accumulate into one buffer rather than being examined per-chunk:
        the URL routinely arrives split across reads, and a per-chunk match would
        find it only when the boundaries happened to fall kindly.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready, _, _ = select.select([self._fd], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 65536)
            except OSError:  # the child exited and closed the pty
                break
            if not chunk:
                break
            self._buf += chunk
            found = extract(self._buf)
            if found:
                return found
        return extract(self._buf)


#: The sign-in in flight on this box, if any. One at a time by construction —
#: canopy-web supersedes an unfinished mint when a new one is requested, so a
#: second concurrent pty would only be a leak.
_MINT_SESSION = None
_MINT_ID = ""


def _drain_mint(runner_id: str) -> None:
    """Advance a browser-driven sign-in by one step, on the poll tick.

    POLLED rather than pushed, the same lesson as menu-answers: a control frame
    published while the WS channel is down reaches a group with no consumer and
    is silently dropped, while the runner keeps heartbeating and reads ONLINE.

    Every failure path reports back. A mint that dies quietly leaves a human
    watching a spinner for a URL that is never coming, which is worse than the
    terminal they were trying to avoid.
    """
    global _MINT_SESSION, _MINT_ID
    try:
        status, payload = _api("GET", f"/runners/{runner_id}/mint/claim")
        if status != 200 or not isinstance(payload, dict):
            return
        mint = payload.get("mint")
        if not mint:
            # Nothing owed. Drop a pty we may still be holding: either the human
            # is mid-sign-in (and this box has already handed over the URL), or
            # the attempt was superseded and the old process is just a leak.
            if _MINT_SESSION is not None and not _MINT_ID:
                _MINT_SESSION.close()
                _MINT_SESSION = None
            return
        mint_id, state = mint.get("id", ""), mint.get("status", "")

        if state == "requested":
            if _MINT_SESSION is not None:
                _MINT_SESSION.close()
            _log(f"mint {mint_id[:8]}: starting claude setup-token")
            _MINT_SESSION, _MINT_ID = MintSession(), mint_id
            try:
                url = _MINT_SESSION.start()
            except Exception as exc:  # noqa: BLE001 — always tell the waiting human
                _MINT_SESSION = None
                _MINT_ID = ""
                _api("POST", f"/runners/{runner_id}/mint/result",
                     {"detail": f"could not start the sign-in on this box: {exc}"})
                return
            _api("POST", f"/runners/{runner_id}/mint/url", {"url": url})
            _log(f"mint {mint_id[:8]}: authorize URL sent")

        elif state == "completing":
            code = payload.get("code") or ""
            if _MINT_SESSION is None or _MINT_ID != mint_id:
                # The CLI is gone (a restart mid-flow), and its PKCE verifier with
                # it — so this code can never be exchanged. Say so plainly rather
                # than leaving the row spinning; the human simply starts again.
                _api("POST", f"/runners/{runner_id}/mint/result",
                     {"detail": "the sign-in was interrupted on this box "
                                "(the runner restarted) — please start again"})
                _MINT_ID = ""
                return
            if not code:
                return  # already consumed; the result post is what ends this
            try:
                token = _MINT_SESSION.submit_code(code)
            except Exception as exc:  # noqa: BLE001
                # str(exc) now carries the CLI's own words — that is the point.
                _api("POST", f"/runners/{runner_id}/mint/result",
                     {"detail": str(exc)[:900]})
            else:
                # The token goes straight to canopy-web, which writes it into the
                # encrypted bundle — it never touches the browser, so the only
                # secret a human handled was the single-use code.
                _api("POST", f"/runners/{runner_id}/mint/result", {"token": token})
                # The FORMAT, never the secret: 12 characters is the prefix and
                # nothing else, and it is the one fact we lacked when a real
                # token was discarded for not looking like the format we assumed.
                _log(f"mint {mint_id[:8]}: signed in; new token stored "
                     f"(format {token[:12]}…)")
                if _reload_claude_credentials():
                    _log("picked up the freshly minted credential")
            finally:
                _MINT_SESSION = None
                _MINT_ID = ""
    except Exception as exc:  # noqa: BLE001 — never take the main loop down
        _log(f"mint drain error: {exc}")


# ── session continuity (resolve-session / record-session round-trip) ───────
def _session_thread_key(turn: dict) -> str:
    """The key resolve-session/record-session use for a SESSION-targeted turn, or
    "" for an agent/project turn. apps/canopy_sessions/services.py always stamps
    BOTH `thread_key` and `chat_session_id` together on a chat send's origin_ref;
    `thread_key` is what the RPCs key on (it can be "emdash:<task>" for a
    runner-discovered binding, unlike `_chat_session_id`, which is always the
    canopy Session's own id — that's what `_turn_cwd` keys the workdir on)."""
    ref = turn.get("origin_ref") or {}
    session_id = _chat_session_id(turn)
    if not session_id:
        return ""
    return ref.get("thread_key") or session_id


# Claude Code resolves a `--resume <id>` target by cwd, not by id alone: the
# transcript lives at ~/.claude/projects/<cwd with '/','.' -> '-'>/<id>.jsonl.
# Mirrors runner/canopy_runner/canopy_runner/transcript.py's
# `encode_project_dir` (duplicated, not imported: that package pulls in
# non-stdlib deps and this runner is deliberately stdlib-only).
CLAUDE_PROJECTS_HOME = pathlib.Path.home() / ".claude" / "projects"


def _encode_project_dir(cwd: pathlib.Path) -> str:
    """Claude Code's ~/.claude/projects/<name> encoding: '/', '.' and '_' -> '-'.

    The underscore was missing until 2026-07-27: a cwd containing one resolved
    to a directory that does not exist, so the session silently had no
    transcript at all. Verified against 286 live project dirs, none of which
    contains an underscore. Byte-identical to
    packages/canopy_transcript/canopy_transcript/paths.py::encode_project_dir —
    keep the two in step (this file ships to the box as a single file, which is
    why the duplication exists).
    """
    return str(cwd).replace("/", "-").replace(".", "-").replace("_", "-")


def _resume_target_exists(cwd: pathlib.Path, session_id: str) -> bool:
    """Whether claude actually has a transcript to `--resume` for (cwd, session_id)
    — the cheap, local equivalent of runner/canopy_runner/execute.py's
    verify-before-reuse (it reads emdash's DB to confirm a task exists before
    driving it; this reads the filesystem to confirm a transcript exists before
    resuming it). Never guesses: a missing file, or any OSError while checking,
    is treated as "not resumable" so the caller falls back to a fresh spawn
    instead of a doomed `--resume` invocation."""
    if not session_id:
        return False
    try:
        return (CLAUDE_PROJECTS_HOME / _encode_project_dir(cwd) / f"{session_id}.jsonl").is_file()
    except OSError:
        return False


def _session_resume_plan(runner_id: str, turn: dict) -> str:
    """Ask the server whether a PRIOR turn on this same canopy Session left a CLI
    session id this runner can `--resume`. Returns that id, or "" for a fresh
    spawn (brand new thread, a different runner/host owns the hint, or the turn
    is not a session turn at all).

    Reuses the existing resolve-session RPC (apps/harness/api.py) rather than a
    new endpoint — `RunnerBinding.session_key` is documented as "engine-agnostic
    ... was emdash_task", so a raw claude CLI session id is exactly the kind of
    handle it was generalized to carry. The originating plan for this work named
    a `canopy_sessions.Session.cli_session_id` column for this purpose — no such
    field or docstring exists anywhere in the codebase (confirmed by grep before
    writing this), so this deliberately reuses the field that DOES exist and is
    fully wired end to end, rather than adding a new column/service change.

    An agentless AND projectless session (legal per the Session model, but not
    something resolve-session's tenant gate accepts today) degrades silently to
    fresh-per-turn here rather than failing the turn.
    """
    thread_key = _session_thread_key(turn)
    if not thread_key:
        return ""
    agent_slug = turn.get("agent_slug") or ""
    project = turn.get("project") or ""
    if not agent_slug and not project:
        return ""
    body: dict = {"thread_key": thread_key}
    if project:
        body["project"] = project
        body["workspace"] = turn.get("workspace_slug") or ""
    else:
        body["agent_slug"] = agent_slug
    status, plan = _api("POST", f"/runners/{runner_id}/resolve-session", body)
    if status != 200 or not plan:
        return ""
    if plan.get("reuse") and plan.get("emdash_task_id"):
        return plan["emdash_task_id"]
    return ""


def _record_session_resume(runner_id: str, turn: dict, cli_session_id: str) -> None:
    """Persist this turn's real CLI session id as the thread's engine handle, so
    a LATER turn on the same canopy Session can `--resume` it (see
    `_session_resume_plan`). Best-effort: a failure here only degrades the NEXT
    turn to a fresh spawn, never this one — logged, never raised.

    Sends BOTH `emdash_task_id` (what's actually read back today, via
    RunnerBinding.session_key) and `session_id` (the wire-compat field already
    threaded through RecordSessionIn -> services.record_session, currently
    accepted and silently discarded there) — if a `Session`-level column for
    this is ever added and wired server-side, this call starts populating it
    with zero runner-side changes; see `_session_resume_plan`'s docstring for
    why no such column exists today despite the originating plan naming one.
    """
    thread_key = _session_thread_key(turn)
    if not thread_key or not cli_session_id:
        return
    agent_slug = turn.get("agent_slug") or ""
    project = turn.get("project") or ""
    if not agent_slug and not project:
        return
    body: dict = {
        "thread_key": thread_key,
        "emdash_task_id": cli_session_id,
        "session_id": cli_session_id,
    }
    if project:
        body["project"] = project
        body["workspace"] = turn.get("workspace_slug") or ""
    else:
        body["agent_slug"] = agent_slug
    try:
        status, _ = _api("POST", f"/runners/{runner_id}/record-session", body)
        if status != 200:
            _log(f"record-session turn={str(turn.get('id', ''))[:8]} -> {status}")
    except Exception as exc:  # noqa: BLE001 — never let this fail the turn
        _log(f"record-session turn={str(turn.get('id', ''))[:8]} raised: {exc}")


# ── REST fallback loop (poll) ───────────────────────────────────────────────
# ── Concurrent turn execution (2026-07-27 convergence) ──────────────────────
#
# The runner used to execute turns STRICTLY SERIALLY: the claim loop called
# run_claude() and blocked inside it until `claude -p` exited, so a box serving
# five agents drained their turns one at a time. The laptop runner has never
# worked that way — it registers a chat turn and pumps it across ticks while it
# keeps heartbeating and claiming.
#
# It blocked because it derived the session's RECORD from stdout, so it had to
# sit in the read loop. Now that the transcript carries the durable record
# (`_ship_transcript_rows`), the read loop is no longer load-bearing for
# anything but this turn's own completion, and the whole turn can move off the
# claim path onto its own thread.
#
# Thread-per-turn rather than the laptop's single-threaded pump, deliberately:
# run_claude() already runs its own flusher and lease threads and has careful
# lock ordering, so wrapping it is a contained change, where making it
# non-blocking would be a rewrite of the most delicate function in this file.
# The cloud box has no CDP/emdash work to interleave with, which is the only
# reason the laptop needs a pump.
#
# WS SAFETY: workers always emit and finish over REST (`_api`), never over the
# shared WebSocket — `_ws_request` is not safe to call from several threads on
# one socket, the same constraint `_start_lease_renewal` already documents.
MAX_CONCURRENT_TURNS = int(os.environ.get("MAX_CONCURRENT_TURNS", "4") or "4")

_TURNS_LOCK = threading.Lock()
_IN_FLIGHT: dict[str, object] = {}


def _in_flight_ids() -> list[str]:
    """Turn ids currently executing — reported on every heartbeat so the server
    renews all of their leases, not just the newest."""
    with _TURNS_LOCK:
        return sorted(_IN_FLIGHT)


def _transcript_core():
    """canopy_transcript, imported lazily and cached.

    Lazy because this module ALSO clones canopy-web (clone_or_pull_canopy_web),
    so at import time the package may not exist on the box yet. Returns None if
    it cannot be imported — the turn still runs and still finishes; only the
    durable transcript rows are skipped, which the laptop path would backfill
    anyway.
    """
    global _TRANSCRIPT_CORE
    if _TRANSCRIPT_CORE is not False:
        return _TRANSCRIPT_CORE
    try:
        import canopy_transcript  # noqa: PLC0415
        _TRANSCRIPT_CORE = canopy_transcript
    except Exception as exc:  # noqa: BLE001
        _log(f"canopy_transcript unavailable ({exc}); durable transcript rows disabled")
        _TRANSCRIPT_CORE = None
    return _TRANSCRIPT_CORE


_TRANSCRIPT_CORE: object = False  # False = not yet attempted


def _ship_transcript_rows(runner_id: str, turn: dict, cwd, cli_session_id: str) -> None:
    """Ship this session's transcript rows so a cloud session's durable record is
    the SAME shape as a laptop session's — ordinal-keyed, full fidelity.

    Only meaningful for session-targeted turns: an agent or project turn has no
    canopy Session to stream into. Best-effort throughout; the record is
    re-derivable from the transcript on disk, so a failure here costs freshness,
    never history.
    """
    session_id = _chat_session_id(turn)
    if not session_id:
        return
    ct = _transcript_core()
    if ct is None:
        return
    path = ct.resolve_cli_transcript(cwd, cli_session_id, claude_home=CLAUDE_PROJECTS_HOME)
    if path is None:
        _log(f"turn {turn['id'][:8]}: no transcript at {cwd} for {cli_session_id[:8]}")
        return
    rows = ct.conversational_messages(ct.read_records(path), -1)
    if not rows:
        return
    events = [
        {"kind": r["role"], "seq": r["index"], "index": r["index"],
         "payload": ct.row_payload(r)}
        for r in rows
    ]
    status, _ = _api("POST", f"/runners/{runner_id}/session-stream",
                     {"session_id": session_id, "events": events})
    _log(f"turn {turn['id'][:8]}: shipped {len(events)} transcript rows -> {status}")


# ── live streaming: /streams + /backfills ────────────────────────────────────
# The observable half of attach/detach. `_ship_transcript_rows` above makes a
# session's record DURABLE at turn end, which is enough for a conversation
# nobody is watching. It is not enough for someone watching one: they would see
# the reduced TurnEvent stream during the turn and the real rows only after it
# finished. These two syncs close that, and bring this runner to parity with
# runner/canopy_runner (whose _sync_session_streams / _drain_backfills this
# mirrors deliberately — same server contract, same ordinal semantics).
#
# Resolution differs from the laptop's and that is the whole trick: a laptop
# session is found by (project, emdash task), a cloud session by (cwd, CLI
# session id). Both are known here without asking anyone — the cwd is
# deterministic from the canopy session id (see _turn_cwd), and the CLI session
# id is what `record_session` stored in RunnerBinding.session_key, which is
# exactly what the stream descriptor hands back.
_STREAM_READERS: dict[str, dict] = {}


def _session_transcript_path(session_id: str, session_key: str):
    """The CLI transcript backing a canopy session, or None if not resolvable yet."""
    ct = _transcript_core()
    if ct is None or not (session_id and session_key):
        return None
    cwd = pathlib.Path(WORK_DIR) / "sessions" / _safe_session_dirname(session_id)
    return ct.resolve_cli_transcript(cwd, session_key, claude_home=CLAUDE_PROJECTS_HOME)


def _post_stream_rows(runner_id: str, session_id: str, rows: list,
                      transcript_id: str = "") -> bool:
    """Ship conversational rows as live events. `seq == index` (the composite
    transcript ordinal): monotonic per session forever, so WS-derived `seq:<n>`
    message ids cannot collide across detaches, restarts or failovers."""
    ct = _transcript_core()
    if ct is None:
        return False
    events = [
        {"kind": r["role"], "seq": r["index"], "index": r["index"],
         "payload": ct.row_payload(r)}
        for r in rows
    ]
    # Chunked: a first-sight ship now carries a session's whole history, which on
    # the longest transcripts blows past the server's 2.5 MB request ceiling and
    # dies as an unhandled 500 (RequestDataTooBig, raised before the view runs).
    for batch in ct.chunk_rows(events) or [[]]:
        status, _ = _api("POST", f"/runners/{runner_id}/session-stream",
                         {"session_id": session_id, "events": batch,
                          # Which conversation these ordinals belong to (issue #615).
                          "transcript_id": transcript_id})
        if status != 200:
            return False
    return True


def _sync_session_streams(runner_id: str) -> None:
    """Tail every session this runner backs and ship its rows.

    Every session, not only watched ones — /streams now lists them all and the
    server persists unconditionally, fanning out live only where a viewer is
    attached. Mirrors canopy_runner.streams.sync_session_streams; the two must
    stay in step or cloud chats silently keep the old 16%-complete behaviour.

    The resume point is SERVER-side (`first_index`/`last_index`, the bounds of
    what it holds), refreshed every tick as our own posts land. There is
    deliberately no local offset checkpoint: a failed post drops the tailer, so
    the next tick re-attaches from the server markers and a restart recovers
    identically. Best-effort throughout — a hiccup here costs live latency, never
    history, because the turn-end ship and the backfill both still run.
    """
    ct = _transcript_core()
    if ct is None:
        return
    status, payload = _api("GET", f"/runners/{runner_id}/streams")
    if status != 200 or not isinstance(payload, dict):
        return
    desired = {s["session_id"]: s for s in (payload.get("streams") or []) if s.get("session_id")}

    for sid in list(_STREAM_READERS):  # drop tailers for sessions nobody watches
        if sid not in desired:
            _STREAM_READERS.pop(sid, None)

    for sid, descriptor in desired.items():
        st = _STREAM_READERS.setdefault(sid, {"reader": None, "count": 0})
        st["session_key"] = descriptor.get("session_key") or ""
        st["last_index"] = descriptor.get("last_index")
        st["first_index"] = descriptor.get("first_index")
        st["server_transcript_id"] = descriptor.get("transcript_id") or ""
        try:
            path = _session_transcript_path(sid, st["session_key"])
            if path is None:
                continue  # not spawned yet, or a different box owns it
            transcript_id = path.stem  # the CLI session uuid — the conversation's identity
            if st["reader"] is not None and st.get("transcript_id") != transcript_id:
                st["reader"], st["count"] = None, 0  # session replaced under us
            st["transcript_id"] = transcript_id
            if st["reader"] is None:
                reader = ct.TailReader(str(path))
                records = reader.read_new()
                # The server's markers are ordinals into the transcript it named;
                # against any other file they license nothing (issue #615).
                same = bool(st["server_transcript_id"]) and (
                    st["server_transcript_id"] == transcript_id
                )
                rows = ct.rows_to_ship(
                    ct.conversational_messages(records, -1),
                    first_held=st.get("first_index") if same else None,
                    last_held=st.get("last_index") if same else None,
                )
                if rows and not _post_stream_rows(runner_id, sid, rows, transcript_id):
                    continue  # nothing consumed — re-attach next tick
                st["reader"], st["count"] = reader, len(records)
                _log(f"stream {sid[:8]}: attached ({len(rows)} rows caught up)")
                continue
            new_records = st["reader"].read_new()
            if not new_records:
                continue
            base = st["count"]
            # The offset applies to the RECORD ordinal inside compose_index, never
            # to the composite index — adding it there would shift a row into
            # another record's slots.
            rows = ct.conversational_messages(new_records, -1, record_offset=base)
            if rows and not _post_stream_rows(runner_id, sid, rows, st["transcript_id"]):
                st["reader"], st["count"] = None, 0  # don't advance past unshipped records
                continue
            st["count"] = base + len(new_records)
        except Exception as exc:  # noqa: BLE001 — one bad session must not stop the rest
            _log(f"stream {sid[:8]}: {exc}; will re-attach")
            st["reader"], st["count"] = None, 0


def _drain_backfills(runner_id: str) -> None:
    """Ship a session's FULL history when the server asks for it, with ordinals,
    so it upsert-fills the older rows around anything the live stream already
    persisted."""
    ct = _transcript_core()
    if ct is None:
        return
    status, payload = _api("GET", f"/runners/{runner_id}/backfills")
    if status != 200 or not isinstance(payload, dict):
        return
    for b in payload.get("backfills") or []:
        sid = b.get("session_id") or ""
        path = _session_transcript_path(sid, b.get("session_key") or "")
        if not (sid and path):
            continue  # unresolvable -> leave the request standing, server keeps the tail
        try:
            messages = ct.conversational_messages(ct.read_records(path), -1)
            # Chunked, and only the LAST chunk is `final` — an earlier one would
            # retire the request while the rest was still in flight, stranding a
            # permanently partial history behind a cleared flag.
            batches = ct.chunk_rows(messages) or [[]]
            st = None
            for i, batch in enumerate(batches):
                st, _ = _api("POST", f"/runners/{runner_id}/session-backfill",
                             {"session_id": sid, "messages": batch,
                              "final": i == len(batches) - 1,
                              # Names the conversation, so a rebuild REPLACES a
                              # predecessor's rows rather than merging (issue #615).
                              "transcript_id": path.stem})
            _log(f"backfill {sid[:8]}: shipped {len(messages)} rows "
                 f"in {len(batches)} chunk(s) -> {st}")
        except Exception as exc:  # noqa: BLE001
            # A backfill that keeps failing never rebuilds history and never stops
            # trying — exactly the case that must not be silent.
            _log(f"backfill {sid[:8]} FAILED: {exc}")


def _sync_session_views(runner_id: str, *, with_backfills: bool = True) -> None:
    """Both session-view syncs, in the order that minimises duplicate work: a
    backfill rewrites the whole history, so run it before the incremental tail
    advances its marker.

    Streams run on their own fast clock (STREAM_POLL_SECONDS) because they ARE
    the live view — at the turn-claim cadence a viewer would watch the reply
    arrive in POLL_SECONDS-sized lumps. Backfills are request-driven and rare,
    so they ride the slower clock and one cheap GET is all an idle tick costs.
    """
    try:
        if with_backfills:
            _drain_backfills(runner_id)
        _sync_session_streams(runner_id)
    except Exception as exc:  # noqa: BLE001 — never take the main loop down
        _log(f"session view sync error: {exc}")


def _run_turn(runner_id: str, turn: dict) -> None:
    """Execute one claimed turn to completion. Runs on its own thread."""
    turn_id = turn["id"]
    try:
        cwd = _turn_cwd(turn, turn_id)
        resume_id = turn.get("_resume_id") or None

        def emit(events, _tid=turn_id):
            _api("POST", f"/turns/{_tid}/events", {"events": events})

        lease_stop = _start_lease_renewal(runner_id, turn_id)
        try:
            try:
                ok, text, cli_session_id = execute_prompt(
                    turn.get("prompt", ""), turn_id, emit, cwd=cwd,
                    agent_slug=_turn_agent_slug(turn), resume_session_id=resume_id,
                )
            except Exception as exc:  # never let one turn kill the runner
                ok, text, cli_session_id = False, f"runner error: {exc}", ""
        finally:
            lease_stop.set()
        if cli_session_id:
            # Never let bookkeeping cost us the finish below — an exception here
            # used to strand the turn exactly like a dead socket did (#448).
            for step, fn in (("record session resume", _record_session_resume),
                             ("ship transcript rows", None)):
                try:
                    if fn is None:
                        _ship_transcript_rows(runner_id, turn, cwd, cli_session_id)
                    else:
                        fn(runner_id, turn, cli_session_id)
                except Exception as exc:  # noqa: BLE001
                    _log(f"warn: could not {step} for {turn_id[:8]}: {exc}")
        # ALWAYS over REST. #448 fixed turns being orphaned when the WebSocket
        # died mid-turn and the finish frame raised; the worker never touches the
        # socket at all, so that failure class is now structurally impossible
        # here rather than merely handled.
        finish = "done" if ok else "failed"
        _api("POST", f"/turns/{turn_id}/finish",
             {"status": finish, "result_note": text[:2000]})
        _log(f"finished turn {turn_id[:8]}: {finish}")
    except Exception as exc:  # noqa: BLE001 — a worker must never take the loop down
        _log(f"turn {turn_id[:8]} worker crashed: {exc}")
    finally:
        with _TURNS_LOCK:
            _IN_FLIGHT.pop(turn_id, None)


def _start_turn(runner_id: str, turn: dict) -> bool:
    """Start a claimed turn on its own thread. False if at the concurrency cap."""
    turn_id = turn["id"]
    with _TURNS_LOCK:
        if len(_IN_FLIGHT) >= MAX_CONCURRENT_TURNS:
            return False
        _IN_FLIGHT[turn_id] = None
    thread = threading.Thread(
        target=_run_turn, args=(runner_id, turn), daemon=True,
        name=f"turn-{turn_id[:8]}",
    )
    with _TURNS_LOCK:
        _IN_FLIGHT[turn_id] = thread
    thread.start()
    return True


def run_over_rest(runner_id: str) -> None:
    _log(f"polling {BASE_URL} every {POLL_SECONDS}s (REST fallback), "
         f"up to {MAX_CONCURRENT_TURNS} concurrent turns")
    while not _stop:
        # Every in-flight turn rides the heartbeat so the server renews all of
        # their leases — with concurrency, reporting only the newest would let
        # the others expire mid-run.
        _api("POST", f"/runners/{runner_id}/heartbeat", _heartbeat_body(_in_flight_ids()))
        # Attached viewers are served on this path too — a runner that fell back
        # to REST must not also silently stop being watchable.
        _sync_session_views(runner_id)
        # Likewise a sign-in: a box on the REST fallback is EXACTLY the box most
        # likely to need one, and leaving this on the WS path only would strand
        # the operator on the runners that fail most.
        _drain_mint(runner_id)
        if len(_in_flight_ids()) >= MAX_CONCURRENT_TURNS:
            time.sleep(POLL_SECONDS)
            continue
        status, turn = _api("POST", f"/runners/{runner_id}/claim")
        if status != 200 or not turn:
            time.sleep(POLL_SECONDS)
            continue
        turn_id = turn["id"]
        _log(f"claimed turn {turn_id[:8]} target={turn.get('target')} (REST)")
        resume_id = _session_resume_plan(runner_id, turn)
        turn["_resume_id"] = resume_id or ""
        _api("POST", f"/turns/{turn_id}/start",
             {"session_id": resume_id or f"cloud-{turn_id[:8]}"})
        if not _start_turn(runner_id, turn):
            # At the cap between the check and the start — rare, and the server
            # re-offers the turn on the next claim, so nothing is lost.
            _log(f"turn {turn_id[:8]}: at concurrency cap, will re-claim")
        # No sleep on a successful claim: drain the queue as fast as the cap allows.


# ── WebSocket control channel (RC2) ─────────────────────────────────────────
def _ws_url(runner_id: str) -> str:
    base = BASE_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{base.replace('/api', '')}/ws/runner/{runner_id}/"


def _ws_request(ws, frame: dict, want_type: str, timeout: float = 120.0):
    """Send an action frame and read until the matching ack/result, skipping
    unrelated frames (a wake/interject that arrives mid-request is not what we're
    waiting on right now). Returns the matched frame, or None on close/timeout."""
    import websocket  # local: only the WS path needs the dep

    # ws.settimeout() also governs sends, not just recv. A large event frame (e.g.
    # drill doctor output) can take longer to write than the short WS_POLL_TIMEOUT
    # allows; if the socket times out mid-write, OpenSSL has already handed part of
    # the frame to the kernel and a subsequent SSL_write MUST resend the exact same
    # buffer or it raises ssl.SSLError BAD_LENGTH — killing the process. Give sends
    # a generous timeout, then restore the short poll timeout for the recv loop.
    ws.settimeout(60)
    try:
        ws.send(json.dumps(frame))
    finally:
        ws.settimeout(WS_POLL_TIMEOUT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue  # socket idle; keep waiting for our ack
        if not raw:
            return None
        msg = json.loads(raw)
        if msg.get("type") == want_type:
            return msg
    return None


def _finish_turn(ws, turn_id: str, ok: bool, text: str) -> None:
    """Finish the turn, falling back to REST when the WebSocket is gone.

    The finish frame used to be a bare `_ws_request`. If the socket had died
    mid-turn that raised, the exception unwound out of `_claim_and_run_once`,
    `_drain` logged it as "drain error", and the turn was NEVER FINISHED — it sat
    EXECUTING until the 900s lease sweep marked it LOST. That is the whole
    "runner lost the turn (lease expired mid-drill)" story: on a 5-agent drill
    wave only the ONE turn short enough to dodge a socket death ever logged
    "finished turn"; the other four were orphaned and reported nothing for 15
    minutes.

    Why the socket dies: the runner only pongs from inside `ws.recv()`, and
    during a turn `recv()` is reached only via each `emit()`'s ack — so a turn
    with a quiet stretch longer than uvicorn's `--ws-ping-timeout` (20s, see the
    Dockerfile) stops ponging and the server closes the connection. Fixing that
    is a separate design question; completion must not depend on the socket
    either way.

    REST finish is idempotent server-side (`api.py::finish_turn` returns early
    on an already-terminal turn), so this fallback is safe even if the WS finish
    partially landed.
    """
    body = {"status": "done" if ok else "failed", "result_note": text[:2000]}
    reason = ""
    try:
        acked = _ws_request(
            ws, {"action": "finish", "turn_id": turn_id, **body}, "finish.ack"
        )
        if acked is not None:
            _log(f"finished turn {turn_id[:8]} (WS): {body['status']}")
            return
        reason = "no finish.ack (socket closed)"
    except Exception as exc:
        reason = f"{exc}"
    status, _ = _api("POST", f"/turns/{turn_id}/finish", body)
    if status == 200:
        _log(f"finished turn {turn_id[:8]} (REST, after ws finish failed: {reason}): {body['status']}")
    else:
        _log(f"COULD NOT FINISH turn {turn_id[:8]} (ws: {reason}; REST status={status}) "
             f"— it will sit EXECUTING until the lease sweep")


def _claim_and_run_once(ws, runner_id: str) -> bool:
    """Claim at most one turn and DISPATCH it. Returns True if a turn was started.

    Dispatches rather than runs: the worker thread executes the turn while this
    loop goes back to claiming, so a burst of queued turns (a drill wave enqueues
    one per agent at once) runs concurrently instead of single-file.

    The worker emits and finishes over REST, never over `ws` — a shared socket
    is not safe across threads, the same constraint `_start_lease_renewal`
    observes.
    """
    if len(_in_flight_ids()) >= MAX_CONCURRENT_TURNS:
        return False
    res = _ws_request(ws, {"action": "claim"}, "claim.result")
    turn = res.get("turn") if res else None
    if not turn:
        return False
    tid = turn["id"]
    _log(f"claimed turn {tid[:8]} target={turn.get('target')} (WS)")
    resume_id = _session_resume_plan(runner_id, turn)
    turn["_resume_id"] = resume_id or ""
    _ws_request(ws, {"action": "start", "turn_id": tid,
                     "session_id": resume_id or f"cloud-{tid[:8]}"}, "start.ack")
    return _start_turn(runner_id, turn)


def _drain(ws, runner_id: str, max_turns: int = 20) -> int:
    """Claim and run queued turns until there are none left. Returns how many ran.

    `_claim_and_run_once` takes at most ONE turn per call, which was the whole problem:
    a drill wave enqueues one turn per agent at once, so five queued turns needed
    five separate triggers. With claims driven only by connects and wakes, the
    tail of a burst simply aged out — the last agent in every wave died as LOST
    with "lease expired mid-drill".

    `max_turns` is a runaway guard, not a throttle: it caps one drain pass so a
    server that always returns a turn cannot spin here forever without ever
    heartbeating. Whatever is left is picked up by the next poll tick.

    Since turns now DISPATCH rather than run inline, this counts turns started,
    and `MAX_CONCURRENT_TURNS` is the real throttle — the loop stops claiming
    once the cap is reached and resumes as workers finish.
    """
    ran = 0
    while ran < max_turns and not _stop:
        before = ran
        try:
            if _claim_and_run_once(ws, runner_id):
                ran += 1
        except Exception as exc:
            _log(f"drain error: {exc}")
            break
        if ran == before:
            break  # nothing claimed — queue is empty for us
    return ran


def run_over_ws(runner_id: str) -> bool:
    """Persistent control channel: heartbeat, claim-on-wake, run + stream over the
    socket. Returns False if the WS lib/endpoint is unavailable (caller falls back
    to REST); loops until _stop otherwise, reconnecting on drops."""
    try:
        import websocket
    except ImportError:
        _log("websocket-client not installed; using REST")
        return False
    url = _ws_url(runner_id)
    connected_ever = False
    while not _stop:
        try:
            ws = websocket.create_connection(
                url, header=[f"Authorization: Bearer {TOKEN}"], timeout=HEARTBEAT_SECONDS,
            )
        except Exception as exc:
            if not connected_ever:
                _log(f"ws connect failed ({exc}); falling back to REST")
                return False
            _log(f"ws reconnect failed ({exc}); retry in {POLL_SECONDS}s")
            time.sleep(POLL_SECONDS)
            continue
        connected_ever = True
        _log(f"ws connected: {url}")
        # Poll on a short timeout so recv() hands control back regularly; the
        # heartbeat is driven on a wall clock below, NOT gated on recv timing out
        # (see WS_POLL_TIMEOUT — server pings would otherwise starve the heartbeat).
        ws.settimeout(WS_POLL_TIMEOUT)

        def _beat():
            # Report in-flight turns, not []: each worker also has its own lease
            # renewer, but a heartbeat that claims nothing is running while four
            # turns are executing is a lie the server acts on.
            # Provenance rides THIS frame too, not just the REST ones: the WS beat
            # is the cloud runner's primary heartbeat, and the server assigns those
            # fields unconditionally — so a frame that omitted them would erase
            # what the REST paths reported, every 20 seconds.
            _ws_request(ws, {"action": "heartbeat", **_heartbeat_body(_in_flight_ids())},
                        "heartbeat.ack", timeout=15)

        try:
            _beat()  # register ONLINE immediately (claim_next_turn gates on a fresh heartbeat)
            last_beat = time.monotonic()
            _drain(ws, runner_id)  # anything already queued gets no wake
            last_poll = time.monotonic()
            last_stream = 0.0
            while not _stop:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raw = None
                if raw:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "wake":
                        _drain(ws, runner_id)
                    elif mtype == "interject":
                        # canopy-web has ALREADY told a person their message was
                        # sent, so whether it actually landed is the thing worth
                        # logging. Before ACP this frame was logged and dropped:
                        # `claude -p` is one process with one prompt and stdin
                        # closed, so there was nowhere to put it.
                        t_id = str(msg.get("turn_id") or "")
                        body = str(msg.get("message") or "")
                        if body and steer_turn(t_id, body):
                            _log(f"interject turn={t_id[:8]}: delivered mid-turn")
                        else:
                            _log(f"interject turn={t_id[:8]}: NOT delivered "
                                 f"(no live ACP turn) — {body[:60]!r}")
                    elif mtype == "stream":
                        # Latency optimization only — /streams is polled below
                        # regardless, so a dropped frame costs one tick, never
                        # a permanently unattached viewer.
                        _sync_session_views(runner_id)
                    elif mtype == "update_available":
                        _nudge_updater(str(msg.get("expected_sha") or ""))
                elif raw == "":
                    break  # server closed the socket
                if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                    _beat()
                    last_beat = time.monotonic()
                # Safety net: claim on a wall clock too, never on wakes alone.
                # Wake delivery is the ONLY other trigger, so a wake that is never
                # sent (or is sent while this loop is blocked for minutes inside a
                # turn) used to strand a queued turn indefinitely — on a healthy
                # socket the runner sat idle while turns aged out to LOST. It was
                # masked for months because the WS kept erroring and every
                # reconnect drained one turn; a stable connection exposed it.
                # The REST path (run_over_rest) has always polled — this brings
                # the WS path to parity.
                if time.monotonic() - last_poll >= POLL_SECONDS:
                    _drain(ws, runner_id)
                    _sync_session_views(runner_id)          # incl. backfills
                    _drain_mint(runner_id)
                    last_poll = time.monotonic()
                    last_stream = time.monotonic()
                elif time.monotonic() - last_stream >= STREAM_POLL_SECONDS:
                    _sync_session_views(runner_id, with_backfills=False)
                    last_stream = time.monotonic()
        except Exception as exc:
            _log(f"ws loop error: {exc}")
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if not _stop:
            time.sleep(2)  # brief backoff before reconnect
    return True


def main() -> None:
    if not BASE_URL or not TOKEN:
        _log("FATAL: CANOPY_BASE_URL and CANOPY_TOKEN are required")
        sys.exit(1)
    runner_id = pair_or_load()
    if not fetch_and_stage_credential(runner_id):
        return  # stopped before a credential was provisioned
    bootstrap_agent_fleet()
    # Prefer the WS control channel; fall back to REST polling if it can't be used.
    if not run_over_ws(runner_id):
        run_over_rest(runner_id)


def _handle_stop(*_a):
    global _stop
    _stop = True
    _log("stopping after current turn")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    main()
