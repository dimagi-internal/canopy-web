#!/usr/bin/env python3
"""Canopy cloud runner — a headless `kind=cloud` executor for EC2.

Self-contained (stdlib only) on purpose: the emdash-coupled packages/canopy_runner
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
POST /turns/{id}/transcript (batched by bytes; see deploy/ec2-runner/README.md), in
addition to the reduced TurnEvents. And a session turn's CLI session id is captured
from the stream and round-tripped through the existing resolve-session/record-session
RPCs so a later turn on the same canopy Session can `--resume` it instead of
cold-starting — see _session_resume_plan / _record_session_resume below for exactly
which field carries that id and why.

Config comes from the environment (see deploy/ec2-runner/README.md):
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
`claude` authenticates from CLAUDE_CODE_OAUTH_TOKEN (a dedicated setup-token from
Secrets Manager, staged into the service env by cloud-init). AGENT_SLUGS /
AGENT_REPO_ORG / GITHUB_TOKEN / OP_SERVICE_ACCOUNT_TOKEN are consumed by
bootstrap_agents.sh (see deploy/ec2-runner/README.md), not this file directly.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
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
# Default OFF (opt-in, RUNNER_SESSIONS=1): this runner has no durable-record path
# for a chat session yet. On labs (CHAT_STUB_EXECUTOR=False) every session is
# stamped transcript_sourced at creation (apps/canopy_sessions/services.py
# create_session), which means the reduced TurnEvents this runner posts to
# /turns/{id}/events NEVER become durable Message rows (project_events short-
# circuits to 0 for such a session) and the user's own line is never durably
# written either (_send_transcript_sourced_message deliberately authors none).
# The only durable path is POST /runners/{id}/session-stream with per-line
# transcript ordinals -> services.persist_transcript_rows, which today has
# exactly one caller: packages/canopy_runner/canopy_runner/client.py (the
# laptop runner). Until THIS runner implements that same call (plus honoring
# /runners/{id}/streams + /backfills), declaring sessions:true would make it
# eligible to claim a real chat turn, stream a perfect-looking live reply, and
# then silently lose the entire conversation the moment the user reloads the
# page — worse than not claiming it at all. Flip this default only once
# persist_transcript_rows (via session-stream/streams/backfills) is wired here.
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
# Agent-fleet bootstrap (deploy/ec2-runner/bootstrap_agents.sh) — see
# bootstrap_agent_fleet() below for why this runs from here and not cloud-init.
AGENT_ROOT = os.environ.get("AGENT_ROOT", "/opt/agents")
CANOPY_WEB_REPO_DIR = os.environ.get("CANOPY_WEB_REPO_DIR", "/opt/canopy-web")
CANOPY_WEB_REPO_URL = os.environ.get("CANOPY_WEB_REPO_URL", "https://github.com/jjackson/canopy-web.git")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
# App-level heartbeat cadence (keeps the lease + status fresh).
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "20"))
# Short recv poll so the WS loop regains control regularly and drives the heartbeat
# on a wall clock. MUST stay below uvicorn's --ws-ping-interval (5s): the server
# pings every 5s and websocket-client auto-pongs and keeps recv() looping, so a
# heartbeat gated on recv() timing out would never fire and the runner would go
# stale while still connected.
WS_POLL_TIMEOUT = float(os.environ.get("WS_POLL_TIMEOUT", "3"))
# How often the lease-renewal thread heartbeats WHILE a turn is executing (both
# loops block inside run_claude() for the whole turn, so nothing else heartbeats
# during that window). Must stay comfortably under DEFAULT_LEASE_SECONDS (900s,
# apps/harness/services.py) or a long turn gets swept LOST mid-execution.
LEASE_HEARTBEAT_SECONDS = int(os.environ.get("LEASE_HEARTBEAT_SECONDS", "60"))
STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", str(pathlib.Path.home() / ".canopy-cloud-runner.json")))

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


def _api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    url = f"{BASE_URL}/api/harness{path}"
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
    `--resume` wiring is unit-testable without touching subprocess."""
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
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
            _api("POST", f"/runners/{runner_id}/heartbeat",
                 {"active_turn_ids": [turn_id], "host": RUNNER_HOST})

    threading.Thread(target=_loop, daemon=True, name=f"lease-{turn_id[:8]}").start()
    return stop


def pair_or_load() -> str:
    if STATE_FILE.exists():
        rid = json.loads(STATE_FILE.read_text()).get("runner_id")
        if rid:
            # Confirm it still exists (a heartbeat 404 means it was retired).
            status, _ = _api("POST", f"/runners/{rid}/heartbeat",
                              {"active_turn_ids": [], "host": RUNNER_HOST})
            if status == 200:
                _log(f"reusing runner {rid}")
                # Capabilities were historically fixed at pairing time; re-pairing
                # to pick up a changed env var (e.g. RUNNER_SESSIONS flipped on for
                # a box paired before this feature existed) would mint a NEW runner
                # id and orphan this one's RunnerBindings. PATCH in place instead
                # (apps/harness/api.py::update_runner_capabilities) so a redeploy
                # with a new env always reflects the CURRENT declared capabilities.
                _api("PATCH", f"/runners/{rid}", {"capabilities": RUNNER_CAPS})
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
    env = os.environ.copy()
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
    contract (deploy/ec2-runner/README.md), a transcript failure must never
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
    packages/canopy_runner/execute.py already uses for emdash sessions: never
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
    # flush is ever "in flight" — this is where the backpressure N2 flags
    # actually still exists (flush vs. flush), which is fine: it only ever
    # blocks ANOTHER FLUSH, never the read loop's line-by-line appending.
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
                        batch.append({"kind": "tool_start", "payload": {"name": block.get("name", "")}})
            elif etype == "user":
                for block in (evt.get("message", {}).get("content") or []):
                    if block.get("type") == "tool_result":
                        batch.append({"kind": "tool_end", "payload": {}})
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
        # KILL (AND CLOSE STDOUT) BEFORE REAPING — unconditionally, regardless
        # of whether the loop reached EOF (review N1). On a mid-loop exception
        # `claude` can still be RUNNING with its stdout pipe no longer drained;
        # once the 64KB pipe buffer fills, the child blocks on write and a
        # bare `proc.wait()` NEVER RETURNS. That doesn't just fail this turn —
        # it means run_claude() itself never returns, so the caller's own
        # `finally: lease_stop.set()` never runs, the lease-renewal thread
        # keeps heartbeating this turn EXECUTING forever, and
        # one_executing_turn_per_agent wedges the whole agent permanently
        # (the exact "wedged-but-heartbeating runner" pathology CLAUDE.md
        # documents) — worse than the turn simply failing.
        #
        # Closing stdout first unblocks any pending write (the read end going
        # away delivers SIGPIPE/EPIPE to the writer); kill() on a process that
        # already exited cleanly (the ordinary happy path, where the loop
        # already reached EOF) is a harmless no-op signal to a reapable
        # zombie — Python's Popen doesn't guard against re-signaling because
        # nothing here has called wait()/poll() yet to know it's dead.
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — best-effort; still try to kill below
            pass
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 — best-effort; still try to wait below
            pass
        # The wait itself is ALSO bounded — never unconditional — because a
        # kill() can still leave a zombie the OS is slow to reap, and this
        # runner must never block on that either.
        try:
            proc.wait(timeout=PROC_REAP_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — subprocess.TimeoutExpired or worse
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
    """Wire a git credential helper so `git clone` of private agent repos works
    (used by the reconciler in the next milestone; harmless for a trivial turn)."""
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
    unsanitized, mirroring packages/canopy_runner/canopy_runner/execute.py's
    `_safe_name` for the same reason."""
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in session_id).strip(".-")
    return cleaned[:80] or "unknown-session"


def _turn_cwd(turn: dict, turn_id: str) -> pathlib.Path:
    """Where claude should run for this turn (deploy/ec2-runner design spec §2:
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
    """canopy-web is PUBLIC (github.com/jjackson/canopy-web) — this needs no
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
        return True
    except Exception as exc:
        _log(f"warn: could not clone/pull canopy-web ({CANOPY_WEB_REPO_URL}) for bootstrap: {exc}")
        return False


def bootstrap_agent_fleet() -> None:
    """Clone/pull canopy-web to CANOPY_WEB_REPO_DIR and run its
    deploy/ec2-runner/bootstrap_agents.sh — the agent-fleet provisioning step
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
    script = pathlib.Path(CANOPY_WEB_REPO_DIR) / "deploy" / "ec2-runner" / "bootstrap_agents.sh"
    if not script.exists():
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


def fetch_and_stage_credential(runner_id: str) -> bool:
    """A CLOUD runner owns no secrets at boot beyond its PAT — it fetches its
    credential bundle from canopy-web (the per-runner hub) and stages it into the
    environment. Blocks (polling) until the Claude token is set, so the operator can
    provision the runner AFTER it has paired and appeared in the fleet. A laptop
    runner never does this — it uses emdash's ambient auth.
    """
    while not _stop:
        status, cred = _api("GET", f"/runners/{runner_id}/credential")
        if status == 200 and cred and cred.get("claude_token"):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = cred["claude_token"]
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
# Mirrors packages/canopy_runner/canopy_runner/transcript.py's
# `encode_project_dir` (duplicated, not imported: that package pulls in
# non-stdlib deps and this runner is deliberately stdlib-only).
CLAUDE_PROJECTS_HOME = pathlib.Path.home() / ".claude" / "projects"


def _encode_project_dir(cwd: pathlib.Path) -> str:
    return str(cwd).replace("/", "-").replace(".", "-")


def _resume_target_exists(cwd: pathlib.Path, session_id: str) -> bool:
    """Whether claude actually has a transcript to `--resume` for (cwd, session_id)
    — the cheap, local equivalent of packages/canopy_runner/execute.py's
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
def run_over_rest(runner_id: str) -> None:
    _log(f"polling {BASE_URL} every {POLL_SECONDS}s (REST fallback)")
    while not _stop:
        _api("POST", f"/runners/{runner_id}/heartbeat", {"active_turn_ids": [], "host": RUNNER_HOST})
        status, turn = _api("POST", f"/runners/{runner_id}/claim")
        if status != 200 or not turn:
            time.sleep(POLL_SECONDS)
            continue
        turn_id = turn["id"]
        _log(f"claimed turn {turn_id[:8]} target={turn.get('target')} (REST)")
        resume_id = _session_resume_plan(runner_id, turn)
        _api("POST", f"/turns/{turn_id}/start",
             {"session_id": resume_id or f"cloud-{turn_id[:8]}"})
        _api("POST", f"/runners/{runner_id}/heartbeat", {"active_turn_ids": [turn_id], "host": RUNNER_HOST})
        cwd = _turn_cwd(turn, turn_id)

        def emit(events, _tid=turn_id):
            _api("POST", f"/turns/{_tid}/events", {"events": events})

        lease_stop = _start_lease_renewal(runner_id, turn_id)
        try:
            try:
                ok, text, cli_session_id = run_claude(
                    turn.get("prompt", ""), turn_id, emit, cwd=cwd,
                    agent_slug=_turn_agent_slug(turn), resume_session_id=resume_id or None,
                )
            except Exception as exc:  # never let one turn kill the loop
                ok, text, cli_session_id = False, f"runner error: {exc}", ""
        finally:
            lease_stop.set()  # turn is over (success/failure/exception) — stop renewing
        if cli_session_id:
            _record_session_resume(runner_id, turn, cli_session_id)
        finish = "done" if ok else "failed"
        _api("POST", f"/turns/{turn_id}/finish", {"status": finish, "result_note": text[:2000]})
        _log(f"finished turn {turn_id[:8]}: {finish}")


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


def _claim_and_run_once(ws, runner_id: str) -> bool:
    """Claim at most one turn and run it. Returns True if a turn was run.

    The bool is what lets `_drain` loop until the queue is actually empty
    instead of assuming one claim per trigger.
    """
    res = _ws_request(ws, {"action": "claim"}, "claim.result")
    turn = res.get("turn") if res else None
    if not turn:
        return False
    tid = turn["id"]
    _log(f"claimed turn {tid[:8]} target={turn.get('target')} (WS)")
    resume_id = _session_resume_plan(runner_id, turn)
    _ws_request(ws, {"action": "start", "turn_id": tid,
                     "session_id": resume_id or f"cloud-{tid[:8]}"}, "start.ack")
    cwd = _turn_cwd(turn, tid)

    def emit(events, _tid=tid):
        _ws_request(ws, {"action": "event", "turn_id": _tid, "events": events}, "event.ack", timeout=60)

    # Lease renewal runs on its own thread over REST (never over `ws` — see
    # _start_lease_renewal) so the lease survives the whole time run_claude()
    # blocks this loop.
    lease_stop = _start_lease_renewal(runner_id, tid)
    try:
        try:
            ok, text, cli_session_id = run_claude(
                turn.get("prompt", ""), tid, emit, cwd=cwd,
                agent_slug=_turn_agent_slug(turn), resume_session_id=resume_id or None,
            )
        except Exception as exc:
            ok, text, cli_session_id = False, f"runner error: {exc}", ""
    finally:
        lease_stop.set()  # turn is over (success/failure/exception) — stop renewing
    if cli_session_id:
        _record_session_resume(runner_id, turn, cli_session_id)
    _ws_request(ws, {"action": "finish", "turn_id": tid,
                     "status": "done" if ok else "failed", "result_note": text[:2000]}, "finish.ack")
    _log(f"finished turn {tid[:8]} (WS): {'done' if ok else 'failed'}")
    return True


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
            _ws_request(ws, {"action": "heartbeat", "active_turn_ids": []}, "heartbeat.ack", timeout=15)

        try:
            _beat()  # register ONLINE immediately (claim_next_turn gates on a fresh heartbeat)
            last_beat = time.monotonic()
            _drain(ws, runner_id)  # anything already queued gets no wake
            last_poll = time.monotonic()
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
                        _log(f"interject turn={msg.get('turn_id')}: {msg.get('message')!r}")
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
                    last_poll = time.monotonic()
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
