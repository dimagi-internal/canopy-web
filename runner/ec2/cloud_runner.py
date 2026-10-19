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
  CANOPY_BASE_URL   e.g. https://canopy.dimagi.com
  CANOPY_TOKEN      OPTIONAL once the box's device key is approved: a person's PAT,
                    used only to pair a new box and register its device key. After
                    that the box signs in as itself (`_bearer`, keyless).
  RUNNER_NAME       display name (default: this hostname)
  RUNNER_PROJECTS   comma-separated repo names this runner may drive (e.g. canopy-web)
  RUNNER_AGENTS     comma-separated agent slugs this runner may drive (e.g. echo,ada)
  RUNNER_SESSIONS   whether this runner claims chat/session turns (default: OFF —
                     opt-in; see the RUNNER_SESSIONS declaration for why)
  RUNNER_DIRECTED   take only what is put on this box — turns pinned to it and
                     chats bound to it, no routed work (default: OFF; a test box)
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
                     an agent turn with a clone here runs in its OWN worktree of
                     it, WORK_DIR/agents/<slug>/<turn> (#1141)
  RUNNER_SRC_DIR    the runner's OWN canopy-web clone (default:
                     /opt/canopy-runner/src) — the in-repo packages it imports and
                     the bootstrap_agents.sh it runs come from here, never from a
                     clone agent turns can reach
  CANOPY_WEB_REPO_DIR/CANOPY_WEB_REPO_URL  the shared canopy-web clone the
                     auto-updater reads (`git show`), and the repo URL both use
  POLL_SECONDS      idle poll interval (default: 15)
  STATE_FILE        runner-id cache (default: ~/.canopy-cloud-runner.json)
  RUNNER_HOME       where this runner's bytes + its two auto-update files live
                     (default: /opt/canopy-runner) — `build-info.json` says what
                     code is installed, `in-flight` says whether now is a safe
                     moment to replace it. Both are written here and read by
                     runner/ec2/update_runner.sh; see spec 2026-07-30.
`claude` authenticates from CLAUDE_CODE_OAUTH_TOKEN (a dedicated setup-token from
Secrets Manager, staged into the service env by cloud-init). AGENT_SLUGS /
AGENT_REPO_ORG / GITHUB_TOKEN are consumed by
bootstrap_agents.sh (see runner/ec2/README.md), not this file directly.
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import hmac
import json
import os
import fcntl
import glob
import pathlib
import pty
import re
import secrets
import select
import signal
import struct
import socket
import shutil
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

#: A box's address when it differs from its stack parameter. Moving a box is
#: manual: write the new https address here and restart the service. Not
#: runner.env: the unit's ExecStartPre (canopy-fetch-env) re-renders that from the
#: stack parameter on EVERY start. Nothing re-renders this file. cloud-ec2-2's
#: stack still names labs.connect.dimagi.com/canopy, so this file is what keeps it
#: on canopy.dimagi.com — do not delete it there.
BASE_URL_OVERRIDE_FILE = os.environ.get("CANOPY_BASE_URL_OVERRIDE_FILE",
                                        "/opt/canopy-runner/base_url.override")


def _initial_base_url() -> str:
    """The env's address, unless this box has moved (BASE_URL_OVERRIDE_FILE). A
    moved box also exports the new address to its children (bootstrap_agents.sh,
    the canopy CLI), which read CANOPY_BASE_URL / CANOPY_WEB_API_URL themselves."""
    env = os.environ.get("CANOPY_BASE_URL", "").rstrip("/")
    try:
        moved = pathlib.Path(BASE_URL_OVERRIDE_FILE).read_text().strip().rstrip("/")
    except Exception:  # noqa: BLE001 — no file is the normal case
        moved = ""
    if not moved.startswith("https://"):
        return env
    os.environ["CANOPY_BASE_URL"] = moved
    if os.environ.get("CANOPY_WEB_API_URL", "").rstrip("/") in ("", env):
        os.environ["CANOPY_WEB_API_URL"] = moved
    return moved


BASE_URL = _initial_base_url()
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
# A directed-only box (a test box, `up.sh --test`) claims only turns pinned to it
# and turns of chats bound to it: no routed agent turn, no repo turn, no unbound
# chat. A self-restriction — it can only make the box take less — so the server
# trusts it as declared (`Runner.directed_only`).
if _bool_env("RUNNER_DIRECTED", False):
    RUNNER_CAPS["directed"] = True
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
#: Which agents this box has clones (and gog credentials) for. Set by cloud-init
#: from the same CFN parameter bootstrap_agents.sh provisions from, so the list
#: the mailbox map is built from is the list that was actually provisioned.
AGENT_SLUGS = os.environ.get("AGENT_SLUGS", "")
#: The timer half of inbox delivery. The doorbell is the delivery mechanism; this
#: is the AUDITOR — mail the timer finds is mail push failed to ring for, which is
#: what makes a broken watch loud instead of silent. Same 300s as the laptop.
INBOX_POLL_SECONDS = int(os.environ.get("INBOX_POLL_SECONDS", "300"))
CANOPY_WEB_REPO_DIR = os.environ.get("CANOPY_WEB_REPO_DIR", "/opt/canopy-web")
CANOPY_WEB_REPO_URL = os.environ.get("CANOPY_WEB_REPO_URL", "https://github.com/dimagi-internal/canopy-web.git")
#: The runner's own clone, kept apart from CANOPY_WEB_REPO_DIR on purpose. That
#: one is reachable by agent turns (it is where anything that wants a canopy-web
#: checkout finds one), and on 2026-09-22 a turn left it on a local branch: the
#: next start's `git pull --ff-only` failed, the packages below were never put on
#: sys.path, and the box ran six hours with no transcripts, no inbox and no ACP
#: while reporting ready. Nothing but this process writes here, and it is reset
#: to origin/main on every start rather than merged into.
RUNNER_SRC_DIR = os.environ.get("RUNNER_SRC_DIR", "/opt/canopy-runner/src")
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
# Idle back-off for that clock (#647): once no transcript has grown for
# STREAM_IDLE_AFTER_SECONDS, tail every STREAM_IDLE_POLL_SECONDS instead. A
# viewer attaching rings the `stream` doorbell, which syncs at once whatever the
# clock says, and the first growth snaps the clock back to STREAM_POLL_SECONDS.
# At 3s an idle box asked /streams 1,200 times an hour for nothing.
STREAM_IDLE_AFTER_SECONDS = float(os.environ.get("STREAM_IDLE_AFTER_SECONDS", "60"))
STREAM_IDLE_POLL_SECONDS = float(os.environ.get("STREAM_IDLE_POLL_SECONDS", "15"))
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
#   runner-token.json — this box's current runner token (0600), so the updater
#                     reads its expected code as the box, not with a PAT.
RUNNER_HOME = pathlib.Path(os.environ.get("RUNNER_HOME", "/opt/canopy-runner"))
BUILD_INFO_FILE = pathlib.Path(os.environ.get("BUILD_INFO_FILE", str(RUNNER_HOME / "build-info.json")))
IN_FLIGHT_FILE = pathlib.Path(os.environ.get("IN_FLIGHT_FILE", str(RUNNER_HOME / "in-flight")))
RUNNER_TOKEN_FILE = pathlib.Path(os.environ.get("RUNNER_TOKEN_FILE", str(RUNNER_HOME / "runner-token.json")))

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


# ── keyless: the box signs in as itself ─────────────────────────────────────
# The box's API credential is a RunnerAccessToken canopy-web hands it for a fresh
# proof of its APPROVED device key — this box's EC2 instance role
# (`POST /api/runner-sessions/`, canopy-web apps/harness/api_runner_sessions.py).
# It lasts an hour and opens the runner protocol only, so no long-lived secret
# has to live on the box. CANOPY_TOKEN (a person's PAT) is only the fallback for
# a box whose key is not approved yet — and the reason this exists: on
# 2026-10-17 that PAT was revoked in a token cleanup and cloud-ec2-2 lost every
# turn it claimed.
_RUNNER_TOKEN = {"raw": "", "exp": 0.0, "failed_at": 0.0, "announced": False}
_RUNNER_TOKEN_LOCK = threading.Lock()
#: Get the next token this long before the current one runs out.
RUNNER_TOKEN_RENEW_SECONDS = 600
#: After a failed exchange, wait this long before trying again (the fallback, if
#: any, is used meanwhile) — a pending key must not cost an STS round trip per call.
RUNNER_TOKEN_RETRY_SECONDS = 60


def _exchange_device_proof(runner_id: str) -> bool:
    """Trade a fresh device proof for a runner token — the instance role's, or for an
    isolation unit its own software key's (`_exchange_unit_proof`). Never raises."""
    if UNIT:
        raw, exp = _exchange_unit_proof(runner_id)
        if not raw:
            return False
        _RUNNER_TOKEN.update(raw=raw, exp=exp)
        _share_runner_token(raw, exp)
        if not _RUNNER_TOKEN["announced"]:
            _RUNNER_TOKEN["announced"] = True
            _log(f"keyless: unit {UNIT} signed in with its own device key (no PAT, no instance role)")
        return True
    url = f"{BASE_URL}/api/runner-sessions/"
    headers = _aws_proof_headers(url)
    if not headers:
        return False
    req = urllib.request.Request(url, data=json.dumps({"runner": runner_id}).encode(),
                                 method="POST")
    for name, value in headers.items():
        req.add_header(name, value)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Canopy-Client", "canopy-cloud-runner")
    req.add_header("User-Agent", f"canopy-cloud-runner host={socket.gethostname()}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        _log(f"keyless: canopy-web refused the device proof ({exc.code}) — is this box's "
             f"device key approved? (Settings → Security → Device keys)")
        return False
    except Exception as exc:  # noqa: BLE001
        _log(f"keyless: could not reach canopy-web for a runner token ({exc})")
        return False
    raw = body.get("access_token") or ""
    try:
        exp = datetime.datetime.fromisoformat(
            str(body.get("expires_at", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        exp = time.time() + 3000
    if not raw:
        return False
    _RUNNER_TOKEN.update(raw=raw, exp=exp)
    _share_runner_token(raw, exp)
    if not _RUNNER_TOKEN["announced"]:
        _RUNNER_TOKEN["announced"] = True
        _log("keyless: signed in with this box's device key (no PAT in use)")
    return True


def _share_runner_token(raw: str, exp: float) -> None:
    """Hand the auto-updater (a separate unit, update_runner.sh) this box's token.

    It used to read its expected code with CANOPY_TOKEN from runner.env — a PAT
    revoked on 2026-10-17, after which every check came back "unknown" and the box
    stopped updating. 0600 and written atomically; the token opens the runner
    protocol only and lasts an hour. Never raises."""
    try:
        tmp = RUNNER_TOKEN_FILE.with_name(RUNNER_TOKEN_FILE.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"access_token": raw, "expires_at": exp}, fh)
        os.replace(tmp, RUNNER_TOKEN_FILE)
    except OSError as exc:
        _log(f"warn: could not share the runner token with the updater ({exc})")


def _bearer(min_life: float = RUNNER_TOKEN_RENEW_SECONDS) -> str:
    """The credential for the next canopy-web call: this box's own runner token
    while its device key can get one, else the CANOPY_TOKEN fallback.

    `min_life`: how long the token must still be good for, or a fresh one is got.
    A child process handed the token (bootstrap, prepare) cannot renew it itself,
    so it asks for one that outlives the child."""
    if _RUNNER_ID and os.environ.get("CANOPY_DEVICE_KEY", "1") != "0":
        with _RUNNER_TOKEN_LOCK:
            now = time.time()
            if _RUNNER_TOKEN["raw"] and _RUNNER_TOKEN["exp"] - now > min_life:
                return _RUNNER_TOKEN["raw"]
            if now - _RUNNER_TOKEN["failed_at"] > RUNNER_TOKEN_RETRY_SECONDS:
                if _exchange_device_proof(_RUNNER_ID):
                    return _RUNNER_TOKEN["raw"]
                _RUNNER_TOKEN["failed_at"] = now
            # An unexpired token is still good while the renewal is retried.
            if _RUNNER_TOKEN["raw"] and _RUNNER_TOKEN["exp"] > now:
                return _RUNNER_TOKEN["raw"]
    # No PAT fallback for a box that holds none by design (spec §5): a unit, or a box
    # paired by invitation. Without a token it reports itself unauthenticated and its
    # device-key path asks the owner — it never borrows a person's credential.
    if _no_pat_fallback():
        return ""
    return TOKEN


#: The state file's `paired_by`, read once (`_no_pat_fallback`); pairing sets it.
_PAIRED_BY: dict = {"v": None}


def _no_pat_fallback() -> bool:
    if UNIT:
        return True
    if _PAIRED_BY["v"] is None:
        try:
            _PAIRED_BY["v"] = str(json.loads(STATE_FILE.read_text()).get("paired_by") or "")
        except (OSError, ValueError, AttributeError):
            return False
    return _PAIRED_BY["v"] == "invitation"


def _drop_runner_token() -> bool:
    """Forget the runner token after a 401 (its key was revoked, or the server
    restarted its clock on it). True if there was one to drop."""
    with _RUNNER_TOKEN_LOCK:
        had = bool(_RUNNER_TOKEN["raw"])
        _RUNNER_TOKEN.update(raw="", exp=0.0, failed_at=0.0)
    return had


def _api(method: str, path: str, body: dict | None = None, *,
         prefix: str = "/api/harness",
         headers: dict | None = None, _retried: bool = False) -> tuple[int, dict | None]:
    """Call canopy-web. `prefix` defaults to the harness router this runner lives
    on; pass another (e.g. `/api/events`) for the few cross-router calls."""
    url = f"{BASE_URL}{prefix}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        if value:
            req.add_header(name, value)
    bearer = _bearer()
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Content-Type", "application/json")
    # Provenance (canopy-web apps/common/request_context.py): what canopy records
    # as the program behind anything this request creates.
    req.add_header("X-Canopy-Client", "canopy-cloud-runner")
    req.add_header("User-Agent", f"canopy-cloud-runner host={socket.gethostname()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            _note_token_accepted()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        _log(f"{method} {path} -> {exc.code}: {raw[:300]!r}")
        if exc.code == 401:
            if bearer.startswith("crt_") and not _retried and _drop_runner_token():
                # A runner token can die mid-hour (its device key revoked): get
                # a fresh one — or fall back — and try this call once more.
                return _api(method, path, body, prefix=prefix, headers=headers, _retried=True)
            _note_token_refused()
        return exc.code, None
    except urllib.error.URLError as exc:
        _log(f"{method} {path} -> URLError {exc.reason}")
        return 0, None


# canopy-web refusing this box's token (a 401 on any call). While it does, the
# box must not CLAIM: a claim can still succeed over an already-open socket, and
# then the turn's events, transcript and finish are all refused and the turn is
# lost (2026-10-17: cloud-ec2-2 swallowed four after its PAT was revoked).
# Cleared by the next accepted call, so replacing the token recovers on its own.
_TOKEN_REFUSED_AT = 0.0


def _note_token_refused() -> None:
    global _TOKEN_REFUSED_AT
    if not _TOKEN_REFUSED_AT:
        _log("canopy-web REFUSED this box's token (401) — not claiming turns until "
             "it is replaced (secret canopy/cloud-runner/canopy-pat, then restart)")
    _TOKEN_REFUSED_AT = time.time()


def _note_token_accepted() -> None:
    global _TOKEN_REFUSED_AT
    if _TOKEN_REFUSED_AT:
        _log("canopy-web accepts this box's token again — claiming resumes")
    _TOKEN_REFUSED_AT = 0.0


def _token_refused() -> bool:
    return bool(_TOKEN_REFUSED_AT)


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
    if os.environ.get("CANOPY_UNIT", "").strip():
        # A unit has no sudo: root's updater timer (30 min) installs for every unit.
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
    body = {
        "active_turn_ids": active_turn_ids,
        "host": RUNNER_HOST,
        "code_sha": info["sha"],
        "code_committed_at": info["committed_at"],
        "health": health_report(),
        # Whether canopy may give this box a CALLER's turn (see `_confine`).
        "profiles": profiles_supported(),
        # The caller-envelope version this code reads (2 = `profile: "confined"`);
        # canopy-web gives caller turns only to a box that reports it.
        "envelope": ENVELOPE_VERSION,
    }
    # Omitted until the first inbox probe: absent is "unknown", [] is "none".
    readable = mailboxes_readable()
    if readable is not None:
        body["mailboxes_readable"] = readable
    unprepared = agents_unprepared()
    if unprepared is not None:
        body["agents_unprepared"] = unprepared
    # Omitted while any credential might work, so the server default (ready)
    # stands; a box whose every credential was just proven not to answer says so.
    ready, ready_note = _credential_readiness()
    if not ready:
        body["ready"] = False
        body["ready_note"] = ready_note[:200]   # Runner.ready_note is 200 chars
    return {**body, **extra}


# ── self-reported health ─────────────────────────────────────────────────────
# What this box can actually do, one named check each, sent on every heartbeat
# (Runner.health on the server). The reason it exists: on 2026-09-22 this box
# read online + ready + code-current while three of its features were off, and
# the only evidence was journald. A feature that turns itself off must say so
# where an operator looks — the runner page — without anyone opening a shell.
#
# Cheap checks (imports, the credential cascade) are recomputed on every beat;
# checks that start a process (versions) are cached for HEALTH_SLOW_SECONDS.

HEALTH_SLOW_SECONDS = int(os.environ.get("HEALTH_SLOW_SECONDS", "600"))
_HEALTH: dict[str, dict] = {}
_HEALTH_LOCK = threading.Lock()
_HEALTH_SLOW_AT = 0.0
#: When bootstrap_agent_fleet last FINISHED (epoch). Reported so the server can
#: discharge a refresh request by observation; 0 = not since this process started.
_BOOTSTRAPPED_AT = 0.0


def _set_check(name: str, status: str, detail: str = "") -> None:
    with _HEALTH_LOCK:
        _HEALTH[name] = {"name": name, "status": status, "detail": detail[:2000]}


def _package_checks() -> None:
    """Did each in-repo package this box depends on actually IMPORT? Asks the
    same lazy loaders the features use, so this cannot disagree with them."""
    _set_check("packages.transcripts", *(
        ("ok", "") if _transcript_core() is not None
        else ("fail", "canopy_transcript did not import: chat sessions on this box write no durable rows")))
    _set_check("packages.inbox", *(
        ("ok", "") if _inbox_core() is not None
        else ("fail", "canopy_runner did not import: this box reads no mail and the Gmail doorbell does nothing")))
    if RUNNER_EXECUTOR == "acp":
        _set_check("packages.acp", *(
            ("ok", "") if _acp_core() is not None
            else ("fail", "canopy_acp did not import: turns fall back to claude -p and cannot be stopped")))


def _credential_check() -> None:
    # A credential that left the cascade takes its probe with it: a dot for a
    # token nobody holds any more would be a report about nothing.
    staged = {c[0] for c in _CLAUDE_CREDS}
    with _PROBES_LOCK:
        for label in [lb for lb in _PROBES if lb not in staged]:
            del _PROBES[label]
    with _HEALTH_LOCK:
        for name in [k for k in _HEALTH
                     if k.startswith("claude.probe.") and k[len("claude.probe."):] not in staged]:
            del _HEALTH[name]
    n = len(_CLAUDE_CREDS)
    if n == 0:
        _set_check("claude.credentials", "fail",
                   "no Claude credential staged: this box cannot run a turn. Sign in with "
                   "Claude (or create a long-lived token) on the runner's credentials.")
    elif n == 1:
        _set_check("claude.credentials", "warn",
                   "one Claude credential: a usage cap stops every agent here with nothing to fail over to")
    else:
        _set_check("claude.credentials", "ok", f"{n} credentials in the fallback chain")
    # Separate from the count on purpose: a box can have plenty of credentials
    # and still none that sees a claude.ai connector. The credentials page reads
    # this one check to say whether the runner is signed in.
    first = next((c[0] for c in _CLAUDE_CREDS if _is_login(c[0])), "")
    if first:
        who = box_login_account(key=_login_key_of(first))
        _set_check("claude.login", "ok",
                   f"signed in on this box as {who} — claude.ai connectors available" if who
                   else "signed in on this box — claude.ai connectors available")
    else:
        _set_check("claude.login", "warn",
                   "not signed in on this box: turns run on long-lived tokens, which cannot use "
                   "claude.ai connectors (Claude Docs). Sign in with Claude on the runner's credentials.")


#: Root-volume thresholds (percent used). cloud-ec2-1 reached 100% on 2026-10-04 —
#: a plugin cache holding every version it ever installed — and nothing said so
#: until turns started failing on writes. Overridable for a box with odd mounts.
DISK_WARN_PCT = float(os.environ.get("DISK_WARN_PCT", "80"))
DISK_FAIL_PCT = float(os.environ.get("DISK_FAIL_PCT", "90"))
DISK_PATH = os.environ.get("DISK_PATH", "/")


def _disk_check() -> None:
    """How full the root volume is. One statvfs, so cheap enough for every beat —
    and a disk filling up is exactly the thing that should not wait ten minutes."""
    try:
        du = shutil.disk_usage(DISK_PATH)
    except OSError as exc:
        _set_check("disk", "warn", f"could not read usage of {DISK_PATH}: {exc}")
        return
    pct = 100.0 * du.used / du.total if du.total else 0.0
    gib = 1024 ** 3
    detail = (f"{DISK_PATH} {pct:.0f}% used ({du.used / gib:.1f} of {du.total / gib:.1f} GiB, "
              f"{du.free / gib:.1f} GiB free)")
    if pct >= DISK_FAIL_PCT:
        _set_check("disk", "fail", detail + ": turns will start failing on writes. "
                   "Refresh this runner (bootstrap prunes the plugin/npm/uv caches) "
                   "or grow the volume (VolumeSize)")
    elif pct >= DISK_WARN_PCT:
        _set_check("disk", "warn", detail + ": refresh this runner to prune caches")
    else:
        _set_check("disk", "ok", detail)


def _version_of(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return ""
    if out.returncode != 0:
        return ""
    # `claude --version` says "2.1.266 (Claude Code)", `canopy --version` says
    # "canopy 0.2.509": the version is the one dotted number in either.
    m = re.search(r"\d+\.\d+\.\d+", out.stdout or "")
    return m.group(0) if m else ""


def _marketplace_canopy_version() -> str:
    """The canopy plugin version the marketplace clone holds — what the CLI
    should match. Read from the plugin's own manifest, like bootstrap does."""
    root = pathlib.Path.home() / ".claude" / "plugins" / "marketplaces" / "canopy"
    for rel in ("plugins/canopy/.claude-plugin/plugin.json", ".claude-plugin/plugin.json"):
        try:
            v = json.loads((root / rel).read_text()).get("version")
        except Exception:  # noqa: BLE001
            continue
        if v:
            return str(v)
    return ""


def _slow_checks() -> None:
    claude = _version_of([CLAUDE_BIN, "--version"])
    _set_check("claude.version", *(("ok", f"Claude Code {claude}") if claude
                                   else ("fail", f"`{CLAUDE_BIN} --version` did not run")))
    cli = _version_of(["canopy", "--version"])
    want = _marketplace_canopy_version()
    if not cli:
        _set_check("canopy.cli", "fail", "the canopy CLI did not run")
    elif want and cli != want:
        _set_check("canopy.cli", "warn", f"canopy CLI {cli}, but the plugin is {want}: refresh this runner")
    else:
        _set_check("canopy.cli", "ok", f"canopy {cli}")


def health_report() -> dict:
    """The `health` block for a heartbeat. Never raises: health is decoration on
    a liveness call, and a check that throws must not cost the beat."""
    global _HEALTH_SLOW_AT
    try:
        _credential_check()
        _disk_check()
        if _BOOTSTRAPPED_AT:
            # Before bootstrap the packages are not exposed yet; reporting them
            # as failed would be a false alarm on every start.
            _package_checks()
        now = time.time()
        if now - _HEALTH_SLOW_AT >= HEALTH_SLOW_SECONDS:
            _HEALTH_SLOW_AT = now
            _slow_checks()
    except Exception as exc:  # noqa: BLE001
        _log(f"health: a check raised ({exc}); reporting what completed")
    with _HEALTH_LOCK:
        checks = list(_HEALTH.values())
    return {"checks": checks, "checked_at": time.time(), "bootstrapped_at": _BOOTSTRAPPED_AT}


# ── self-refresh ─────────────────────────────────────────────────────────────
# A refresh is a restart: bootstrap runs once per service start (see
# bootstrap_agent_fleet), and it is what updates the canopy plugin + CLI, Claude
# Code and each agent's provisioning — so re-running it IS restarting. The
# scoped sudoers rule the auto-updater uses already allows exactly this command.
#
# Two triggers: an operator's request (Runner.refresh_pending, read off the
# heartbeat reply — durable, so a dropped socket costs a beat, not the request),
# and age, because without it a box's plugins only moved when its RUNNER code
# changed, which is how hal's cloud drill came to fail on a stale canopy CLI.

REFRESH_MAX_AGE_SECONDS = int(os.environ.get("REFRESH_MAX_AGE_SECONDS", str(24 * 3600)))
#: Floor between self-restarts, persisted across them, so a bootstrap that
#: cannot complete can never become a restart loop.
REFRESH_MIN_INTERVAL_SECONDS = int(os.environ.get("REFRESH_MIN_INTERVAL_SECONDS", "1800"))


def _refresh_marker() -> pathlib.Path:
    return pathlib.Path(RUNNER_HOME) / "last-self-refresh"


def _refresh_reason(pending: bool, now: float) -> str:
    if pending:
        return "an operator asked for a refresh"
    if _BOOTSTRAPPED_AT and now - _BOOTSTRAPPED_AT >= REFRESH_MAX_AGE_SECONDS:
        return f"last bootstrap was {int((now - _BOOTSTRAPPED_AT) // 3600)}h ago"
    return ""


def maybe_self_refresh(pending: bool, *, now: float | None = None) -> bool:
    """Restart this service to re-run bootstrap, if asked or overdue AND idle.

    Called only from the thread that CLAIMS turns, right after a heartbeat, so
    "idle" cannot change between the check and the restart: nothing else here
    claims. Returns True when a restart was started."""
    now = time.time() if now is None else now
    reason = _refresh_reason(pending, now)
    if not reason or _in_flight_ids():
        return False
    try:
        last = float(_refresh_marker().read_text().strip() or 0)
    except Exception:  # noqa: BLE001
        last = 0.0
    if now - last < REFRESH_MIN_INTERVAL_SECONDS:
        return False
    try:
        _refresh_marker().write_text(str(now))
    except Exception as exc:  # noqa: BLE001
        _log(f"self-refresh: cannot write {_refresh_marker()} ({exc}); not restarting without the loop guard")
        return False
    _log(f"self-refresh: {reason}; restarting to re-run bootstrap")
    try:
        subprocess.run(["sudo", "-n", "systemctl", "restart", "canopy-runner.service"],
                       capture_output=True, timeout=30, check=True)
    except Exception as exc:  # noqa: BLE001
        _log(f"self-refresh: restart failed ({exc})")
        return False
    return True


#: Addresses canopy has left (it moved to canopy.dimagi.com on 2026-10-05).
FORMER_BASES = frozenset({"https://labs.connect.dimagi.com/canopy"})


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
    # A confined turn's native permission layer (see `_native_settings`). Deny rules
    # hold under --dangerously-skip-permissions; the flag above stays because this
    # box has no human to answer a prompt.
    settings = getattr(_TURN_ENV, "settings", None)
    if settings:
        cmd += ["--settings", str(settings)]
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
            _, reply = _api("POST", f"/runners/{runner_id}/heartbeat", _heartbeat_body([turn_id]))
            # The reply names this turn if a person asked to stop it — the
            # durable copy of the `cancel` frame, heard even with no socket.
            honour_stops(reply)

    threading.Thread(target=_loop, daemon=True, name=f"lease-{turn_id[:8]}").start()
    return stop


def pair_or_load() -> str:
    if UNIT and not STATE_FILE.exists():
        # A unit never pairs itself: root's canopy-pair pairs every unit at once.
        if not _wait_for_unit_state():
            sys.exit(0)
    if STATE_FILE.exists():
        rid = json.loads(STATE_FILE.read_text()).get("runner_id")
        if rid:
            # Known before the first call, so that call can already sign in as
            # this box (`_bearer`) rather than needing a PAT.
            global _RUNNER_ID
            _RUNNER_ID = rid
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
    if UNIT:
        _log(f"FATAL: unit {UNIT}'s runner is gone from canopy-web (retired?) — re-run "
             "canopy-pair (`sudo systemctl restart canopy-pair`) after a new invitation")
        sys.exit(1)
    if not TOKEN:
        # A new box holds no PAT: it pairs by the invitation up.sh made for its
        # instance role, once the operator approves it with a passkey.
        rid = pair_by_role()
        if not rid:
            _log("FATAL: could not pair by invitation (no EC2 instance role, or stopped).")
            sys.exit(1)
        return rid
    # An old stack (CANOPY_TOKEN set, no state yet) pairs with its PAT, as before.
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


PAIR_BY_ROLE_PATH = "/api/harness/runners/pair-by-role"


def _post_unauthenticated(url: str, body: dict) -> tuple[int, dict | None]:
    """POST JSON with NO Authorization header: pair-by-role takes no token, and an
    empty `Bearer` would be read as a bad credential."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Canopy-Client", "canopy-cloud-runner")
    req.add_header("User-Agent", f"canopy-cloud-runner host={socket.gethostname()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else None)
        except ValueError:
            return exc.code, None
    except (urllib.error.URLError, OSError) as exc:
        _log(f"pair-by-role: could not reach canopy-web ({exc})")
        return 0, None


def pair_by_role() -> str | None:
    """Pair this box by its EC2 instance role and the invitation `up.sh` made for
    it (spec 2026-10-19 #9) — no PAT. Asks until a person approves the
    invitation: 202 means waiting, 401 means no approved invitation matches this
    role and name (yet). 201 paired / 200 reattached (a replaced instance of an
    approved role) → the state file is written and the runner id returned. None
    when the box has no instance role to prove, or it is stopping."""
    url = BASE_URL + PAIR_BY_ROLE_PATH
    ecdh: dict | None = None
    role = ""
    while not _stop:
        creds = _aws_instance_credentials()
        if not creds:
            _log("pair-by-role: no EC2 instance role here — this box cannot pair without a token")
            return None
        if ecdh is None:
            ecdh = _ecdh_public_key()
        body = {"name": RUNNER_NAME, "kind": "cloud", "capabilities": RUNNER_CAPS, "host": RUNNER_HOST,
                "proof": aws_proof(url, creds=creds)}
        if ecdh:
            body["ecdh_public_jwk"] = ecdh["jwk"]
            body["ecdh_store"] = ecdh.get("kind", "")
        status, reply = _post_unauthenticated(url, body)
        rid = (reply or {}).get("runner_id") if isinstance(reply, dict) else None
        if status in (200, 201) and rid:
            # `paired_by`: this box holds no PAT by design, so `_bearer` never falls
            # back to one (spec 2026-10-19-runner-isolation-units §5).
            STATE_FILE.write_text(json.dumps({"runner_id": rid, "paired_by": "invitation"}))
            _PAIRED_BY["v"] = "invitation"
            how = "re-attached to" if status == 200 else "paired as new"
            _log(f"pair-by-role: {how} runner {rid} ({RUNNER_NAME}, caps={RUNNER_CAPS})")
            return rid
        if status == 202:
            _log(f"pair-by-role: waiting for the pairing invitation to be approved ({RUNNER_NAME})")
        elif status == 401:
            if not role:
                try:
                    role = _aws_role_arn(creds)
                except Exception:  # noqa: BLE001 — only for the log line
                    role = "this box's instance role"
            _log(f"pair-by-role: no approved pairing invitation for {role} / {RUNNER_NAME} — run up.sh "
                 f"(it creates one and prints its approve link); asking again in {POLL_SECONDS}s")
        else:
            _log(f"pair-by-role: HTTP {status}; asking again in {POLL_SECONDS}s")
        time.sleep(POLL_SECONDS)
    return None


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


#: Per-turn environment the worker thread adds on top of `_agent_env` — today only
#: CANOPY_PROFILE for a confined turn (see `_confine`). Thread-local because every
#: turn runs on its own thread, so this cannot leak from one turn into another.
_TURN_ENV = threading.local()


#: Per-agent 1Password keys, resolved from canopy-web and cached briefly. The
#: cache keeps a turn from re-fetching on every claim; the TTL keeps a rotated
#: or revoked key from living on this box longer than a few minutes.
_AGENT_OP: dict[str, tuple[float, str]] = {}
_AGENT_OP_TTL_SECONDS = float(os.environ.get("AGENT_OP_TTL_SECONDS", "300"))


#: The 1Password vault each agent INSTANCE is bound to (`Agent.op_vault`), from
#: the same resolve call as its key and cached with it. Agent `.env.tpl`s name it
#: as `op://${AGENT_VAULT}/...` rather than hardcoding `Agent-<Slug>`, so an
#: install binds to the instance's recorded vault (2026-10-14, canopy#899).
#: Never derived from the slug: an agent with no recorded vault is unconfigured.
_AGENT_VAULT: dict[str, str] = {}


#: The chrome-sales Salesforce credential each agent BORROWS (canopy-web#1291),
#: fetched on the same resolve call as its 1Password key and cached with it.
_AGENT_SF: dict[str, str] = {}


def _delegated_chrome_sales_home(slug: str) -> pathlib.Path:
    """Where chrome-sales reads Salesforce creds in THIS agent's turns — the same
    path the canopy plugin uses on a laptop, so chrome-sales has one rule."""
    return pathlib.Path.home() / ".canopy" / "delegated" / slug / "chrome-sales"


def _stage_salesforce(slug: str) -> pathlib.Path | None:
    """Write the borrowed `.sf-creds.json` for this agent (0600), or REMOVE a stale
    one when canopy-web says it borrows none — a withdrawn loan must stop on the
    box, not linger in a file. Returns the dir to point CHROME_SALES_HOME at."""
    home = _delegated_chrome_sales_home(slug)
    target = home / ".sf-creds.json"
    creds = _AGENT_SF.get(slug, "")
    try:
        if not creds:
            target.unlink(missing_ok=True)
            return None
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(creds)
        os.replace(tmp, target)
        return home
    except OSError as exc:
        _log(f"warn: could not stage {slug}'s borrowed Salesforce credential ({exc})")
        return None


# ── Canopy Vault device proof: this box's IAM role (canopy-web#1549, Phase 1b) ──
#
# An EC2 box has no Secure Enclave; the strongest identity it has is its instance
# role. As in HashiCorp Vault's AWS auth, it proves the role by SigV4-signing — and
# NOT sending — an `sts:GetCallerIdentity`, and hands canopy-web the signed request in
# `X-Canopy-Aws-Proof`: standard base64 of compact JSON {method, url, headers, body}.
# canopy-web replays it to its own pinned https://sts.amazonaws.com/ and accepts the
# proof when the ARN AWS answers with is an ACTIVE aws-iam RunnerDeviceKey of a runner
# the agent routes to. `X-Canopy-Proof-Htu` (the URL being called, no query) and
# `X-Canopy-Proof-Iat` (unix seconds) are SIGNED headers, so a captured proof works
# against no other endpoint and goes stale.
#
# The signer is the stdlib's hmac/hashlib, not botocore: this file runs on the box's
# system python3 and must stay stdlib-only (tests/test_stdlib_only.py). It is pinned
# byte-for-byte to botocore's SigV4Auth by tests/test_aws_device_proof.py, and canopy's
# `device_key_aws.py` carries the same signer for the canopy CLI's resolves on this box
# (`canopy agent prepare --runner`, `cred sync`) — which is why enrollment also writes
# canopy's status cache below. Credentials come from IMDSv2, live only in memory, and
# are never logged. Every step fails OPEN: no proof is a header not sent, never a turn
# that cannot run (canopy-web decides whether a proof is required).
AWS_PROOF_HEADER = "X-Canopy-Aws-Proof"
_STS_URL = "https://sts.amazonaws.com/"
_STS_HOST = "sts.amazonaws.com"
_STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"
_STS_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"
_IMDS_TIMEOUT = 2.0
_AWS_CREDS: dict = {}
_AWS_CREDS_LOCK = threading.Lock()


def _imds_base() -> str:
    return (os.environ.get("CANOPY_AWS_IMDS_URL", "").strip()
            or "http://169.254.169.254").rstrip("/")


def _imds(path: str, *, token: str = "", put: bool = False, headers: dict | None = None) -> str:
    req = urllib.request.Request(_imds_base() + path, method="PUT" if put else "GET",
                                 data=b"" if put else None)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    if token:
        req.add_header("X-aws-ec2-metadata-token", token)
    with urllib.request.urlopen(req, timeout=_IMDS_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _aws_instance_credentials() -> dict | None:
    """The instance role's temporary credentials (IMDSv2) as {access_key, secret_key,
    token, expires_at}, or None off EC2 / with no role. Cached until 5 min before expiry."""
    now = time.time()
    with _AWS_CREDS_LOCK:
        hit = _AWS_CREDS.get(_imds_base())
        if hit and (not hit["expires_at"] or hit["expires_at"] - 300 > now):
            return hit
    try:
        token = _imds("/latest/api/token", put=True,
                      headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"})
        roles = _imds("/latest/meta-data/iam/security-credentials/", token=token).split()
        if not roles:
            return None
        doc = json.loads(_imds(f"/latest/meta-data/iam/security-credentials/{roles[0]}",
                               token=token))
        if doc.get("Code", "Success") != "Success" or not doc.get("AccessKeyId"):
            return None
        try:
            expires = datetime.datetime.strptime(
                str(doc.get("Expiration")), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            expires = 0.0
        creds = {"access_key": str(doc["AccessKeyId"]), "secret_key": str(doc["SecretAccessKey"]),
                 "token": str(doc.get("Token") or ""), "expires_at": expires}
    except Exception:  # noqa: BLE001 — not EC2, IMDS off, no role: all mean "no identity"
        return None
    with _AWS_CREDS_LOCK:
        _AWS_CREDS[_imds_base()] = creds
    return creds


def _sigv4(creds: dict, headers: dict, amz_date: str) -> dict:
    """`headers` + X-Amz-Date, X-Amz-Security-Token and Authorization for a POST of
    _STS_BODY to https://sts.amazonaws.com/ — every given header signed."""
    out = dict(headers)
    out["X-Amz-Date"] = amz_date
    if creds.get("token"):
        out["X-Amz-Security-Token"] = creds["token"]
    canon = {name.lower().strip(): " ".join(str(v).split()) for name, v in out.items()}
    canon.setdefault("host", _STS_HOST)
    signed = ";".join(sorted(canon))
    canonical = "\n".join(["POST", "/", "", "".join(f"{k}:{canon[k]}\n" for k in sorted(canon)),
                           signed, hashlib.sha256(_STS_BODY.encode()).hexdigest()])
    scope = f"{amz_date[:8]}/us-east-1/sts/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + creds["secret_key"]).encode()
    for part in (amz_date[:8], "us-east-1", "sts", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds['access_key']}/{scope}, "
                            f"SignedHeaders={signed}, Signature={sig}")
    return out


def _signed_get_caller_identity(creds: dict, htu: str = "", *, now: float | None = None,
                                iat: int | None = None, jti: str | None = None) -> dict:
    """The unsent STS request. With `htu`, the binding headers are signed, plus a fresh
    random X-Canopy-Proof-Jti (``jti=""`` omits it): canopy-web keeps each proof single-
    use, and without a nonce two proofs for one htu in the same second are identical —
    the second refused as a replay (canopy's device_key_aws.JTI_HEADER, 2026-10-17)."""
    t = time.time() if now is None else now
    headers = {"Content-Type": _STS_CONTENT_TYPE, "Host": _STS_HOST}
    if htu:
        headers["X-Canopy-Proof-Htu"] = htu
        headers["X-Canopy-Proof-Iat"] = str(int(t if iat is None else iat))
        nonce = secrets.token_hex(16) if jti is None else jti
        if nonce:
            headers["X-Canopy-Proof-Jti"] = nonce
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))
    return {"method": "POST", "url": _STS_URL, "headers": _sigv4(creds, headers, amz_date),
            "body": _STS_BODY}


def _htu(url: str) -> str:
    """scheme://host/path — no query, no fragment (the contract's htu)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def aws_proof(url: str, *, creds: dict | None = None, now: float | None = None) -> str:
    """The X-Canopy-Aws-Proof value for a request to `url`, or "" when this box has no
    instance role to prove."""
    creds = creds or _aws_instance_credentials()
    if not creds:
        return ""
    req = _signed_get_caller_identity(creds, _htu(url), now=now)
    return base64.b64encode(json.dumps(req, separators=(",", ":")).encode()).decode("ascii")


def _aws_proof_headers(url: str) -> dict:
    """{X-Canopy-Aws-Proof: …} for every resolve this box makes, or {} — never raises.
    A unit has no instance role to prove (IMDS is root's): {}."""
    if os.environ.get("CANOPY_DEVICE_KEY", "1") == "0" or UNIT:
        return {}
    try:
        proof = aws_proof(url)
    except Exception as exc:  # noqa: BLE001 — a proof is an addition, never a new failure
        _log(f"warn: could not build the aws-iam device proof ({exc.__class__.__name__})")
        return {}
    return {AWS_PROOF_HEADER: proof} if proof else {}


def normalize_role_arn(arn: str) -> str:
    """arn:aws:sts::1:assumed-role/ROLE/i-… → arn:aws:iam::1:role/ROLE (canopy-web
    normalises what STS answers the same way)."""
    m = re.fullmatch(r"arn:([\w-]+):sts::(\d+):assumed-role/([^/]+)/.+", arn.strip())
    return f"arn:{m.group(1)}:iam::{m.group(2)}:role/{m.group(3)}" if m else arn.strip()


def _aws_role_arn(creds: dict) -> str:
    """SEND one GetCallerIdentity (needs no IAM permission) and return the role ARN."""
    doc = _signed_get_caller_identity(creds)
    req = urllib.request.Request(doc["url"], data=doc["body"].encode(), method="POST")
    for name, value in doc["headers"].items():
        req.add_header(name, value)
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode("utf-8")
    m = re.search(r"<Arn>([^<]+)</Arn>", text)
    if not m:
        raise RuntimeError("STS answered without an Arn")
    return normalize_role_arn(m.group(1))


def _device_key_dir() -> pathlib.Path:
    override = os.environ.get("CANOPY_DEVICE_KEY_DIR", "").strip()
    return pathlib.Path(override).expanduser() if override else (
        pathlib.Path.home() / ".canopy" / "device-key")


def _write_device_key_cache(role_arn: str, status: dict) -> None:
    """canopy's own cache files (device_key.aws_path / status_path), so the canopy CLI's
    resolves on this box — `canopy agent prepare --runner`, `cred sync` — attach the same
    proof. Not secrets: a role ARN and a key's id/status."""
    d = _device_key_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private_text(d / "aws-iam.json", json.dumps({"role_arn": role_arn}, indent=1))
    _write_private_text(d / "status.json", json.dumps(status, indent=1, sort_keys=True))


def _ecdh_public_key() -> dict:
    """This box's ECDH key for the zero-knowledge vault (canopy-web#1550), made by the
    canopy CLI — this file stays stdlib-only, and making a P-256 key needs a crypto
    library. The private half is in AWS KMS when the box's `<stack>-ecdh` stack made
    it one (canopy's device_key_kms; kind "aws-kms"), else canopy's 0600
    `~/.canopy/device-key/ecdh.pem`; only
    `{kind, jkt, jwk}` (public) comes back. {} when canopy cannot (older CLI, no CLI) —
    the role is then enrolled exactly as before. Never raises."""
    try:
        res = subprocess.run([_canopy_cli(), "vault", "device", "--ecdh-jwk", "--kind",
                              "aws-iam"], capture_output=True, text=True, timeout=60,
                             env=_child_safe_env())
        if res.returncode != 0:
            why = " ".join((res.stderr or res.stdout or "").split())[-200:]
            _log(f"device key: no ECDH key for the zero-knowledge vault ({why or res.returncode})")
            return {}
        out = json.loads((res.stdout or "").strip().splitlines()[-1])
        jwk = out.get("jwk") if isinstance(out, dict) else None
        if not isinstance(jwk, dict) or set(jwk) != {"kty", "crv", "x", "y"}:
            _log("device key: canopy printed no public ECDH JWK")
            return {}
        return {"jwk": jwk, "jkt": str(out.get("jkt") or ""),
                "kind": str(out.get("kind") or "software")}
    except Exception as exc:  # noqa: BLE001 — an ECDH key is an addition, never a failure
        _log(f"device key: no ECDH key ({exc.__class__.__name__}: {exc})")
        return {}


def enroll_aws_device_key(runner_id: str) -> dict:
    """Register this box's instance role as its device key: `POST /api/runner-device-keys/`
    {runner, kind: "aws-iam", role_arn, label, proof, ecdh_public_jwk}. Idempotent on the
    server. Logs the approve URL while pending. Returns the status, or {"error": …}; never
    raises."""
    if os.environ.get("CANOPY_DEVICE_KEY", "1") == "0":
        return {"skipped": "CANOPY_DEVICE_KEY=0"}
    try:
        creds = _aws_instance_credentials()
        if not creds:
            _log("device key: no EC2 instance role here — aws-iam device key not enrolled")
            return {"error": "no instance role"}
        role_arn = _aws_role_arn(creds)
        path = "/api/runner-device-keys/"
        ecdh = _ecdh_public_key()
        payload = {"runner": runner_id, "kind": "aws-iam", "role_arn": role_arn,
                   "label": f"{RUNNER_NAME} (EC2 instance role)",
                   "proof": aws_proof(BASE_URL + path, creds=creds)}
        if ecdh:
            payload["ecdh_public_jwk"] = ecdh["jwk"]
            # Where its private half lives ("aws-kms" or "software"), so Settings
            # can say "key held by AWS". Display only.
            payload["ecdh_store"] = ecdh.get("kind", "")
        status, body = _api("POST", "/", payload, prefix="/api/runner-device-keys")
    except Exception as exc:  # noqa: BLE001 — never take the runner down over its key
        _log(f"device key: aws-iam enrollment failed ({exc.__class__.__name__}: {exc})")
        return {"error": str(exc)}
    if status not in (200, 201) or not isinstance(body, dict):
        _log(f"device key: canopy-web did not register the aws-iam key (HTTP {status}) — "
             "an older canopy-web, or the role is not this runner's")
        return {"error": f"HTTP {status}"}
    st = {"id": body.get("id"), "jkt": str(body.get("jkt") or ""),
          "status": str(body.get("status") or "pending"),
          "approve_url": str(body.get("approve_url") or ""), "kind": "aws-iam",
          "runner": runner_id, "role_arn": role_arn, "checked_at": time.time()}
    if ecdh:
        # canopy's cache fields, so `canopy vault zk` and a ZK resolve on this box know
        # which ECDH key is enrolled.
        st.update(ecdh_jkt=ecdh["jkt"], ecdh_kind=ecdh.get("kind") or "software")
    try:
        _write_device_key_cache(role_arn, st)
    except OSError as exc:
        _log(f"warn: device key: could not write canopy's status cache ({exc})")
    _log(f"device key: aws-iam {role_arn} for runner {runner_id} — {st['status']}")
    if st["status"] == "pending" and st["approve_url"]:
        _log(f"device key: PENDING — approve this box's instance role in canopy-web "
             f"(passkey step-up): {st['approve_url']}")
    return st


def start_aws_device_key_enrollment(runner_id: str) -> threading.Thread:
    """Enroll in the background so a slow STS/canopy-web never delays the first claim."""
    t = threading.Thread(target=enroll_aws_device_key, args=(runner_id,), daemon=True,
                         name="aws-device-key-enroll")
    t.start()
    return t


# ── Isolation units: one box, many tenants (spec 2026-10-19-runner-isolation-units) ──
#
# A box may run several isolation units — a workspace TREE, or a single AGENT —
# each its own Unix user (`cu-<unit>`), home, runner process
# (`canopy-runner@<unit>.service`), `Runner` row and device identity. The instance
# role is root's: an iptables rule rejects IMDS for every other uid, so a unit can
# neither sign as the box nor call KMS. A unit therefore holds:
#
# * a SOFTWARE signing key in its home (`~/.canopy/device-key/sw.pem`), made by the
#   canopy CLI (`canopy vault device --public-jwk --kind software`) — this file has
#   no EC crypto (stdlib-only), so the CLI also signs its DPoP proofs
#   (`canopy vault runner-session`);
# * an ECDH key in KMS, one per unit, which only the root-owned broker
#   (`kms_broker.py`, a Unix socket checked by SO_PEERCRED) derives with on its
#   behalf (`CANOPY_ECDH_BROKER`);
# * its own gog secret (`/etc/canopy-runner/<unit>.gog-secret`), from which each
#   agent's keyring password is derived (`gog_env`).
#
# Root does the box-wide parts from this same file: `--setup-units` (users, files,
# the IMDS rule; every start) and `--pair-units` (one `pair-by-role` call for every
# unit, under ONE approval). A box with no `CANOPY_UNIT` in its environment — a
# laptop-shaped single box, or a box built before units — behaves exactly as before.

UNIT = os.environ.get("CANOPY_UNIT", "").strip()
UNIT_KIND = os.environ.get("CANOPY_UNIT_KIND", "").strip()
UNIT_AGENT = os.environ.get("CANOPY_UNIT_AGENT", "").strip()
if UNIT and UNIT_KIND == "agent" and UNIT_AGENT:
    # A self-restriction canopy-web also records from the approved invitation:
    # this runner runs only its agent (`routing.unit_refusal`).
    RUNNER_CAPS["unit"] = {"kind": "agent", "slug": UNIT_AGENT}
UNIT_ETC = pathlib.Path(os.environ.get("CANOPY_UNIT_ETC", "/etc/canopy-runner"))
UNITS_FILE = UNIT_ETC / "units.json"
BOX_ENV_FILE = UNIT_ETC / "box.env"
KMS_BROKER_SOCKET = os.environ.get("CANOPY_KMS_BROKER_SOCKET", "/run/canopy-kms/sock")
TURN_ENV_BASE = pathlib.Path(os.environ.get("CANOPY_TURN_ENV_BASE", "/run/canopy"))
UNIT_HOME_ROOT = os.environ.get("CANOPY_UNIT_HOME_ROOT", "/home")
IMDS_V4, IMDS_V6 = "169.254.169.254", "fd00:ec2::254"
#: `cu-<id>` must fit Linux's 32-character user names.
_UNIT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,27}$")
_RUNNER_NAME = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
#: The KMS alias the `<stack>-ecdh` stack gives a unit's key. The unit that keeps
#: the box's name keeps the box's ORIGINAL key (`alias/canopy-runner-ecdh/<role>`),
#: so a box moved to units loses no wrap; every other unit's has its id appended.
KMS_ALIAS_PREFIX = "alias/canopy-runner-ecdh/"


def parse_units(spec: str, *, workspace: str, box: str) -> list[dict]:
    """`RunnerUnits` (`tree:dimagi,tree:marshellis,agent:eva`) as unit records.

    Empty means ONE unit, `tree:<workspace>`, so a single-tree box keeps its shape.
    `agent:<slug>[@<workspace>]` is an agent unit (its workspace defaults to the
    box's). The tree unit of the box's own workspace keeps the BOX's name — the
    live box keeps its runner (and its id) when it moves to units; every other unit
    is `<box>-<id>`. Raises ValueError on anything it cannot make a unit of."""
    items = [x.strip() for x in (spec or "").split(",") if x.strip()]
    if not items:
        if not workspace:
            raise ValueError("no RunnerUnits and no RunnerWorkspace: nothing to make a unit of")
        items = [f"tree:{workspace}"]
    out: list[dict] = []
    for item in items:
        kind, sep, rest = item.partition(":")
        if not sep or kind not in ("tree", "agent") or not rest:
            raise ValueError(f"unit '{item}': expected tree:<workspace> or agent:<slug>[@<workspace>]")
        if kind == "tree":
            ws, slug, uid = rest, "", rest
        else:
            slug, _, ws = rest.partition("@")
            ws = ws or workspace
            uid = f"agent-{slug}"
        if not _UNIT_ID.match(uid) or not ws:
            raise ValueError(f"unit '{item}': '{uid}' is not a usable unit id (lowercase, digits, '-')")
        name = box if kind == "tree" and ws == workspace else f"{box}-{uid}"
        if not _RUNNER_NAME.match(name):
            raise ValueError(f"unit '{item}': runner name '{name}' is not a valid runner name")
        out.append({"id": uid, "kind": kind, "workspace": ws, "slug": slug, "name": name,
                    "user": f"cu-{uid}", "home": f"{UNIT_HOME_ROOT}/cu-{uid}"})
    for field in ("id", "name"):
        seen = [u[field] for u in out]
        if len(set(seen)) != len(seen):
            raise ValueError(f"RunnerUnits names a unit twice ({field})")
    trees = [u["workspace"] for u in out if u["kind"] == "tree"]
    if len(set(trees)) != len(trees):
        raise ValueError("RunnerUnits names one workspace tree twice")
    return out


def unit_kms_key(unit: dict, box: str, role_name: str) -> str:
    """The KMS key id (an alias) of `unit`'s ECDH key."""
    suffix = "" if unit["name"] == box else f"/{unit['id']}"
    return f"{KMS_ALIAS_PREFIX}{role_name}{suffix}"


def _csv_minus(value: str, drop: set) -> str:
    return ",".join(x for x in (x.strip() for x in (value or "").split(",")) if x and x not in drop)


def unit_env(unit: dict, units: list[dict], box: dict, *, uid: int) -> dict:
    """The environment `canopy-runner@<unit>` runs with (`/etc/canopy-runner/<id>.env`).

    Deliberately WITHOUT `CANOPY_TOKEN` (no PAT on a runner), `CLAUDE_CODE_OAUTH_TOKEN`
    (the Claude bundle comes from canopy-web per unit) and `GOG_KEYRING_PASSWORD`
    (each agent's is derived from the unit's own secret). An agent unit runs only its
    agent; the box's own tree unit runs the box's agents and repos minus every agent
    that has a unit of its own; another tree unit starts with neither and takes what
    canopy-web routes to it."""
    agent_units = {u["slug"] for u in units if u["kind"] == "agent"}
    home = unit["home"]
    if unit["kind"] == "agent":
        agents = slugs = unit["slug"]
        projects = ""
    elif unit["name"] == box.get("RUNNER_NAME"):
        agents = _csv_minus(box.get("RUNNER_AGENTS", ""), agent_units)
        slugs = _csv_minus(box.get("AGENT_SLUGS", ""), agent_units)
        projects = box.get("RUNNER_PROJECTS", "")
    else:
        agents = slugs = projects = ""
    env = {
        "CANOPY_BASE_URL": box.get("CANOPY_BASE_URL", ""),
        "CANOPY_UNIT": unit["id"], "CANOPY_UNIT_KIND": unit["kind"],
        "CANOPY_UNIT_AGENT": unit["slug"],
        "RUNNER_NAME": unit["name"], "RUNNER_HOST": unit["name"],
        "RUNNER_WORKSPACE": unit["workspace"],
        "RUNNER_AGENTS": agents, "AGENT_SLUGS": slugs, "RUNNER_PROJECTS": projects,
        "RUNNER_SESSIONS": box.get("RUNNER_SESSIONS", "1"),
        "RUNNER_DIRECTED": box.get("RUNNER_DIRECTED", "0"),
        "RUNNER_EXECUTOR": box.get("RUNNER_EXECUTOR", "acp"),
        "HOME": home, "USER": unit["user"], "LOGNAME": unit["user"],
        "PATH": f"{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "WORK_DIR": f"{home}/work", "AGENT_ROOT": f"{home}/agents",
        "STATE_FILE": f"{home}/.canopy-cloud-runner.json",
        "RUNNER_TOKEN_FILE": f"{home}/.canopy-runner/runner-token.json",
        "IN_FLIGHT_FILE": f"{home}/.canopy-runner/in-flight",
        "RUNNER_SRC_DIR": f"{home}/.canopy-runner/src",
        "CANOPY_WEB_REPO_DIR": f"{home}/canopy-web",
        "CANOPY_BASE_URL_OVERRIDE_FILE": f"{home}/.canopy-runner/base_url.override",
        "CANOPY_DEVICE_KEY_KIND": "software",
        "CANOPY_ECDH_BROKER": KMS_BROKER_SOCKET,
        "CANOPY_GOG_SECRET_FILE": str(UNIT_ETC / f"{unit['id']}.gog-secret"),
        "CANOPY_TURN_ENV_ROOT": str(TURN_ENV_BASE / str(uid)),
    }
    return env


def _env_text(env: dict) -> str:
    return "".join(f"{k}={v}\n" for k, v in env.items())


def read_env_file(path: pathlib.Path) -> dict:
    """KEY=value lines (systemd EnvironmentFile shape); {} when unreadable."""
    out: dict = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _write_owned(path: pathlib.Path, data: bytes, *, uid: int, gid: int, mode: int, chown) -> None:
    """Write `data` atomically at `mode`, owned by uid:gid (root writes for a unit)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode)
    chown(str(tmp), uid, gid)
    os.replace(tmp, path)


def _mkdir_owned(path: pathlib.Path, *, uid: int, gid: int, mode: int, chown) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    chown(str(path), uid, gid)


def imds_rules(run=subprocess.run) -> list[str]:
    """Reject IMDS for every uid but root's (spec §2): no unit process can fetch the
    instance role's credentials. Idempotent (`-C` before `-I`). IPv6 only where the
    tool exists. Returns what it changed."""
    changed = []
    for tool, dest in (("iptables", IMDS_V4), ("ip6tables", IMDS_V6)):
        rule = ["OUTPUT", "-d", dest, "-m", "owner", "!", "--uid-owner", "0", "-j", "REJECT"]
        if tool == "ip6tables" and not shutil.which(tool):
            continue
        try:
            if run([tool, "-C", *rule], capture_output=True).returncode == 0:
                continue
            res = run([tool, "-I", *rule[:1], "1", *rule[1:]], capture_output=True)
        except OSError as exc:
            if tool == "iptables":
                raise RuntimeError(f"cannot install the IMDS rule: {exc}") from exc
            continue
        if res.returncode != 0 and tool == "iptables":
            raise RuntimeError(f"iptables refused the IMDS rule: {res.stderr!r}")
        changed.append(tool)
    return changed


def setup_units(box: dict, *, run=subprocess.run, chown=os.chown, uid_of=None,
                etc: pathlib.Path | None = None, role_name: str = "") -> list[dict]:
    """ROOT, on every start (`cloud_runner.py --setup-units`): make each unit's user,
    home, env file, gog secret and tmpfs root; write `units.json` for the broker and
    `canopy-pair`; install the IMDS rule. Idempotent. Returns the units, with uids."""
    import pwd

    etc = etc or UNIT_ETC
    uid_of = uid_of or (lambda user: pwd.getpwnam(user))
    units = parse_units(box.get("RUNNER_UNITS", ""), workspace=box.get("RUNNER_WORKSPACE", ""),
                        box=box.get("RUNNER_NAME", ""))
    _mkdir_owned(etc, uid=0, gid=0, mode=0o755, chown=chown)
    for unit in units:
        if run(["id", "-u", unit["user"]], capture_output=True).returncode != 0:
            res = run(["useradd", "--create-home", "--home-dir", unit["home"], "--shell", "/bin/bash",
                       "--comment", f"canopy isolation unit {unit['id']}", unit["user"]],
                      capture_output=True)
            if res.returncode != 0:
                raise RuntimeError(f"useradd {unit['user']} failed: {res.stderr!r}")
        pw = uid_of(unit["user"])
        uid, gid = pw.pw_uid, pw.pw_gid
        unit["uid"], unit["gid"] = uid, gid
        home = pathlib.Path(unit["home"])
        _mkdir_owned(home, uid=uid, gid=gid, mode=0o700, chown=chown)
        for sub in ("work", "agents", ".canopy-runner"):
            _mkdir_owned(home / sub, uid=uid, gid=gid, mode=0o700, chown=chown)
        env = unit_env(unit, units, box, uid=uid)
        _write_owned(etc / f"{unit['id']}.env", _env_text(env).encode(), uid=uid, gid=gid, mode=0o600,
                     chown=chown)
        secret = etc / f"{unit['id']}.gog-secret"
        if not secret.exists():
            # 32 random bytes, never rotated by a restart: every agent's gog keyring
            # password in this unit is derived from it (`gog_env`).
            _write_owned(secret, secrets.token_bytes(32), uid=uid, gid=gid, mode=0o400, chown=chown)
        else:
            os.chmod(secret, 0o400)
            chown(str(secret), uid, gid)
        root = pathlib.Path(env["CANOPY_TURN_ENV_ROOT"])
        _mkdir_owned(root.parent, uid=0, gid=0, mode=0o755, chown=chown)
        _mkdir_owned(root, uid=uid, gid=gid, mode=0o700, chown=chown)
        unit["kms_key"] = unit_kms_key(unit, box.get("RUNNER_NAME", ""), role_name) if role_name else ""
    doc = {"box": box.get("RUNNER_NAME", ""), "role_name": role_name, "units": units}
    _write_owned(etc / "units.json", json.dumps(doc, indent=1).encode(), uid=0, gid=0, mode=0o644,
                 chown=chown)
    return units


def load_units(path: pathlib.Path | None = None) -> dict:
    try:
        return json.loads((path or UNITS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _role_name() -> str:
    """The instance role's NAME (root only: IMDS is closed to units)."""
    try:
        token = _imds("/latest/api/token", put=True, headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
        names = _imds("/latest/meta-data/iam/security-credentials/", token=token).split()
        return names[0] if names else ""
    except Exception:  # noqa: BLE001
        return ""


def _as_unit(unit: dict, argv: list[str], *, run=subprocess.run, env: dict | None = None,
             timeout: float = 300) -> subprocess.CompletedProcess:
    """Run `argv` AS the unit's user, in its environment (`runuser`, root only)."""
    base = read_env_file(UNIT_ETC / f"{unit['id']}.env")
    pairs = [f"{k}={v}" for k, v in {**base, **(env or {})}.items()]
    return run(["runuser", "-u", unit["user"], "--", "env", "-i", *pairs, *argv],
               capture_output=True, text=True, timeout=timeout)


#: How a unit gets the canopy CLI before it is paired. canopy is a public repo, so
#: this needs no token — which is what lets a fresh box make its keys at all.
CANOPY_CLI_URL = os.environ.get("CANOPY_PLUGIN_URL", "https://github.com/dimagi-internal/canopy.git")
_ENSURE_CLI = (
    'command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; '
    'command -v canopy >/dev/null 2>&1 || "$HOME/.local/bin/uv" tool install --force "git+$CANOPY_CLI_URL" '
    '>/dev/null 2>&1; command -v canopy')


def _unit_keys(unit: dict, *, run=subprocess.run) -> dict:
    """The unit's PUBLIC keys, made by the canopy CLI as the unit: its software signing
    key (`--public-jwk --kind software`) and its broker-held ECDH key (`--ecdh-jwk`).
    `{public_jwk, jkt, ecdh_public_jwk?}`; raises RuntimeError without a signing key."""
    res = _as_unit(unit, ["bash", "-c", _ENSURE_CLI], run=run, env={"CANOPY_CLI_URL": CANOPY_CLI_URL},
                   timeout=900)
    if res.returncode != 0:
        raise RuntimeError(f"{unit['id']}: no canopy CLI ({(res.stderr or '').strip()[-200:]})")
    out: dict = {}
    res = _as_unit(unit, ["canopy", "vault", "device", "--public-jwk", "--kind", "software"], run=run)
    try:
        doc = json.loads((res.stdout or "").strip().splitlines()[-1])
        out["public_jwk"], out["jkt"] = doc["jwk"], str(doc.get("jkt") or "")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{unit['id']}: canopy made no signing key "
                           f"({(res.stderr or res.stdout or '').strip()[-200:]})") from exc
    res = _as_unit(unit, ["canopy", "vault", "device", "--ecdh-jwk", "--kind", "software"], run=run)
    try:
        doc = json.loads((res.stdout or "").strip().splitlines()[-1])
        if set(doc["jwk"]) == {"kty", "crv", "x", "y"}:
            out["ecdh_public_jwk"] = doc["jwk"]
    except Exception:  # noqa: BLE001 — no ECDH key yet (no KMS key for this unit): it waits
        _log(f"pair-units: {unit['id']} has no ECDH key from the broker yet — its zero-knowledge "
             "agents wait until its <stack>-ecdh key exists")
    return out


def _unit_state(unit: dict) -> dict:
    try:
        return json.loads((pathlib.Path(unit["home"]) / ".canopy-cloud-runner.json").read_text())
    except (OSError, ValueError):
        return {}


def pair_units_payload(box: str, units: list[dict], keys: dict, proof: str,
                       unit_envs: dict | None = None) -> dict:
    """The ONE pair-by-role body for every unit (spec §2): the box's name, one role
    proof, and each unit's name, public keys and declared capabilities."""
    out = []
    for unit in units:
        k = keys[unit["id"]]
        uenv = (unit_envs or {}).get(unit["id"]) or read_env_file(UNIT_ETC / f"{unit['id']}.env")
        caps: dict = {}
        if uenv.get("RUNNER_SESSIONS", "1").strip().lower() not in ("0", "false", "no", ""):
            caps["sessions"] = True
        if uenv.get("RUNNER_DIRECTED", "0").strip().lower() not in ("0", "false", "no", ""):
            caps["directed"] = True
        if unit["kind"] == "agent":
            caps["unit"] = {"kind": "agent", "slug": unit["slug"]}
        entry = {"name": unit["name"], "public_jwk": k["public_jwk"], "host": unit["name"],
                 "capabilities": caps}
        if k.get("ecdh_public_jwk"):
            entry["ecdh_public_jwk"], entry["ecdh_store"] = k["ecdh_public_jwk"], "kms-broker"
        out.append(entry)
    return {"name": box, "kind": "cloud", "proof": proof, "units": out}


def write_unit_states(units: list[dict], reply: dict, keys: dict, *, run=subprocess.run,
                      chown=os.chown) -> None:
    """Each unit's state file (owned by the unit), and canopy's record that its key is
    registered (`canopy vault device --registered`), so its DPoP proofs are sent."""
    ids, key_ids = reply.get("units") or {}, reply.get("device_keys") or {}
    for unit in units:
        rid = ids.get(unit["name"])
        if not rid:
            continue
        state = {"runner_id": rid, "paired_by": "invitation", "unit": unit["id"],
                 "jkt": keys[unit["id"]].get("jkt", "")}
        _write_owned(pathlib.Path(unit["home"]) / ".canopy-cloud-runner.json",
                     json.dumps(state).encode(), uid=unit.get("uid", 0), gid=unit.get("gid", 0),
                     mode=0o600, chown=chown)
        if key_ids.get(unit["name"]):
            _as_unit(unit, ["canopy", "vault", "device", "--registered", str(key_ids[unit["name"]]),
                            "--runner", str(rid), "--status", "active"], run=run)


def pair_units_main(*, run=subprocess.run, post=None, sleep=time.sleep, chown=os.chown) -> int:
    """ROOT (`canopy-pair.service`, `cloud_runner.py --pair-units`): one `pair-by-role`
    call for every unit on the box, under the box's ONE approved invitation. Units
    already paired with the keys they hold are left alone; a replaced instance (new
    keys, same role and names) re-attaches and keeps its runner ids."""
    doc = load_units()
    units = doc.get("units") or []
    box = doc.get("box") or RUNNER_NAME
    if not units:
        _log("pair-units: no units.json — run --setup-units first")
        return 1
    keys = {u["id"]: _unit_keys(u, run=run) for u in units}
    if all(_unit_state(u).get("runner_id") and _unit_state(u).get("jkt") == keys[u["id"]]["jkt"]
           for u in units):
        _log("pair-units: every unit is paired with the key it holds — nothing to do")
        return 0
    url = BASE_URL + PAIR_BY_ROLE_PATH
    post = post or _post_unauthenticated
    while not _stop:
        creds = _aws_instance_credentials()
        if not creds:
            _log("pair-units: no instance role (this must run as root on the EC2 box)")
            return 1
        status, reply = post(url, pair_units_payload(box, units, keys, aws_proof(url, creds=creds)))
        if status in (200, 201) and isinstance(reply, dict) and reply.get("units"):
            write_unit_states(units, reply, keys, run=run, chown=chown)
            _log(f"pair-units: {'paired' if status == 201 else 're-attached'} "
                 + ", ".join(f"{n}={r}" for n, r in reply["units"].items()))
            return 0
        if status == 202:
            _log(f"pair-units: waiting for the invitation for {box} to be approved")
        else:
            _log(f"pair-units: HTTP {status} — no approved invitation lists these units yet "
                 f"(./up.sh --units …); asking again in {POLL_SECONDS}s")
        sleep(POLL_SECONDS)
    return 1


# ── systemd for a box of units (`cloud_runner.py --install-units`, root) ─────
#
# The unit files live HERE, not in runner.cfn.yaml's UserData: UserData is capped at
# 16 KB, and cloud-init writes it only on an instance's first boot — so a box built
# before units could never be moved in place. `--install-units` is the one path for
# both: cloud-init runs it on a new box, and an operator runs it (with Jonathan's
# approval) to move a live box to units (runner/ec2/README.md "Moving a box to units").
SYSTEMD_DIR = pathlib.Path(os.environ.get("CANOPY_SYSTEMD_DIR", "/etc/systemd/system"))
RUNNER_PY = "/opt/canopy-runner/cloud_runner.py"

SYSTEMD_UNITS = {
    # Every start: re-read the stack's parameters, then (re)make every unit's user,
    # files and secrets and the IMDS rule — iptables does not survive a reboot.
    "canopy-units.service": f"""[Unit]
Description=Canopy isolation units: users, env files, secrets, the IMDS rule
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/canopy-fetch-env
ExecStart=/usr/bin/python3 {RUNNER_PY} --setup-units
[Install]
WantedBy=multi-user.target
""",
    "canopy-kms-broker.service": """[Unit]
Description=Canopy KMS broker: each unit's zero-knowledge ECDH key, for that unit only
After=canopy-units.service
Requires=canopy-units.service
[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/canopy-runner/kms_broker.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
""",
    # Pairs every unit in ONE pair-by-role call; waits for the invitation's approval.
    "canopy-pair.service": f"""[Unit]
Description=Canopy pairing: every isolation unit on this box, one approval
After=canopy-kms-broker.service network-online.target
Requires=canopy-units.service
[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=infinity
EnvironmentFile=-/etc/canopy-runner/box.env
ExecStart=/usr/bin/python3 {RUNNER_PY} --pair-units
Restart=on-failure
RestartSec=30
[Install]
WantedBy=multi-user.target
""",
    # One runner per unit, as that unit's user. No sudo, no instance role (IMDS is
    # root's), its own /tmp.
    "canopy-runner@.service": f"""[Unit]
Description=Canopy runner, isolation unit %i
After=network-online.target canopy-units.service canopy-kms-broker.service
Wants=network-online.target
Requires=canopy-units.service
[Service]
Type=simple
User=cu-%i
EnvironmentFile=/etc/canopy-runner/%i.env
WorkingDirectory=/home/cu-%i
ExecStart=/usr/bin/python3 {RUNNER_PY}
Restart=on-failure
RestartSec=10
OOMPolicy=continue
NoNewPrivileges=yes
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
""",
    # The auto-updater, as ROOT on a box of units: one install, every unit restarted.
    "canopy-runner-update.service": """[Unit]
Description=Canopy cloud runner auto-update (every isolation unit)
[Service]
Type=oneshot
User=root
Environment=HOME=/root
Environment=UNITS_FILE=/etc/canopy-runner/units.json
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/local/bin/canopy-runner-update
""",
}


def _git_root(*args: str, run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["git", "-c", "safe.directory=*", "-C", CANOPY_WEB_REPO_DIR, *args],
               capture_output=True, text=True, timeout=300)


def install_broker(*, run=subprocess.run) -> bool:
    """`/opt/canopy-runner/kms_broker.py` at the sha this runner was installed from
    (build-info), from the root-owned canopy-web clone (a public repo: no token)."""
    target = RUNNER_HOME / "kms_broker.py"
    repo = pathlib.Path(CANOPY_WEB_REPO_DIR)
    if not (repo / ".git").is_dir():
        run(["git", "clone", "--quiet", CANOPY_WEB_REPO_URL, str(repo)], capture_output=True, timeout=600)
    sha = build_info().get("sha") or ""
    if sha:
        _git_root("fetch", "--quiet", "origin", sha, run=run)
    _git_root("fetch", "--quiet", "origin", "main", run=run)
    for ref in ([sha] if sha else []) + ["origin/main"]:
        res = _git_root("show", f"{ref}:runner/ec2/kms_broker.py", run=run)
        if res.returncode == 0 and res.stdout.strip():
            target.write_text(res.stdout)
            os.chmod(target, 0o755)
            return True
    return False


#: The stack parameters a box of units needs (never a secret), as the legacy
#: runner.env names them — what an in-place move carries over into box.env.
BOX_ENV_KEYS = ("CANOPY_BASE_URL", "RUNNER_PROJECTS", "RUNNER_AGENTS", "RUNNER_SESSIONS", "RUNNER_DIRECTED",
                "RUNNER_EXECUTOR", "RUNNER_WORKSPACE", "RUNNER_NAME", "AGENT_SLUGS", "RUNNER_UNITS")
_IN_PLACE_FETCH_ENV = """#!/bin/sh
# Replaced by `cloud_runner.py --install-units` when this box moved to isolation
# units in place: the old script re-wrote /opt/canopy-runner/runner.env (a PAT, the
# Claude token, the box-wide gog password) on every start. A box of units reads its
# parameters from /etc/canopy-runner/box.env, which holds no secret.
exit 0
"""


def write_box_env_from_legacy(*, units_spec: str, legacy: pathlib.Path | None = None,
                              out: pathlib.Path | None = None) -> dict:
    """An in-place move: box.env from the legacy runner.env's NON-secret keys, plus
    the units the operator asked for. Leaves an existing box.env alone."""
    out = out or BOX_ENV_FILE
    if out.exists():
        return read_env_file(out)
    old = read_env_file(legacy or RUNNER_HOME / "runner.env")
    box = {k: old[k] for k in BOX_ENV_KEYS if k in old}
    box["RUNNER_UNITS"] = units_spec or box.get("RUNNER_UNITS", "")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(_env_text(box))
    return box


def install_units(*, run=subprocess.run, systemd: pathlib.Path | None = None,
                  fetch_env: pathlib.Path = pathlib.Path("/usr/local/bin/canopy-fetch-env"),
                  sudoers: pathlib.Path = pathlib.Path("/etc/sudoers.d/canopy-runner-update")) -> list[str]:
    """ROOT: the systemd units for a box of isolation units, the legacy single runner
    retired, root ownership of the shared code, and everything enabled and started.
    Idempotent. Returns the runner services it enabled.

    On a box built before units (an in-place move), box.env is made from the legacy
    runner.env's non-secret keys and `$RUNNER_UNITS`, and the legacy fetch-env that
    re-wrote runner.env's secrets on every start is replaced."""
    systemd = systemd or SYSTEMD_DIR
    write_box_env_from_legacy(units_spec=os.environ.get("RUNNER_UNITS", ""))
    try:
        if "runner.env" in fetch_env.read_text() and "box.env" not in fetch_env.read_text():
            fetch_env.write_text(_IN_PLACE_FETCH_ENV)
    except OSError:
        pass
    for name, text in SYSTEMD_UNITS.items():
        (systemd / name).write_text(text)
    # The shared, read-only parts: root's, so no unit can rewrite what the others run.
    home = RUNNER_HOME
    # The legacy single runner's credentials go first: they must never become
    # readable by a unit (its runner.env held a PAT, the Claude token and the
    # box-wide gog password; runner-token.json its runner token).
    for stale in ("runner.env", "runner-token.json", "runner-token.json.tmp", "in-flight", "turn-env-root"):
        try:
            (home / stale).unlink()
        except OSError:
            pass
    for path in (home, pathlib.Path(CANOPY_WEB_REPO_DIR)):
        if path.exists():
            run(["chown", "-R", "root:root", str(path)], capture_output=True)
    run(["chmod", "-R", "go+rX,go-w", CANOPY_WEB_REPO_DIR], capture_output=True)   # a public repo
    for name, mode in (("cloud_runner.py", 0o755), ("kms_broker.py", 0o755), ("build-info.json", 0o644)):
        try:
            os.chmod(home / name, mode)
        except OSError:
            pass
    try:
        os.chmod(home, 0o755)
    except OSError:
        pass
    # The legacy single runner (User=ubuntu, the box-wide runner.env) stops here;
    # its runner row is adopted by the unit that keeps the box's name.
    run(["systemctl", "disable", "--now", "canopy-runner.service"], capture_output=True)
    for stale in (sudoers,):
        try:
            stale.unlink()
        except OSError:
            pass
    install_broker(run=run)
    run(["systemctl", "daemon-reload"], capture_output=True)
    run(["systemctl", "enable", "--now", "canopy-units.service"], capture_output=True)
    units = load_units().get("units") or []
    services = [f"canopy-runner@{u['id']}.service" for u in units]
    run(["systemctl", "enable", "--now", "canopy-kms-broker.service", "canopy-pair.service",
         "canopy-runner-update.timer", *services], capture_output=True)
    return services


def _wait_for_unit_state() -> str:
    """A unit never pairs itself (it has no role to prove): it waits for root's
    `canopy-pair` to write its state file."""
    said = False
    while not _stop:
        try:
            rid = json.loads(STATE_FILE.read_text()).get("runner_id")
        except (OSError, ValueError):
            rid = ""
        if rid:
            return str(rid)
        if not said:
            _log(f"unit {UNIT}: not paired yet — waiting for canopy-pair to write {STATE_FILE}")
            said = True
        time.sleep(POLL_SECONDS)
    return ""


def _exchange_unit_proof(runner_id: str) -> tuple[str, float]:
    """A unit's runner token, bought with ITS software key: `canopy vault runner-session`
    signs the DPoP proof (this file has no EC crypto). ("", 0) on any failure."""
    try:
        res = subprocess.run([_canopy_cli(), "vault", "runner-session", "--runner", runner_id,
                              "--base-url", BASE_URL], capture_output=True, text=True, timeout=60,
                             env=_child_safe_env())
    except Exception as exc:  # noqa: BLE001
        _log(f"unit {UNIT}: could not run canopy vault runner-session ({exc})")
        return "", 0.0
    if res.returncode != 0:
        _log(f"unit {UNIT}: canopy-web refused this unit's device proof — "
             f"{' '.join((res.stderr or res.stdout or '').split())[-200:]}")
        return "", 0.0
    try:
        body = json.loads((res.stdout or "").strip().splitlines()[-1])
        exp = datetime.datetime.fromisoformat(
            str(body.get("expires_at", "")).replace("Z", "+00:00")).timestamp()
    except (ValueError, IndexError, AttributeError):
        return "", 0.0
    return str(body.get("access_token") or ""), exp


# ── gog: one home and one keyring password per agent (spec §6) ──────────────
GOG_SECRET_ENV = "CANOPY_GOG_SECRET_FILE"


def hkdf_sha256(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    """RFC 5869 HKDF-SHA256 (empty salt = HashLen zero bytes), stdlib only."""
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def gog_password(secret: bytes, slug: str) -> str:
    """An agent's gog keyring password: HKDF-SHA256(unit secret, info=b"gog:"+slug), hex
    — canopy's `gog_home` derives the same, so either side can open the keyring."""
    return hkdf_sha256(secret, b"gog:" + slug.encode()).hex()


def _gog_secret() -> bytes | None:
    """The unit secret's bytes; None when no secret is configured (a box before units:
    today's box-wide GOG_* in this process's env stand) — and b"" when one IS
    configured but cannot be read, which must give nobody a password."""
    path = os.environ.get(GOG_SECRET_ENV, "").strip()
    if not path:
        return None
    try:
        return pathlib.Path(path).read_bytes()
    except OSError:
        return b""


def gog_env(slug: str) -> dict:
    """`slug`'s own gog home and keyring password, and nothing of any other agent's.
    {} when there is no unit secret (legacy: the process env's GOG_* apply) or it is
    unreadable."""
    secret = _gog_secret()
    if not secret or not slug:
        return {}
    return {"GOG_HOME": str(pathlib.Path.home() / ".gog" / slug), "GOG_KEYRING_BACKEND": "file",
            "GOG_KEYRING_PASSWORD": gog_password(secret, slug)}


def _gog_runner(slug: str):
    """`subprocess.run` with `slug`'s gog env, for the inbox poller's gog calls."""
    extra = gog_env(slug)
    if not extra:
        return subprocess.run

    def run(argv, **kw):
        env = {k: v for k, v in (kw.pop("env", None) or os.environ).items()
               if k not in ("GOG_HOME", "GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND")}
        return subprocess.run(argv, env={**env, **extra}, **kw)

    return run


# ── secrets off disk: each turn's env on tmpfs, gone when it ends (spec §4) ──
#: The canopy CLI that reads `CANOPY_AGENT_ENV_FILE` first and can resolve in memory
#: (`cred env --print-json/--out`, prepare's off-disk mode): canopy PR #999.
OFF_DISK_MIN_CLI = (0, 2, 736)
_CLI_VERSION: dict = {"at": 0.0, "v": None}
CLI_VERSION_TTL_SECONDS = 600.0


def _canopy_cli_version(*, now: float | None = None) -> tuple | None:
    """The installed canopy CLI's version as a tuple, cached; None when it cannot say."""
    now = time.time() if now is None else now
    if _CLI_VERSION["v"] is not None and now - _CLI_VERSION["at"] < CLI_VERSION_TTL_SECONDS:
        return _CLI_VERSION["v"] or None
    v = _version_of([_canopy_cli(), "--version"])
    parsed = tuple(int(x) for x in v.split(".")) if v else ()
    _CLI_VERSION.update(at=now, v=parsed)
    return parsed or None


def _forget_cli_version() -> None:
    _CLI_VERSION.update(at=0.0, v=None)


def _turn_env_root(*, create: bool = True) -> pathlib.Path | None:
    """Where this runner writes per-turn env files: `$CANOPY_TURN_ENV_ROOT` (a unit's
    `/run/canopy/<uid>`), else `/run/canopy/<uid>` when it exists and is ours, else a
    0700 `/dev/shm/canopy-<uid>` (tmpfs, which a box built before units can still
    make). None when none is usable: the turn then runs the legacy way."""
    uid = os.getuid()
    env = os.environ.get("CANOPY_TURN_ENV_ROOT", "").strip()
    candidates = [pathlib.Path(env)] if env else [TURN_ENV_BASE / str(uid),
                                                  pathlib.Path("/dev/shm") / f"canopy-{uid}"]
    for root in candidates:
        try:
            if not root.exists():
                if not create or not root.parent.is_dir():
                    continue
                root.mkdir(mode=0o700)
            st = os.lstat(root)
            if not os.path.isdir(root) or os.path.islink(root) or st.st_uid != uid:
                continue
            os.chmod(root, 0o700)
            return root
        except OSError:
            continue
    return None


def secrets_off_disk() -> bool:
    """Do this runner's turns get their env on tmpfs (and no `~/.<slug>/.env`)?

    By default only on an isolation unit (`CANOPY_UNIT`, or a box with units.json): a
    box built before units keeps its files until an operator opts it in with
    `RUNNER_SECRET_FILES=0` — a new runner deploying must never change how a live box
    hands its agents their secrets. `RUNNER_SECRET_FILES=1` keeps the files anywhere.
    And only when the installed canopy CLI can read `CANOPY_AGENT_ENV_FILE` — on an
    older CLI a turn's PAT reads would find nothing, so the legacy files stay and
    health says why (Review Focus 5)."""
    choice = os.environ.get("RUNNER_SECRET_FILES", "").strip().lower()
    if choice in ("1", "true", "yes"):
        _set_check("secrets", "warn", "RUNNER_SECRET_FILES=1: agent secrets are kept in "
                   "~/.<agent>/.env files on purpose")
        return False
    if choice not in ("0", "false", "no") and not (UNIT or UNITS_FILE.exists()):
        _set_check("secrets", "ok", "agent secrets are in ~/.<agent>/.env files (a box without "
                   "isolation units; RUNNER_SECRET_FILES=0 moves them off disk)")
        return False
    v = _canopy_cli_version()
    want = ".".join(map(str, OFF_DISK_MIN_CLI))
    if not v or v < OFF_DISK_MIN_CLI:
        _set_check("secrets", "warn", f"canopy CLI {'.'.join(map(str, v)) if v else '(unknown)'} is "
                   f"older than {want}: agent secrets are still written to ~/.<agent>/.env — "
                   "update the CLI (a bootstrap does)")
        return False
    root = _turn_env_root()
    if root is None:
        _set_check("secrets", "warn", "no tmpfs directory for per-turn env files: agent secrets "
                   "are still written to ~/.<agent>/.env")
        return False
    _set_check("secrets", "ok", f"agent secrets stay off disk: each turn's env is under {root} "
               "and removed when it ends")
    return True


#: slug -> (fetched_at, {NAME: value}) from `canopy cred env --print-json`. The same
#: 5-minute TTL `cred sync` had, so a claim is not a 1Password read.
_TURN_VALUES: dict = {}
_TURN_VALUES_LOCK = threading.Lock()
#: Fields of /credentials/resolve the runner needs beside the env values, taken
#: from `canopy cred env --runner --print-json` when it includes them (top-level
#: keys, same names as the resolve response). canopy 0.2.736 prints only
#: {agent, source, values}; until it adds these, a unit's own (proofless) resolve
#: stands, which a device-bound or zero-knowledge agent refuses.
RESOLVE_EXTRAS = ("op_sa_token", "op_vault", "salesforce_creds")
#: slug -> why its env could not be had (the turn's failure note says it).
_TURN_ENV_ERRORS: dict = {}


def _off_disk_marker(slug: str) -> pathlib.Path:
    return pathlib.Path.home() / ".canopy-runner" / "off-disk-ok" / slug


def off_disk_proven(slug: str) -> bool:
    """Has a turn of `slug` run successfully on its tmpfs env? Only then are its
    legacy env files removed: until then they are the way back."""
    return bool(slug) and _off_disk_marker(slug).exists()


def note_off_disk_success(slug: str) -> None:
    """The first good off-disk turn of `slug`: record it, then remove its legacy files."""
    if not slug or off_disk_proven(slug):
        return
    try:
        marker = _off_disk_marker(slug)
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text(str(int(time.time())))
    except OSError as exc:
        _log(f"warn: {slug}: could not record its first off-disk turn ({exc}); legacy files kept")
        return
    remove_legacy_secret_files([slug])


def _turn_values(slug: str, *, now: float | None = None) -> dict | None:
    """`slug`'s env values, resolved in memory by the canopy CLI as this runner, cached
    for `_AGENT_ENV_TTL_SECONDS`. On a failed refresh the last good values are kept
    (the vault, not this box, is the source; a blip must not cost the turn)."""
    now = time.time() if now is None else now
    with _TURN_VALUES_LOCK:
        hit = _TURN_VALUES.get(slug)
        if hit and now - hit[0] < _AGENT_ENV_TTL_SECONDS:
            return hit[1]
    if not _has_env_source(slug):
        # canopy's `_env_in_memory` "n/a": no template and no canopy-web values. An
        # agent with nothing to resolve has an EMPTY env, not a failed one.
        with _TURN_VALUES_LOCK:
            _TURN_VALUES[slug] = (now, {})
        _set_check(f"env.{slug}", "ok", "no env to resolve (no .env.tpl, not canopy-web-sourced)")
        return {}
    env = {k: v for k, v in _child_safe_env().items()
           if k not in _OP_PERSONAL_ENV_KEYS and not k.startswith(_OP_PERSONAL_ENV_PREFIXES)}
    env.update(CANOPY_TOKEN=_bearer(min_life=300), CANOPY_BASE_URL=BASE_URL, AGENT_ROOT=AGENT_ROOT)
    turn_id = str((getattr(_TURN_ENV, "extra", None) or {}).get("CANOPY_TURN_ID") or "")
    if turn_id:
        env["CANOPY_TURN_ID"] = turn_id
    try:
        res = subprocess.run([_canopy_cli(), "cred", "env", "--slug", slug, "--runner",
                              "--agent-root", AGENT_ROOT, "--print-json"], env=env,
                             capture_output=True, text=True, timeout=_AGENT_ENV_TIMEOUT_SECONDS + 200)
        doc = json.loads((res.stdout or "").strip().splitlines()[-1]) if res.returncode == 0 else None
        values = doc.get("values") if isinstance(doc, dict) else None
        if not isinstance(values, dict):
            raise RuntimeError(" ".join((res.stderr or "").split())[-300:] or f"exit {res.returncode}")
    except Exception as exc:  # noqa: BLE001
        _TURN_ENV_ERRORS[slug] = " ".join(str(exc).split())[:300]
        _set_check(f"env.{slug}", "warn", f"could not resolve {slug}'s env in memory: {exc}")
        _log(f"warn: {slug}: could not resolve its env ({exc}); "
             f"{'using the last good values' if hit else 'the turn runs without it'}")
        with _TURN_VALUES_LOCK:
            if hit:
                _TURN_VALUES[slug] = (now, hit[1])
        return hit[1] if hit else None
    values = {str(k): str(v) for k, v in values.items()}
    _TURN_ENV_ERRORS.pop(slug, None)
    note_cred_success(slug)
    # The resolve-time extras, when the CLI hands them over (`RESOLVE_EXTRAS`): it
    # resolved WITH this runner's device proof (and unwrapped a zero-knowledge
    # vault), which a unit's own resolve cannot attach.
    extras = {k: str(doc.get(k) or "") for k in RESOLVE_EXTRAS if k in doc}
    if extras:
        _AGENT_OP[slug] = (now, extras.get("op_sa_token", ""))
        if "op_vault" in extras:
            _AGENT_VAULT[slug] = extras["op_vault"].strip()
        if "salesforce_creds" in extras:
            _AGENT_SF[slug] = extras["salesforce_creds"]
    with _TURN_VALUES_LOCK:
        _TURN_VALUES[slug] = (now, values)
    _set_check(f"env.{slug}", "ok", f"{len(values)} var(s) from {doc.get('source') or 'canopy'}, "
               "held in memory")
    return values


def _has_env_source(slug: str) -> bool:
    """Does `slug` have an env to resolve: a `.env.tpl` in its clone, or canopy-web as
    its credential source? Unknown (no clone yet, canopy-web unreachable) counts as
    YES — a resolve that then fails fails the turn, rather than running it bare."""
    clone = pathlib.Path(AGENT_ROOT) / slug
    if (clone / ".env.tpl").is_file() or not (clone / ".git").is_dir():
        return True
    status, body = _api("GET", f"/{slug}/credentials/access", prefix="/api/agents")
    if status != 200 or not isinstance(body, dict):
        return True
    return str(body.get("credential_source") or "") == "canopy-web"


# ── backing off an agent whose credentials this box cannot resolve ──────────
#: A turn that fails "could not resolve <slug>'s credentials" goes back to the queue
#: (a sessionless failure is a non-attempt). Claimed straight back by this box it
#: would spend the server's MAX_SESSIONLESS_RETRIES in a minute; so the box stops
#: claiming that agent for a growing interval, reset by the next good resolve. It is
#: the claim's `paused` list (the per-agent local pause), so the turn simply waits.
CRED_BACKOFF_STEPS = (60, 120, 300, 600)
_CRED_BACKOFF: dict = {}   # slug -> (consecutive failures, not before epoch)


def note_cred_failure(slug: str, *, now: float | None = None) -> float:
    """Back `slug` off; returns until when."""
    now = time.time() if now is None else now
    n = _CRED_BACKOFF.get(slug, (0, 0.0))[0] + 1
    until = now + CRED_BACKOFF_STEPS[min(n, len(CRED_BACKOFF_STEPS)) - 1]
    _CRED_BACKOFF[slug] = (n, until)
    return until


def note_cred_success(slug: str) -> None:
    _CRED_BACKOFF.pop(slug, None)


def backed_off_slugs(*, now: float | None = None) -> list[str]:
    """The agents this box will not claim right now (credential-resolve backoff)."""
    now = time.time() if now is None else now
    return sorted(s for s, (_n, until) in _CRED_BACKOFF.items() if until > now)


def _turn_env_dir(turn_id: str, *, create: bool = True) -> pathlib.Path | None:
    root = _turn_env_root(create=create)
    if root is None or not re.fullmatch(r"[0-9a-zA-Z-]{8,64}", str(turn_id)):
        return None
    return root / str(turn_id)


def write_turn_env(slug: str, turn_id: str) -> pathlib.Path | None:
    """`<root>/<turn>/env` (dir 0700, file 0600) holding `slug`'s env for ONE turn.
    The turn gets its path as `CANOPY_AGENT_ENV_FILE`; `remove_turn_env` deletes it in
    the turn's `finally`. None when it cannot be written (the turn still has the
    values in its environment)."""
    d = _turn_env_dir(turn_id)
    values = _turn_values(slug)
    if d is None or values is None:
        return None
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        path = d / "env"
        body = f"# {slug} credentials for one turn ({turn_id[:8]}), removed when it ends.\n" + \
            "".join(f"{k}={v}\n" for k, v in values.items()
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and "\n" not in v)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o600)
        return path
    except OSError as exc:
        _TURN_ENV_ERRORS[slug] = str(exc)[:300]
        _log(f"warn: {slug}: could not write this turn's env file ({exc})")
        remove_turn_env(turn_id)
        return None


def remove_turn_env(turn_id: str) -> None:
    """Delete a turn's env dir — however the turn ended. Never raises."""
    try:
        d = _turn_env_dir(turn_id, create=False)
    except Exception:  # noqa: BLE001
        return
    if d is not None:
        shutil.rmtree(d, ignore_errors=True)


def sweep_turn_envs() -> int:
    """At start: remove every per-turn env a previous process left (a crash, a
    kill -9). No turn is running yet, so nothing here is anyone's."""
    root = _turn_env_root(create=False)
    if root is None:
        return 0
    n = 0
    for child in list(root.iterdir()) if root.is_dir() else []:
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            n += 1
        except OSError:
            continue
    if n:
        _log(f"secrets: swept {n} leftover per-turn env(s) from {root}")
    return n


def remove_legacy_secret_files(slugs) -> list[str]:
    """Once turns get their env off disk, the copies the old path wrote are only a
    liability: `~/.<slug>/.env`, `<AGENT_ROOT>/<slug>/.env`, `.gws-sa-key.json`."""
    removed = []
    for slug in sorted({s for s in slugs if s and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", s)}):
        for path in (pathlib.Path.home() / f".{slug}" / ".env",
                     pathlib.Path.home() / f".{slug}" / ".gws-sa-key.json",
                     pathlib.Path(AGENT_ROOT) / slug / ".env",
                     pathlib.Path(AGENT_ROOT) / slug / ".gws-sa-key.json"):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    removed.append(str(path))
            except OSError:
                continue
    if removed:
        _log(f"secrets: removed {len(removed)} legacy secret file(s): {', '.join(removed)}")
    return removed


def _known_agent_slugs() -> list[str]:
    slugs = {s.strip() for s in AGENT_SLUGS.split(",") if s.strip()}
    try:
        slugs |= {p.name for p in pathlib.Path(AGENT_ROOT).iterdir() if p.is_dir()}
    except OSError:
        pass
    return sorted(slugs)


def _agent_op_token(slug: str) -> str:
    """THIS agent's 1Password key, or "" — never another agent's, never a
    box-wide one. canopy-web is the custodian (`Agent.op_sa_token_enc`)."""
    hit = _AGENT_OP.get(slug)
    now = time.time()
    if hit and now - hit[0] < _AGENT_OP_TTL_SECONDS:
        return hit[1]
    token = ""
    # The turn this resolve is for, so canopy-web's read log names it (Canopy Vault,
    # canopy#946). Known when called from a turn's thread; an older canopy-web ignores it.
    turn_id = str((getattr(_TURN_ENV, "extra", None) or {}).get("CANOPY_TURN_ID") or "")
    # And this box's IAM-role device proof (canopy-web#1549) — required only for an
    # agent with require_device_binding; ignored by canopy-web otherwise.
    proof = _aws_proof_headers(f"{BASE_URL}/api/agents/{slug}/credentials/resolve")
    try:
        status, payload = _api("GET", f"/{slug}/credentials/resolve", prefix="/api/agents",
                               headers={"X-Canopy-Turn-Id": turn_id, **proof})
        if status == 409 and isinstance(payload, dict) and payload.get("error") == "zk_wrap_missing":
            # A zero-knowledge agent (canopy-web#1550) whose vault key was never wrapped to
            # this box's ECDH key: nothing here can read its values until an admin grants it.
            _log(f"{slug}: zero-knowledge vault — this box has no wrap of its key; ask an "
                 f"admin to grant this runner's device ('Grant pending' on {slug}'s page)")
            _set_check(f"zk.{slug}", "fail", "zero-knowledge vault: this device is not "
                       "granted (zk_wrap_missing) — an admin must grant it")
        elif status == 200 and isinstance(payload, dict):
            if payload.get("zk"):
                # Ciphertext only. This file never decrypts (stdlib-only): the canopy CLI
                # on this box — `agent prepare --runner`, `cred sync` — unwraps with the
                # box's ECDH key and writes the env, exactly as for a custodial agent.
                _set_check(f"zk.{slug}", "ok", "zero-knowledge vault: values are opened "
                           "by the canopy CLI on this box")
            token = str(payload.get("op_sa_token") or "")
            _AGENT_VAULT[slug] = str(payload.get("op_vault") or "").strip()
            _AGENT_SF[slug] = str(payload.get("salesforce_creds") or "")
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: could not resolve {slug}'s 1Password key ({exc}); the turn runs without one")
    _AGENT_OP[slug] = (now, token)
    if not token:
        _log(f"{slug}: no 1Password key registered in canopy-web — `op` is unavailable to this turn "
             f"(register it: PUT /api/agents/{slug}/vault)")
    return token


#: Re-reading each agent's `~/.<slug>/.env` from its vault when a turn is claimed.
#: The vault is the one place a value lives: bootstrap used to be the ONLY writer
#: of the file, so a value changed in 1Password reached no turn until the service
#: restarted — cloud-ec2-2 kept ACE's old Drive root all day on 2026-10-12.
#:
#: Cheap by construction: at most once per TTL per agent, and the refresh is
#: canopy's `canopy cred sync` — ONE `op vault get` per vault the template names,
#: the full `op inject` only when that fingerprint or the template changed. The
#: same command an agent session runs at start, so there is nothing to drift.
_AGENT_ENV_AT: dict[str, float] = {}
_AGENT_ENV_TTL_SECONDS = float(os.environ.get("AGENT_ENV_TTL_SECONDS", "300"))
_AGENT_ENV_TIMEOUT_SECONDS = float(os.environ.get("AGENT_ENV_TIMEOUT_SECONDS", "30"))
_AGENT_ENV_LOCKS: dict[str, threading.Lock] = {}
_AGENT_ENV_LOCKS_GUARD = threading.Lock()

#: Only the agent's key decides whose vault is read — never a personal `op`
#: session or Connect server that happens to be in this process's environment.
_OP_PERSONAL_ENV_PREFIXES = ("OP_SESSION_",)
_OP_PERSONAL_ENV_KEYS = ("OP_ACCOUNT", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN")


def _refresh_agent_env_canopy(slug: str, op_token: str, vault: str) -> None:
    """The claim-time refresh: canopy's `canopy cred sync` — the function every agent
    session's start hook already runs, same fingerprint, same `.env.stamp` — given the
    agent's own key and the vault this box was told, run from the agent's clone so it
    finds the template there. This box kept its own copy until 2026-10-15; canopy holds
    the one implementation now (docs/architecture/agent-setup-lifecycle.md)."""
    env = {k: v for k, v in _child_safe_env().items()
           if k not in _OP_PERSONAL_ENV_KEYS and not k.startswith(_OP_PERSONAL_ENV_PREFIXES)}
    env.update(OP_SERVICE_ACCOUNT_TOKEN=op_token, AGENT_VAULT=vault, AGENT_SLUG=slug)
    # The turn this sync is for, so the resolve it makes names it (canopy's cred_resolve
    # reads CANOPY_TURN_ID). It lives in this thread's turn env, not the process's, so
    # without this every claim-time sync read as a box outside a turn (canopy-web#1563).
    turn_id = str((getattr(_TURN_ENV, "extra", None) or {}).get("CANOPY_TURN_ID") or "")
    if turn_id:
        env["CANOPY_TURN_ID"] = turn_id
    try:
        res = subprocess.run([_canopy_cli(), "cred", "sync", "--agent", slug, "--ttl", "0"],
                             env=env, cwd=str(pathlib.Path(AGENT_ROOT) / slug),
                             capture_output=True, text=True,
                             timeout=_AGENT_ENV_TIMEOUT_SECONDS + 200)
        said = " ".join((res.stderr or res.stdout or "").split())[:300]
        err = "" if res.returncode == 0 else (said or f"canopy cred sync exited {res.returncode}")
    except subprocess.TimeoutExpired:
        err, said = "canopy cred sync did not finish in time", ""
    except OSError as exc:
        err, said = f"canopy could not run ({exc})", ""
    out = pathlib.Path.home() / f".{slug}" / ".env"
    if err:
        _log(f"warn: {slug}: could not refresh {out} from its vault — this turn uses the "
             f"existing file, which may be stale: {err}")
        _set_check(f"env.{slug}", "warn",
                   f"vault refresh failed; turns run on the last good {out}: {err}")
        return
    _set_check(f"env.{slug}", "ok", f"{out}: {said.removeprefix(f'[canopy cred] {slug}: ')}")


def _refresh_agent_env(slug: str, op_token: str, vault: str = "") -> None:
    """Bring `~/.<slug>/.env` up to date with the agent's vault, cheaply.

    At most once per `_AGENT_ENV_TTL_SECONDS` per agent, timed from the last
    ATTEMPT, so an unreachable 1Password costs one slow claim per TTL. The refresh
    itself is canopy's `canopy cred sync` (`_refresh_agent_env_canopy`): re-inject
    only when the vault or template changed, keep the last good file on failure,
    report the `env.<slug>` health check. This box used to carry its own copy.
    """
    tpl = pathlib.Path(AGENT_ROOT) / slug / ".env.tpl"
    if not op_token or not tpl.is_file():
        return  # bootstrap already reports an unregistered or tpl-less agent
    if not vault:
        # A key with no recorded vault: the template's `${AGENT_VAULT}` has nothing
        # to expand to, and deriving `Agent-<Slug>` is the fallback we removed
        # (2026-10-14, canopy#899). Keep the last good file and say why.
        _set_check(f"env.{slug}", "warn",
                   f"canopy-web records no 1Password vault for {slug} "
                   f"(PUT /api/agents/{slug}/vault) — .env not refreshed")
        return
    with _AGENT_ENV_LOCKS_GUARD:
        lock = _AGENT_ENV_LOCKS.setdefault(slug, threading.Lock())
    with lock:  # two turns of one agent claimed together probe once
        now = time.time()
        if now - _AGENT_ENV_AT.get(slug, 0.0) < _AGENT_ENV_TTL_SECONDS:
            return
        _AGENT_ENV_AT[slug] = now
        _refresh_agent_env_canopy(slug, op_token, vault)


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
    runner must control (CANOPY_BASE_URL, PATH), and
    OVER it for everything else the agent declares. Parsing is intentionally
    minimal — `op inject` emits plain `KEY=value` lines; anything exotic (export
    prefixes, shell interpolation) is not part of the format and is skipped
    rather than guessed at.
    """
    extra = getattr(_TURN_ENV, "extra", None) or {}
    confined = getattr(_TURN_ENV, "confined", None)
    if confined is not None:
        # A caller's turn: an allowlist, not the layering below — no agent .env,
        # no 1Password key (so no credentials/resolve call), no canopy PAT.
        return _confined_env(slug, confined, extra)
    env = _child_safe_env()
    for key in _GITHUB_ENV_KEYS:
        env.pop(key, None)
    if not slug:
        return {**env, **extra}
    # Never the box's credential for an AGENT: it acts as itself (CANOPY_WEB_PAT
    # from its ~/.<slug>/.env), and a turn that fell back to this process's
    # CANOPY_TOKEN acted as the box's owner with their whole canopy.
    env.pop("CANOPY_TOKEN", None)
    # WHICH agent this is, as a variable Claude Code will actually hand on. It
    # strips secret-looking variables (CANOPY_WEB_PAT among them) from the env it
    # gives a plugin's MCP `headersHelper`, and runs that helper from the plugin's
    # own directory, so the canopy-web helper could neither read the agent's PAT
    # nor find the agent by walking up from cwd — it sent no auth header and the
    # session had no canopy-web MCP at all (cloud-ec2-1, 2026-09-22). A slug is
    # not a secret; the helper reads the PAT from ~/.<slug>/.env itself.
    env["CANOPY_AGENT"] = slug
    # The agent's OWN 1Password key, or none at all. Inheriting this process's
    # would hand every agent a credential that reads every vault.
    op_token = _agent_op_token(slug)
    if op_token:
        env["OP_SERVICE_ACCOUNT_TOKEN"] = op_token
    else:
        env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    # The instance's recorded vault, for anything in the turn that `op inject`s
    # an agent template itself (2026-10-14, canopy#899). Not a secret. Unset,
    # never derived, when canopy-web records none.
    vault = _AGENT_VAULT.get(slug, "") if op_token else ""
    env["AGENT_SLUG"] = slug
    if vault:
        env["AGENT_VAULT"] = vault
    else:
        env.pop("AGENT_VAULT", None)
    # chrome-sales in an agent turn acts in Salesforce only as the identity this
    # agent BORROWS (canopy-web#1291), never as whatever sits in the box's
    # ~/.chrome-sales/. No loan → no CHROME_SALES_HOME, and chrome-sales finds no
    # Salesforce creds for an agent session at all.
    env.pop("CHROME_SALES_HOME", None)
    sf_home = _stage_salesforce(slug)
    if sf_home is not None:
        env["CHROME_SALES_HOME"] = str(sf_home)
    # The agent's OWN gog home and keyring password (spec §6) — never another
    # agent's, never a box-wide one. {} on a box without a unit secret.
    env.update(gog_env(slug))
    if secrets_off_disk():
        # Off disk (spec §4): the values the canopy CLI resolved in memory, and the
        # turn's own tmpfs file (`CANOPY_AGENT_ENV_FILE`, in `extra`) for the readers
        # that cannot see this environment (the MCP headersHelper). No
        # `~/.<slug>/.env` is read or written — `cred sync` would write one.
        lines = [f"{k}={v}" for k, v in (_turn_values(slug) or {}).items()]
        env_file = pathlib.Path(str(extra.get("CANOPY_AGENT_ENV_FILE") or "(memory)"))
    else:
        # The vault, not this box's last bootstrap, decides what the turn reads.
        _refresh_agent_env(slug, op_token, vault)
        env_file = pathlib.Path.home() / f".{slug}" / ".env"
        try:
            lines = env_file.read_text().splitlines()
        except OSError:
            return {**env, **extra}  # not provisioned (yet) — the turn still runs
    loaded = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if key in _GITHUB_ENV_KEYS:
            continue  # GitHub is the owner's, issued per turn — never the agent's .env
        value = value.strip().strip('"').strip("'")
        # An agent .env provisioned before canopy moved names its OLD address;
        # left alone it would send every CLI call this turn makes there
        # (observed 2026-10-06, after this box itself had moved).
        if key in ("CANOPY_WEB_API_URL", "CANOPY_BASE_URL") and value.rstrip("/") in FORMER_BASES:
            value = BASE_URL
        env[key] = value
        loaded += 1
    if loaded:
        _log(f"loaded {loaded} vars from {env_file}")
    # Over the agent's values too: a stale box-wide GOG_* in an old .env must not
    # replace the agent's own pair.
    env.update(gog_env(slug))
    # LAST, over the agent's own .env: a confined turn's profile is the runner's
    # decision, and nothing an agent provisions may switch it off.
    return {**env, **extra}


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

# No time limit on an ACP turn, like `claude -p`. A wall-clock deadline (5400s)
# killed ACE's full `/ace:run bednet-check-2-visit` on cloud-ec2-2 while it was
# still working (turn 09d9322a, 2026-10-18, failure investigation 8), and an
# idle limit would kill a long quiet tool call the same way. Agent turns run for
# hours. A turn that really is stuck is ended by a stop (`request_stop` cancels
# the ACP session, then closes it if ignored), and an adapter that dies ends
# the prompt on its own.
# Starting (or resuming) the ACP session — Claude Code booting with the agent's
# plugins + MCP servers — is NOT the turn. canopy_acp defaults it to 120s, and
# eva's environment sometimes takes 112-125s (one slow MCP server at startup), so
# a fresh box's first eva drill timed out while ada's took ~2s (2026-10-04,
# cloud-ec2-test). Generous by default; override per box via runner.env.
ACP_SESSION_START_TIMEOUT_SECONDS = float(
    os.environ.get("ACP_SESSION_START_TIMEOUT_SECONDS", "300"))

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


# Turn id -> what to do the moment the executor learns the turn's CLI session id.
# Both executors learn it partway in (claude -p from its `system`/`init` line,
# ACP from session/new or session/load), and the turn's session must be
# recorded THEN — not at finish — for the live stream tail to follow it while it
# runs. A registry rather than a parameter so neither executor's signature
# changes (they are deliberately interchangeable; see run_acp).
_SESSION_HOOKS: dict[str, "object"] = {}
_SESSION_HOOKS_LOCK = threading.Lock()


def _announce_session(turn_id: str, cli_session_id: str) -> None:
    """Executors call this whenever they learn (or change) the turn's CLI session
    id. Never raises: bookkeeping must not cost the turn."""
    if not cli_session_id:
        return
    # Remembered for a session-shaped stop (`stop_session`), confined or not.
    with _STOP_LOCK:
        _TURN_SESSIONS[turn_id] = cli_session_id
    with _SESSION_HOOKS_LOCK:
        hook = _SESSION_HOOKS.get(turn_id)
    if hook is None:
        return
    try:
        hook(cli_session_id)
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: session hook for {turn_id[:8]} failed: {exc}")


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
    from canopy_acp.turn import TurnError, drive_turn  # noqa: PLC0415 — lazy, like _acp_core

    workdir = cwd if cwd is not None else pathlib.Path(WORK_DIR) / turn_id[:8]
    workdir.mkdir(parents=True, exist_ok=True)
    resume = (resume_session_id
              if resume_session_id and _resume_target_exists(workdir, resume_session_id) else None)

    def on_session(session_id: str) -> None:
        _announce_session(turn_id, session_id)
        _log(f"exec: acp (turn {turn_id[:8]}) in {workdir} session={session_id[:8]}")

    try:
        # The loop itself — events, replay, wedge guard, releasing the session —
        # is canopy_acp.drive_turn, shared with the laptop's `claude-cli` runtime.
        # Hooks: reachable by the WS thread (`stop_turn`) while it runs, and
        # Unregister BEFORE close: a stop/cancel that arrives in the gap would
        # otherwise reach a closed connection and raise inside the WS thread.
        res = drive_turn(
            prompt, make_agent=core.AcpAgent, reducer=core.UpdateReducer(), emit=emit,
            cwd=workdir, env=_agent_env(agent_slug), resume=resume,
            on_start=lambda agent: _acp_register(turn_id, agent),
            on_session=on_session,
            on_end=lambda agent: _acp_unregister(turn_id),
            cancelled=lambda: stop_requested(turn_id),
            session_start_timeout=ACP_SESSION_START_TIMEOUT_SECONDS,
            flush_seconds=TRANSCRIPT_FLUSH_SECONDS, clock=time.time, log=_log)
    except TurnError as exc:
        _log(f"turn {turn_id[:8]}: ACP executor failed: {exc.detail}")
        return False, f"runner error (acp): {exc.detail}", exc.session_id
    final_text = res.text
    limit = res.rate_limit
    if limit:
        _log(f"turn {turn_id[:8]}: rate limit {limit.get('status')} ({limit.get('rateLimitType')})")
        if limit.get("status") == "rejected":
            # The adapter can end a capped turn with a clean `end_turn`; drive_turn
            # fails it on the meta. Cap wording so `execute_prompt` runs its
            # cascade, and keep the exact reset.
            if not _is_usage_cap(final_text):
                final_text = (f"{final_text}\n\nYou've hit your "
                              f"{limit.get('rateLimitType') or 'usage'} limit")
            resets = limit.get("resetsAt")
            if isinstance(resets, (int, float)) and resets > 0:
                _CAP_RESET_HINT[turn_id] = float(resets)
    return res.ok, final_text, res.session_id


def _execute_once(prompt: str, turn_id: str, emit, cwd=None, agent_slug=None,
                  resume_session_id=None) -> tuple[bool, str, str]:
    """The one place a turn picks an executor."""
    if stop_requested(turn_id):
        return False, "stopped before it started", ""
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
    resets: list[float] = []      # each capped credential's reset, epoch seconds
    capped_labels: list[str] = []
    dead_labels: list[str] = []
    cap_line = ""                 # the first cap message, kept for the final note
    while True:
        ok, text, session_id = _execute_once(
            prompt, turn_id, emit, cwd=cwd, agent_slug=agent_slug,
            resume_session_id=resume_session_id)
        label = _claude_cred_label()
        attempted.append(label)
        if stop_requested(turn_id):
            # A person stopped it: never fail over and run it again.
            return ok, text, session_id
        capped, dead = _is_usage_cap(text), _is_auth_required(text)
        hint = _CAP_RESET_HINT.pop(turn_id, None)
        reset = None
        if capped and not ok:
            reset = hint or _cap_reset_epoch(text)
            if reset and reset > time.time():
                resets.append(reset)
            else:
                reset = None
        if ok or not (capped or dead):
            if ok:
                _record_outcome(label, "ok", detail="ran a turn")
                _promote_working_subscription(turn_id)
            return ok, text, session_id
        if capped:
            capped_labels.append(label)
            _record_outcome(label, "capped", detail=next(
                (ln.strip() for ln in text.splitlines() if _is_usage_cap(ln)), ""),
                reset=reset)
            cap_line = cap_line or next(
                (ln.strip() for ln in text.splitlines() if _is_usage_cap(ln)), "")
            _log(f"turn {turn_id[:8]}: {label} is at its usage cap")
        elif capped_labels and _is_bare_acp_refusal(text):
            # Investigation 9 (turn 7d230f39, ACE on cloud-ec2-2, 2026-10-18): the
            # box login capped, then the setup-token answered ACP's bare
            # `auth_required`. Every credential was out of usage, but this was
            # read as "not signed in": a false alert, and a note telling a person
            # to re-mint a token that would reset on its own. ACP gives the same
            # bare refusal for a capped token and a dead one, so once this turn
            # has seen a cap it counts as capped. A token that is really dead
            # still shows up as dead on the first turn after the caps reset.
            capped_labels.append(label)
            _record_outcome(label, "capped", detail="refused the session after a cap")
            _log(f"turn {turn_id[:8]}: {label} refused the session after a cap — "
                 "counted as capped")
        elif _is_bare_acp_refusal(text) and _probe_says_capped(label, resets, turn_id):
            # cloud-ec2-fork, 2026-10-18: ONE setup-token, at its weekly cap,
            # answered ACP's bare refusal, and with nothing else in the turn to
            # have capped first, every turn said "not signed in". The refusal
            # does not say which it is, so the credential is asked directly.
            capped_labels.append(label)
            cap_line = cap_line or _PROBES.get(label, {}).get("detail", "")
        else:
            # An unusable credential either way, so it advances the same as a cap
            # — but the REASON is carried to the terminal message below, because
            # "wait for the reset" is wrong advice for a token that has none.
            dead_labels.append(label)
            if not _is_bare_acp_refusal(text):
                # The CLI's own sentence: evidence. A bare refusal's probe has
                # already recorded what it found, and "unknown" stays unknown.
                _record_outcome(label, "dead", detail=next(
                    (ln.strip() for ln in text.splitlines() if _is_auth_required(ln)), ""))
            _log(f"turn {turn_id[:8]}: {label} is not signed in")
            _notify_auth_required(label, turn_id)
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
            if not capped_labels:
                # Deliberately does NOT mention waiting: this is the state that
                # never clears on its own, and telling an operator a cap will
                # reset is how a box sits dead for a day.
                note = (f"[runner] this box is not signed in to Claude (tried: "
                        f"{', '.join(attempted)}). This will NOT clear on its own — "
                        f"someone has to re-authenticate the subscription and set the "
                        f"token (`canopy runner credential`).")
            else:
                until = _park_until_cap_resets(resets, cap_line or text, turn_id)
                _notify_out_of_usage(capped_labels, dead_labels, until, turn_id)
                note = (f"[runner] out of Claude usage — every Claude credential on "
                        f"this box is exhausted (tried: {', '.join(attempted)})."
                        f"{' ' + cap_line if cap_line else ''} This runner has paused "
                        f"itself until {until} so other runners take its work; it "
                        f"resumes on its own then, or sooner if a new credential is "
                        f"set (`canopy runner credential`) and it is unpaused.")
                if dead_labels:
                    note += (f" Separately, {', '.join(dead_labels)} is not signed in "
                             f"and will NOT clear on its own — someone has to "
                             f"re-authenticate it.")
            return ok, f"{text}\n\n{note}", session_id
        # `--resume` is deliberately dropped on the retry: the failed attempt may
        # have written a partial session, and resuming it under a different
        # credential is not a state we want to debug at 2am.
        resume_session_id = None


def _probe_says_capped(label: str, resets: list[float], turn_id: str) -> bool:
    """Probe the credential a bare ACP refusal came from. True (and its reset
    appended) when it is at its usage cap; the result is recorded either way."""
    probe = _probe_credential(_CLAUDE_CRED_I)
    _record_probe(probe)
    _log(f"turn {turn_id[:8]}: {label} refused the session; probe says {probe['status']}")
    if probe["status"] != "capped":
        return False
    reset = probe.get("reset")
    if reset and reset > time.time():
        resets.append(reset)
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
    # Reachable by a stop from here on (`request_stop`).
    _register_cli_proc(turn_id, proc)
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
                if evt["session_id"] != cli_session_id:
                    _announce_session(turn_id, evt["session_id"])
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
            and loop_error is None and proc.returncode != 0
            and not stop_requested(turn_id)):  # a stopped turn is not a failed resume
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


# ── GitHub: the agent owner's identity, one turn at a time ──────────────────
#
# There is no box-wide GitHub credential. There used to be one — a shared
# fine-grained PAT exported as GH_TOKEN into this process (so every turn
# inherited it) and written to a global ~/.git-credentials — which meant every
# agent pushed as whoever made that token, with whatever it could do, and could
# not open a pull request (#747). Now each TURN asks canopy-web for its agent
# owner's token for that agent (`AgentDelegation`), and it goes into that turn's
# environment and nowhere else, so two concurrent turns for agents with
# different owners cannot see each other's.

#: Variables that may carry a GitHub credential into a turn from anywhere but
#: `_github_turn_env`: this process's own environment, or an agent's rendered
#: `.env`. Stripped from every turn, so a turn with no delegation has NO GitHub
#: identity rather than a leftover one.
_GITHUB_ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")


def _retire_shared_github_credentials() -> None:
    """Remove what the shared-PAT era left on this box: the global
    `~/.git-credentials` store and the `credential.helper store` pointing at it,
    and any GitHub token in this process's environment. Idempotent; run at every
    start, because a box that updated in place still has them."""
    for key in _GITHUB_ENV_KEYS:
        os.environ.pop(key, None)
    creds = pathlib.Path.home() / ".git-credentials"
    try:
        if creds.exists():
            creds.unlink()
            _log("removed the shared ~/.git-credentials (GitHub is per turn now)")
    except OSError as exc:
        _log(f"warn: could not remove {creds}: {exc}")
    try:
        helper = subprocess.run(["git", "config", "--global", "--get", "credential.helper"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if helper == "store":
            subprocess.run(["git", "config", "--global", "--unset", "credential.helper"],
                           capture_output=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: could not unset the global git credential helper: {exc}")


#: The credential helper a turn's git uses. It reads GH_TOKEN from the turn's own
#: environment at the moment git asks, so it holds nothing itself, and the empty
#: first entry clears any helper configured globally for github.com.
_GIT_HELPER = '!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'


def github_env(token: str, *, git_name: str = "", git_email: str = "",
               requested_by: str = "") -> dict:
    """The environment that gives a subprocess — and only it — a GitHub identity.

    `gh` reads GH_TOKEN; `git` gets a helper through GIT_CONFIG_COUNT/KEY/VALUE,
    which applies to that process tree and touches no config file. Commits carry
    the owner's identity instead of "Ubuntu <ubuntu@ip-…>". Every key is always
    present, blank when unknown, so a turn's env never inherits a stale one.
    """
    env = {
        "GH_TOKEN": token,
        "GITHUB_TOKEN": token,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_1": _GIT_HELPER,
        "CANOPY_REQUESTED_BY": requested_by,
    }
    if git_name and git_email:
        env.update({"GIT_AUTHOR_NAME": git_name, "GIT_AUTHOR_EMAIL": git_email,
                    "GIT_COMMITTER_NAME": git_name, "GIT_COMMITTER_EMAIL": git_email})
    return env


#: Where a chat's key (canopy_sessions.ChatKey) is left for the one Claude session
#: driving that chat — see `_chat_key_env`.
CHAT_KEY_ROOT = pathlib.Path.home() / ".canopy" / "chat"
_CHAT_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _chat_key_env(turn: dict) -> dict:
    """Give this chat turn its chat's key, and only this turn.

    canopy mints a key when a runner claims a chat's turn; presenting it (the
    `X-Canopy-Chat-Key` header) reaches that chat's secrets and page and nothing
    else. `canopy secret` reads it from CANOPY_CHAT_KEY. The MCP headers helper
    cannot — Claude Code strips secret-looking variables from its environment —
    so the key is also written to `~/.canopy/chat/chat/<chat id>.key` (0600) and
    the helper finds it through CANOPY_CHAT_SESSION, which is not a secret."""
    key = str(turn.get("chat_key") or "")
    chat_id = _chat_session_id(turn)
    if not key or not _CHAT_ID.match(chat_id):
        return {}
    try:
        _write_private_text(CHAT_KEY_ROOT / "chat" / f"{chat_id}.key", key)
    except OSError as exc:
        _log(f"warn: could not leave chat {chat_id[:8]}'s key for its MCP helper: {exc}")
    return {"CANOPY_CHAT_KEY": key, "CANOPY_CHAT_SESSION": chat_id}


SCOPED_TOKEN_ROOT = pathlib.Path.home() / ".canopy" / "scoped"
_SCOPED_KEEP_SECONDS = 7 * 24 * 3600


def _scoped_token_env(turn: dict) -> dict:
    """A full-profile turn asked by someone who is not the agent's owner, an admin
    or canopy (a `full:` rule, an editor) reaches canopy's MCP as its ASKER, not as
    this box's PAT (who-is-asking phase 5, canopy-web#1332).

    canopy hands such a turn a caller token (`scoped`) with the claim. The MCP
    headers helper cannot read it from the environment — Claude Code strips
    secret-looking variables — so it is written to `~/.canopy/scoped/turn/<id>.token`
    (0600) and the helper finds it through CANOPY_SCOPED_TURN, a turn id, which is
    not a secret. Once that variable is set the helper NEVER falls back to a PAT:
    a missing file makes it send an invalid header. A write that fails refuses the
    turn (ConfineError) rather than run it on the box's credential."""
    token = str(turn.get("mcp_token") or "")
    turn_id = str(turn.get("id") or "")
    if not token:
        return {}
    if not _CHAT_ID.match(turn_id):
        raise ConfineError("this turn's id is not a plain id, so its canopy token cannot be left")
    d = SCOPED_TOKEN_ROOT / "turn"
    try:
        _write_private_text(d / f"{turn_id}.token", token)
        now = time.time()
        for old in d.glob("*.token"):
            try:
                if now - old.stat().st_mtime > _SCOPED_KEEP_SECONDS:
                    old.unlink()
            except OSError:
                continue
    except OSError as exc:
        raise ConfineError(f"could not leave the session's canopy token: {exc}") from exc
    return {"CANOPY_SCOPED_TURN": turn_id}


def _write_private_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _requested_by_from(turn: dict) -> str:
    who = ((turn.get("caller_context") or {}).get("who") or {})
    person = who.get("user") or who.get("contact") or {}
    name, email = person.get("name") or "", person.get("email") or ""
    if email and name and name != email:
        return f"{name} <{email}>"
    return email or name


def _github_turn_env(runner_id: str, turn: dict) -> dict:
    """This turn's GitHub identity, from canopy-web — or none.

    canopy-web decides WHOSE (the turn's agent owner, for that agent, or a
    project turn's configured fallback — this box doesn't know which); this
    only names the turn. A refusal (no delegation, expired, no fallback
    configured) is logged, emitted on the turn so its viewer can see why a
    push will fail, and the turn still runs: most turns never touch GitHub,
    and failing them all would be worse."""
    requested_by = _requested_by_from(turn)
    if not _turn_agent_slug(turn) and not turn.get("project"):
        return {"CANOPY_REQUESTED_BY": requested_by}
    turn_id = str(turn["id"])
    status, body = _api("POST", f"/runners/{runner_id}/turns/{turn_id}/github-token")
    if status == 200 and body and body.get("token"):
        _log(f"turn {turn_id[:8]}: GitHub as @{body.get('github_login') or '?'}")
        return github_env(body["token"], git_name=body.get("git_name") or "",
                          git_email=body.get("git_email") or "",
                          requested_by=body.get("requested_by") or requested_by)
    _log(f"turn {turn_id[:8]}: no GitHub identity (HTTP {status}) — pushes and pull requests "
         f"will fail; the agent's owner lends one at the agent's Settings → Credentials → GitHub")
    return {"CANOPY_REQUESTED_BY": requested_by}


# Where a turn's chat attachments land: outside every checkout, so a screenshot
# never shows up as an untracked file in the agent's repo.
ATTACHMENT_DIR = pathlib.Path(WORK_DIR) / "attachments"


def _attachment_name(name: str) -> str:
    """A filename safe to join onto a local path — never trust one handed over
    the wire. Mirrors canopy_runner.execute._safe_name (this file is stdlib-only
    and imports nothing from canopy_runner)."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in name).strip(".-")
    return cleaned[:120] or "attachment"


def _fetch_attachments(runner_id: str, turn: dict) -> tuple[list[pathlib.Path], list[str]]:
    """Download the files a person sent with this turn: (paths that arrived,
    names that did not). The same contract as canopy_runner.execute.
    fetch_attachments — this box used to ignore attachments altogether, so a
    screenshot sent to a cloud-served chat simply never reached the agent.

    Fetched through the turn (`/runners/{rid}/turns/{tid}/attachments/…`):
    canopy-web serves them because this box holds the turn, not because its
    owner can read the chat. Best-effort per file; a failure is named, never
    silent."""
    refs = (turn.get("origin_ref") or {}).get("attachments") or []
    turn_id = str(turn.get("id", ""))
    paths: list[pathlib.Path] = []
    missing: list[str] = []
    for n, ref in enumerate(refs, start=1):
        aid = ref.get("id")
        if not aid:
            continue
        # Numbered by position, as canopy_runner.execute.fetch_attachments: two
        # pasted screenshots are both "image.png" and overwrote each other.
        name = f"{n}-{_attachment_name(ref.get('filename', ''))}"
        dest = ATTACHMENT_DIR / turn_id / name
        url = (f"{BASE_URL}/api/harness/runners/{runner_id}/turns/{turn_id}"
               f"/attachments/{aid}/content")
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {_bearer()}")
            req.add_header("X-Canopy-Client", "canopy-cloud-runner")
            req.add_header("User-Agent", f"canopy-cloud-runner host={socket.gethostname()}")
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
        except Exception as exc:  # noqa: BLE001 — one file must never crash the turn's worker
            _log(f"turn {turn_id[:8]}: attachment {aid} could not be fetched ({exc}); continuing without it")
            missing.append(name)
            continue
        paths.append(dest)
    return paths, missing


_ATTACHMENT_PLACEHOLDER = re.compile(r"\[(?:Image|File) #(\d+)\]")


def _attachment_lines(prompt: str, paths: list[pathlib.Path]) -> tuple[str, bool]:
    """Mirrors canopy_runner.execute.attachment_lines: each path led by the
    composer's "[Image #N]" when the message has one; the message is untouched."""
    placed = {m.group(1): m.group(0) for m in _ATTACHMENT_PLACEHOLDER.finditer(prompt or "")}
    lines, any_placed = [], False
    for p in paths:
        token = placed.get(p.name.split("-", 1)[0])
        any_placed = any_placed or bool(token)
        lines.append(f"- {token} {p}" if token else f"- {p}")
    return "\n".join(lines), any_placed


def _with_attachments(prompt: str, paths: list[pathlib.Path], missing: list[str]) -> str:
    """Name the files for the agent to READ, and the ones that never arrived.
    Same wording as canopy_runner.execute.prompt_with_attachments, so an agent
    behaves the same whichever box serves it."""
    out = prompt
    if paths:
        lines, placed = _attachment_lines(prompt, paths)
        noun = "file" if len(paths) == 1 else "files"
        out = (f"{out}\n\nThe user attached the following {noun}. "
               f"Read {'it' if len(paths) == 1 else 'them'} with the Read tool before replying:\n{lines}")
        if placed:
            out += "\n([Image #N] in the message marks where the user placed that file.)"
    if missing:
        names = ", ".join(missing[:10]) + (f" and {len(missing) - 10} more" if len(missing) > 10 else "")
        out = (f"{out}\n\n{len(missing)} attached file{'' if len(missing) == 1 else 's'} could not be "
               f"downloaded and {'is' if len(missing) == 1 else 'are'} not available to you: {names}. "
               "Tell the user, rather than answering as if you had seen "
               f"{'it' if len(missing) == 1 else 'them'}.")
    return out


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

    A chat WITH an agent is that agent, the same as any other turn for it. This
    used to return "" for a chat, on the theory that a live chat is bridged
    rather than run from a checkout — which made a cloud chat with hal a bare
    Claude in a scratch directory: no hal persona, none of hal's own hooks
    (its gating rails live in the repo's .claude/settings.json), and none of its
    credentials. On a laptop the same chat runs in an emdash worktree of the
    agent's repo; `_turn_cwd` now gives the cloud the same thing. Shared by
    `_turn_cwd` (which clone to run in) and `_agent_env` (whose credentials to
    load) so the two can never disagree about who the turn is.
    """
    return turn.get("agent_slug") or ""


def _safe_session_dirname(session_id: str) -> str:
    """A filesystem-safe basename for a session workdir. `session_id` is normally
    a canopy Session UUID (server-controlled at creation, but a turn payload is
    still data off the wire) — this never trusts it enough to join onto a path
    unsanitized, mirroring runner/canopy_runner/canopy_runner/execute.py's
    `_safe_name` for the same reason."""
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in session_id).strip(".-")
    return cleaned[:80] or "unknown-session"


def _git_quiet(*args: str, timeout: float = 60, env: dict | None = None) -> bool:
    try:
        return subprocess.run(["git", *args], capture_output=True, timeout=timeout,
                              env=env).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _ensure_session_worktree(clone: pathlib.Path, path: pathlib.Path,
                             env: dict | None = None) -> None:
    """Give an agent chat session its OWN worktree of the agent's repo, at the
    session's stable path — the cloud equivalent of the emdash worktree a laptop
    chat runs in. The path never changes (`--resume` resolves a Claude session
    by cwd), so this only decides what is IN it:

    * absent → a new detached worktree at origin/main (HEAD if a fetch fails);
    * a worktree with no local changes → moved to origin/main, so a week-old
      conversation still runs the agent's current skills and rails;
    * a worktree with local changes → left alone: the agent is mid-work there;
    * a worktree on a BRANCH → left alone too: a clean tree there can still hold
      unpushed commits, and detaching it would pull the agent off its own work;
    * a plain directory → left alone: a session from before this existed, whose
      `--resume` target lives under exactly this path.

    Best-effort throughout; a failure leaves a plain directory, which is what
    every session got before."""
    if path.exists():
        if not (path / ".git").exists():
            return
        try:
            dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            return
        if dirty or _git_quiet("-C", str(path), "symbolic-ref", "-q", "HEAD"):
            return
        _git_quiet("-C", str(clone), "fetch", "--quiet", "origin", env=env)
        _git_quiet("-C", str(path), "checkout", "--quiet", "--detach", "origin/main")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _git_quiet("-C", str(clone), "fetch", "--quiet", "origin", env=env)
    for ref in ("origin/main", "HEAD"):
        if _git_quiet("-C", str(clone), "worktree", "add", "--quiet", "--detach", str(path), ref):
            _log(f"session worktree: {path} from {clone.name} {ref}")
            return
    _log(f"warn: could not create a worktree of {clone} at {path}; using a plain directory")
    path.mkdir(parents=True, exist_ok=True)


# ── per-turn worktrees of any repo (#1131) ───────────────────────────────────
# On 2026-10-05 four turns shared /opt/canopy-web: a stranded commit, a shared
# stash, branches moving under each other. Nothing gave a turn its own checkout of
# a repo that is not its agent's, so each one found the box's shared clone with
# `find /`. Now every such checkout is a worktree of ONE shared clone per repo,
# keyed by the conversation (or the turn), like the emdash worktree a laptop gives.

#: Where a repo name is fetched from. `{repo}` is the bare name (canopy-web).
REPO_URL_TEMPLATE = os.environ.get("REPO_URL_TEMPLATE",
                                   "https://github.com/dimagi-internal/{repo}.git")
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _repos_root() -> pathlib.Path:
    return pathlib.Path(WORK_DIR) / "repos"


def _repo_clone(repo: str, env: dict | None = None) -> pathlib.Path:
    """The shared bare clone every worktree of `repo` hangs off, created on first
    use and fetched on every call. Locked with flock, not a thread lock: the
    `canopy-repo-worktree` helper runs in a turn's own process."""
    if not _REPO_NAME.match(repo) or repo.endswith(".git") or ".." in repo:
        raise ValueError(f"not a repo name: {repo!r}")
    clones = _repos_root() / ".clones"
    clones.mkdir(parents=True, exist_ok=True)
    clone = clones / f"{repo}.git"
    with open(clones / f"{repo}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not clone.exists():
            url = REPO_URL_TEMPLATE.format(repo=repo)
            if not _git_quiet("clone", "--quiet", "--bare", url, str(clone), timeout=600, env=env):
                shutil.rmtree(clone, ignore_errors=True)
                raise RuntimeError(f"could not clone {url}")
            # A bare clone maps branches onto refs/heads; worktrees want origin/main.
            _git_quiet("-C", str(clone), "config", "remote.origin.fetch",
                       "+refs/heads/*:refs/remotes/origin/*")
        _git_quiet("-C", str(clone), "fetch", "--quiet", "--prune", "origin", env=env)
    return clone


def repo_worktree(repo: str, key: str, env: dict | None = None) -> pathlib.Path:
    """`repo`'s worktree for one conversation or turn: WORK_DIR/repos/<repo>/<key>.
    Same refresh rules as a session's worktree (see `_ensure_session_worktree`):
    created at origin/main, brought forward only when clean and detached."""
    path = _repos_root() / repo / _safe_session_dirname(key)
    _ensure_session_worktree(_repo_clone(repo, env=env), path, env=env)
    return path


def _repo_worktree_key(turn: dict, turn_id: str) -> str:
    """A conversation keeps one checkout across its turns; anything else gets its own."""
    return _chat_session_id(turn) or turn_id[:8]


def _repo_worktree_env(turn: dict, turn_id: str) -> dict:
    """What `canopy-repo-worktree` reads to know whose checkout to hand out."""
    return {"CANOPY_REPO_WORKTREE_KEY": _repo_worktree_key(turn, turn_id)}


def _repo_worktree_cli(argv: list[str]) -> int:
    """`canopy-repo-worktree <repo>` — print this turn's own worktree of <repo>."""
    if len(argv) != 1:
        print("usage: canopy-repo-worktree <repo>   (e.g. canopy-web)", file=sys.stderr)
        return 2
    key = os.environ.get("CANOPY_REPO_WORKTREE_KEY", "")
    if not key:
        print("canopy-repo-worktree: CANOPY_REPO_WORKTREE_KEY is not set — "
              "only a turn the cloud runner started can ask for a worktree", file=sys.stderr)
        return 2
    try:
        # stdout is the answer (`cd "$(canopy-repo-worktree x)"`): logs go to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            path = repo_worktree(argv[0], key, env=dict(os.environ))
        print(path)
    except (ValueError, RuntimeError) as exc:
        print(f"canopy-repo-worktree: {exc}", file=sys.stderr)
        return 1
    return 0


def _install_repo_worktree_shim(bindir: pathlib.Path | None = None) -> pathlib.Path:
    """Put `canopy-repo-worktree` on every turn's PATH (~/.local/bin is first on
    the unit's PATH, as for gog). It runs THIS file, so it ships with it."""
    bindir = bindir or pathlib.Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "canopy-repo-worktree"
    shim.write_text("#!/bin/sh\n"
                    "# Installed by cloud_runner.py: this turn's own worktree of a repo.\n"
                    f'exec "{sys.executable}" "{pathlib.Path(__file__).resolve()}" '
                    'repo-worktree "$@"\n')
    shim.chmod(0o755)
    return shim


_SHARED_CLONE_HOOK = """#!/bin/sh
# Installed by cloud_runner.py (#1131). This clone is the box's own: the
# auto-updater reads origin/main from it, and every turn can see it.
repo=$(basename "$(git config --get remote.origin.url)" .git)
echo "This is the cloud box's SHARED $repo clone; turns must not commit here." >&2
echo "Work in your own worktree instead:" >&2
echo "    cd \\"\\$(canopy-repo-worktree $repo)\\"" >&2
exit 1
"""


def _guard_shared_clone(repo_dir: pathlib.Path) -> None:
    """Refuse commits in a clone turns can find but must not work in. Its hooks
    are its own: worktrees come from WORK_DIR/repos/.clones, never from here."""
    hooks = repo_dir / ".git" / "hooks"
    if not hooks.is_dir():
        return
    try:
        hook = hooks / "pre-commit"
        hook.write_text(_SHARED_CLONE_HOOK)
        hook.chmod(0o755)
    except OSError as exc:
        _log(f"warn: could not guard {repo_dir}: {exc}")


#: One on-demand bootstrap at a time, and not again for the same agent within
#: LAZY_BOOTSTRAP_RETRY seconds — an agent whose clone keeps failing must not make
#: every one of its turns wait out a full bootstrap.
_LAZY_BOOTSTRAP_LOCK = threading.Lock()
_LAZY_BOOTSTRAP_TRIED: dict[str, float] = {}
LAZY_BOOTSTRAP_RETRY = int(os.environ.get("LAZY_BOOTSTRAP_RETRY", "600"))
#: Where bootstrap_agents.sh writes the agents its last full pass could not set up.
UNPREPARED_FILE = ".unprepared"


def agents_unprepared() -> list[str] | None:
    """The agents this box TRIED to set up and has no clone of — reported on the
    heartbeat so canopy-web gives it none of their turns (routing.cannot_serve).
    Before, such a turn ran in a bare scratch dir with none of the agent's plugin
    or credentials, and ended having done nothing (fizzy on cloud-ec2-2,
    2026-10-15). None until anything was tried: "unknown", never "all fine"."""
    tried: set[str] = set(_LAZY_BOOTSTRAP_TRIED)
    path = pathlib.Path(AGENT_ROOT) / UNPREPARED_FILE
    known = bool(tried)
    try:
        tried |= {ln.strip() for ln in path.read_text().splitlines() if ln.strip()}
        known = True
    except OSError:
        pass
    if not known:
        return None
    return sorted(s for s in tried if not (pathlib.Path(AGENT_ROOT) / s / ".git").is_dir())


def _ensure_agent_clone(slug: str) -> None:
    """Set an agent up NOW if this box has no clone of it — before its turn runs.

    A box clones agents only when it bootstraps (service start, or a refresh it
    acts on when idle). An agent created since then had its turns run in a bare
    scratch dir: none of its skills, hooks or persona, and the turn still reported
    `done` (labs 2026-10-14: a new agent's first turn answered with the fleet's
    plugins). canopy-web asks the box to refresh when an agent is registered
    (apps/agents/setup.py::request_setup), but a turn can arrive first. Waiting a
    bootstrap here is slower; running as nobody is wrong. It also stamps
    `_BOOTSTRAPPED_AT`, which discharges that pending refresh."""
    if not slug or _safe_session_dirname(slug) != slug:
        return
    if (pathlib.Path(AGENT_ROOT) / slug / ".git").is_dir():
        return
    with _LAZY_BOOTSTRAP_LOCK:
        # Re-checked under the lock: a concurrent turn may have just done it.
        if (pathlib.Path(AGENT_ROOT) / slug / ".git").is_dir():
            return
        if time.time() - _LAZY_BOOTSTRAP_TRIED.get(slug, 0.0) < LAZY_BOOTSTRAP_RETRY:
            return
        _LAZY_BOOTSTRAP_TRIED[slug] = time.time()
        _log(f"agent {slug}: no clone on this box yet — bootstrapping before its turn runs")
        _LAZY_BOOTSTRAPPER(slug)


def _turn_cwd(turn: dict, turn_id: str, env: dict | None = None) -> pathlib.Path:
    """Where claude should run for this turn (runner/ec2 design spec §2:
    'agent turns execute in the agent's clone'). An AGENT turn whose slug has a
    bootstrapped clone under AGENT_ROOT (bootstrap_agents.sh, run once per
    service start — see bootstrap_agent_fleet) runs in its OWN worktree of that
    clone (WORK_DIR/agents/<slug>/<turn>, `_agent_turn_worktree`, #1141), so it
    sees the agent's real repo — config, skills, state — and two concurrent
    turns for one agent no longer share a working tree. The clone is `git
    pull`ed here at claim; a pull failure logs and the worktree comes from the
    clone as-is (stale beats absent).

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

    An agent this box has no clone of yet is bootstrapped first
    (`_ensure_agent_clone`). Everything else — project turns, or an agent whose
    bootstrap failed — keeps the original scratch-dir behavior.

    A FORKED chat runs in its source's directory (`_worktree_of`, from
    resolve-session), so the two share one worktree and the fork's transcript
    sits beside the one it was cut from.

    `env` is the turn's own environment: its git fetches and pulls use the
    turn's GitHub identity, since this box holds none of its own."""
    session_id = turn.get("_worktree_of") or _chat_session_id(turn)
    slug = _turn_agent_slug(turn)
    if slug:
        _ensure_agent_clone(slug)
    # A PROJECT turn (a repo, no agent) works in its own worktree of that repo, at
    # the same path it always had — an empty scratch dir until #1131, which is why
    # such turns went looking for /opt/canopy-web.
    project = "" if slug else (turn.get("project") or "")
    if session_id:
        path = pathlib.Path(WORK_DIR) / "sessions" / _safe_session_dirname(session_id)
        clone = pathlib.Path(AGENT_ROOT) / slug if slug else None
        if clone is not None and (clone / ".git").exists():
            _ensure_session_worktree(clone, path, env=env)
        elif project:
            _project_worktree(project, path, env)
        return path
    if slug:
        agent_dir = pathlib.Path(AGENT_ROOT) / slug
        if (agent_dir / ".git").is_dir() and _safe_session_dirname(slug) == slug:
            # The clone is still pulled: the agent's plugin is installed from it.
            try:
                subprocess.run(
                    ["git", "-C", str(agent_dir), "pull", "--ff-only"],
                    check=False, capture_output=True, timeout=60, env=env,
                )
            except Exception as exc:
                _log(f"warn: git pull in {agent_dir} failed (using clone as-is): {exc}")
            return _agent_turn_worktree(slug, agent_dir, turn_id, env=env)
    path = pathlib.Path(WORK_DIR) / turn_id[:8]
    if project:
        _project_worktree(project, path, env)
    return path


def _project_worktree(project: str, path: pathlib.Path, env: dict | None) -> None:
    """Best-effort, like the agent clone's pull: a repo that cannot be fetched
    leaves the plain directory every project turn got before."""
    try:
        _ensure_session_worktree(_repo_clone(project, env=env), path, env=env)
    except (ValueError, RuntimeError, OSError) as exc:
        _log(f"warn: no worktree of {project!r} at {path} ({exc}); using a plain directory")


# ── per-turn worktrees of an AGENT's repo (#1141) ────────────────────────────
# An agent turn used to run IN the agent's one clone, AGENT_ROOT/<slug>, so two
# concurrent turns for the same agent shared a working tree — the same stranded
# commits and moving branches #1131 fixed for other repos. Each agent turn now
# gets its own worktree of that clone at WORK_DIR/agents/<slug>/<turn>.
#
# What does NOT move: the clone stays where bootstrap put it, pulled at claim,
# because the agent's own plugin is installed FROM it (skills, hooks, MCP
# servers). Its git-ignored state — .env, node_modules, local settings — is not
# in a fresh checkout, so it is linked in (see `_link_clone_state`). Anything an
# agent wants to keep across turns that is neither tracked nor ignored belongs in
# ~/.<slug>/, not the clone.

#: Most ignored entries linked into one worktree — a guard against a clone whose
#: ignore rules match thousands of files, never reached in practice.
_LINK_LIMIT = 200
#: A turn worktree left behind (a crashed worker) is swept after this long. Far
#: longer than any turn runs, so a sweep never pulls a tree from under one.
AGENT_WORKTREE_MAX_AGE = 24 * 3600
#: Session-id → cwd records kept; the oldest go first.
_SESSION_CWDS_KEEP = 2000
_SESSION_CWDS_LOCK = threading.Lock()


def _agent_turns_root(slug: str) -> pathlib.Path:
    return pathlib.Path(WORK_DIR) / "agents" / slug


def _session_cwds_file() -> pathlib.Path:
    return pathlib.Path(WORK_DIR) / "agents" / ".session-cwds.json"


def _git_out(*args: str, timeout: float = 30) -> str | None:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    return r.stdout if r.returncode == 0 else None


def _link_clone_state(clone: pathlib.Path, worktree: pathlib.Path) -> None:
    """Symlink the clone's git-IGNORED files into a fresh worktree of it: the
    rendered .env, node_modules, local settings — what bootstrap and the agent's
    installer put in the clone and a checkout does not carry. Shared, exactly as
    they were when every turn ran in the clone; only the tracked tree and git
    state are per turn.

    Each link is also added to the clone's info/exclude (which every worktree
    shares): git matches a `dir/` ignore rule against directories only, so a
    symlinked node_modules would otherwise show as untracked — and be one
    `git add -A` away from a commit."""
    out = _git_out("-C", str(clone), "ls-files", "--others", "--ignored",
                   "--exclude-standard", "--directory", "-z")
    if not out:
        return
    linked: list[str] = []
    for entry in out.split("\0"):
        rel = entry.rstrip("/")
        if not rel or rel.startswith("/") or ".." in rel.split("/") or rel.split("/")[0] == ".git":
            continue
        if len(linked) >= _LINK_LIMIT:
            _log(f"warn: {clone.name}: more than {_LINK_LIMIT} ignored entries; linked the first")
            break
        dst = worktree / rel
        if dst.exists() or dst.is_symlink() or not dst.parent.is_dir():
            continue
        try:
            dst.symlink_to(clone / rel)
        except OSError as exc:
            _log(f"warn: could not link {rel} into {worktree}: {exc}")
            continue
        linked.append(rel)
    common = _git_out("-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not linked or not common:
        return
    exclude = pathlib.Path(common.strip()) / "info" / "exclude"
    try:
        have = set(exclude.read_text().splitlines()) if exclude.exists() else set()
        add = [f"/{rel}" for rel in linked if f"/{rel}" not in have]
        if add:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as fh:
                fh.write("\n# linked into turn worktrees by cloud_runner.py (#1141)\n")
                fh.write("".join(f"{line}\n" for line in add))
    except OSError as exc:
        _log(f"warn: could not update {exclude}: {exc}")


def _agent_turn_worktree(slug: str, clone: pathlib.Path, turn_id: str,
                         env: dict | None = None) -> pathlib.Path:
    """This agent turn's own worktree of `clone`, or the clone itself when one
    cannot be made — a turn in the shared clone (what every turn got before)
    beats a turn in an empty directory with none of the agent in it."""
    _sweep_agent_worktrees(slug, clone)
    path = _agent_turns_root(slug) / _safe_session_dirname(turn_id[:8])
    _ensure_session_worktree(clone, path, env=env)
    if not (path / ".git").exists():
        with contextlib.suppress(OSError):
            path.rmdir()  # the plain-dir fallback, still empty
        _log(f"warn: no worktree of {slug} for turn {turn_id[:8]}; running in the shared clone")
        return clone
    _link_clone_state(clone, path)
    return path


def _is_agent_worktree(path: pathlib.Path) -> bool:
    try:
        rel = pathlib.Path(path).resolve().relative_to((pathlib.Path(WORK_DIR) / "agents").resolve())
    except (OSError, ValueError, TypeError):
        return False
    return len(rel.parts) == 2


def _release_agent_worktree(path: pathlib.Path) -> bool:
    """Remove a finished agent turn's worktree if nothing in it could be lost:
    no uncommitted change, and HEAD either detached or already on a remote
    branch. Anything else is kept, and said. The turn's transcript is not in the
    worktree (it lives under ~/.claude/projects), so a resume is unaffected.
    True if removed."""
    if not _is_agent_worktree(path) or not (path / ".git").exists():
        return False
    status = _git_out("-C", str(path), "status", "--porcelain")
    if status is None:
        return False
    if status.strip():
        _log(f"kept {path}: it has uncommitted changes")
        return False
    if _git_out("-C", str(path), "symbolic-ref", "-q", "HEAD") is not None:
        if not (_git_out("-C", str(path), "branch", "-r", "--contains", "HEAD") or "").strip():
            _log(f"kept {path}: its branch has commits on no remote")
            return False
    if _git_out("-C", str(path), "worktree", "remove", str(path)) is None:
        _log(f"warn: could not remove the turn worktree {path}")
        return False
    return True


def _sweep_agent_worktrees(slug: str, clone: pathlib.Path) -> None:
    """Release the worktrees of this agent's turns that ended without doing it
    themselves (a crash, a restart) — only ones old enough that no turn can
    still be in them."""
    root = _agent_turns_root(slug)
    if not root.is_dir():
        return
    cutoff = time.time() - AGENT_WORKTREE_MAX_AGE
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                _release_agent_worktree(child)
        except OSError:
            continue
    _git_quiet("-C", str(clone), "worktree", "prune")


def _record_session_cwd(cli_session_id: str, cwd: pathlib.Path) -> None:
    """Remember where an agent turn's CLI session ran, so a chat reply's resume
    and the stream tail can find its transcript without re-deriving a path that
    is now per turn. Only agent-turn worktrees are recorded: every other cwd is
    still derivable from the session itself."""
    if not cli_session_id or not _is_agent_worktree(cwd):
        return
    path = _session_cwds_file()
    with _SESSION_CWDS_LOCK:
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.pop(cli_session_id, None)
        data[cli_session_id] = str(cwd)
        while len(data) > _SESSION_CWDS_KEEP:
            data.pop(next(iter(data)))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
        except OSError as exc:
            _log(f"warn: could not record session {cli_session_id[:8]}'s cwd: {exc}")


def _agent_session_transcript(slug: str, cli_session_id: str) -> pathlib.Path | None:
    """Where an agent turn's CLI session wrote its transcript, or None.

    The recorded cwd first; then any of this agent's turn worktrees (a record
    lost with WORK_DIR, e.g. on a reboot — the transcripts outlive it); then the
    shared clone, where every agent turn ran before #1141."""
    if not (slug and cli_session_id) or _safe_session_dirname(slug) != slug:
        return None
    name = f"{cli_session_id}.jsonl"
    try:
        recorded = json.loads(_session_cwds_file().read_text()).get(cli_session_id)
    except (OSError, ValueError, AttributeError):
        recorded = None
    candidates = []
    if recorded:
        candidates.append(CLAUDE_PROJECTS_HOME / _encode_project_dir(pathlib.Path(recorded)) / name)
    prefix = _encode_project_dir(_agent_turns_root(slug)) + "-"
    try:
        candidates += sorted(CLAUDE_PROJECTS_HOME.glob(f"{glob.escape(prefix)}*/{glob.escape(name)}"))
    except OSError:
        pass
    candidates.append(CLAUDE_PROJECTS_HOME / _encode_project_dir(pathlib.Path(AGENT_ROOT) / slug) / name)
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def sync_runner_src() -> bool:
    """Bring RUNNER_SRC_DIR to origin/main and expose its packages.

    Reset, not merged: this clone belongs to the runner alone, so there is no
    local work to protect, and `pull --ff-only` is exactly what failed on
    2026-09-22. The packages are exposed WHENEVER the clone exists — a stale
    clone beats a disabled feature — and the outcome lands in the `code_clone`
    health check instead of one journald line."""
    repo_dir = pathlib.Path(RUNNER_SRC_DIR)
    synced = False
    try:
        if (repo_dir / ".git").is_dir():
            subprocess.run(["git", "-C", str(repo_dir), "fetch", "--quiet", "--depth", "1",
                            CANOPY_WEB_REPO_URL, "main"], check=True, timeout=120)
            subprocess.run(["git", "-C", str(repo_dir), "checkout", "--quiet", "--force",
                            "--detach", "FETCH_HEAD"], check=True, timeout=60)
        else:
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", "--depth", "1", CANOPY_WEB_REPO_URL,
                            str(repo_dir)], check=True, timeout=180)
        synced = True
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: could not sync {repo_dir} to origin/main: {exc}")
        _set_check("code_clone", "fail" if not (repo_dir / ".git").is_dir() else "warn",
                   f"could not sync {repo_dir} to origin/main ({exc}); "
                   + ("using the clone as it was" if (repo_dir / ".git").is_dir() else "no clone"))
    if not (repo_dir / ".git").is_dir():
        return False
    if synced:
        sha = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        _set_check("code_clone", "ok", f"{repo_dir} at origin/main {sha}")
    _install_transcript_core(repo_dir)
    return True


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
    # The SAME inbox reader the laptop runs, imported rather than reimplemented.
    # `canopy_runner.inbox` and `.inbox_due` are stdlib-only and its package
    # __init__ is a version string, so this costs nothing and — more to the point
    # — means the doorbell, the (thread, messageCount) idempotency key, the
    # own-reply skip and the CloudWatch alarm coalescing have ONE implementation
    # and one set of tests. A second copy of that logic would drift the way
    # `encode_project_dir` already has (three files, "keep the two in step").
    _expose_repo_package(repo_dir, "canopy_runner", "the shared inbox reader",
                         parent="runner")
    _install_acp_adapter()


#: Turn id -> the live AcpAgent driving it, for the WS thread to reach into.
#:
#: A turn runs on a worker thread blocked inside `run_acp`'s pump; the cancel
#: control frame arrives on the WS thread and must reach that agent without
#: waiting for the pump. `AcpConnection._send` is lock-guarded, so a call from
#: another thread is safe by construction.
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


# ── A person's stop ─────────────────────────────────────────────────────────
#
# canopy-web asks a runner to stop a turn three ways, and until 2026-10-17 this
# runner heard none of them: a `cancel` frame (turn-shaped), a `session_interrupt`
# frame (session-shaped) and — the durable one — `cancel_turn_ids` on every
# heartbeat reply. The WS loop had no branch for either frame and nothing read
# the reply, so `stop_turn` above existed and was never called: Ada stopped a
# huddle turn on cloud-ec2-2 twice and it ran 23 more minutes, dispatching
# rounds to Hal after each stop.
#
# All three now land on `request_stop`, from whichever thread heard them (the
# WS thread, the REST loop, a turn's lease renewer). It must never block: it
# records the stop, signals the executor, and returns. The turn's own worker
# notices on its way out and finishes it CANCELLED.

#: Turn ids a person asked to stop, and when the stop first arrived.
_STOP_REQUESTED: dict[str, float] = {}
_STOP_LOCK = threading.Lock()
#: Turn id -> the live `claude -p` process (the cli executor's handle).
_CLI_PROCS: dict = {}
#: Turn id -> the CLI session id it drives, for a session-shaped stop.
_TURN_SESSIONS: dict[str, str] = {}
#: A stop re-delivered this long after the first one escalates: SIGKILL for
#: `claude -p`, closing the adapter for ACP. Every heartbeat re-delivers a stop
#: the server still sees running, so this is reached only by a turn that ignored
#: the polite one.
STOP_ESCALATE_SECONDS = float(os.environ.get("STOP_ESCALATE_SECONDS", "30"))


def stop_requested(turn_id: str) -> bool:
    with _STOP_LOCK:
        return turn_id in _STOP_REQUESTED


def _clear_stop(turn_id: str) -> None:
    with _STOP_LOCK:
        _STOP_REQUESTED.pop(turn_id, None)
        _CLI_PROCS.pop(turn_id, None)
        _TURN_SESSIONS.pop(turn_id, None)


def _register_cli_proc(turn_id: str, proc) -> None:
    """Make a `claude -p` process stoppable — and stop it at once if the stop
    beat the spawn (it arrived while the worktree was still being prepared)."""
    with _STOP_LOCK:
        _CLI_PROCS[turn_id] = proc
        early = turn_id in _STOP_REQUESTED
    if early:
        _signal_cli(turn_id, proc, escalate=False)


def _signal_cli(turn_id: str, proc, *, escalate: bool) -> bool:
    try:
        if proc.poll() is not None:
            return False
        if escalate:
            proc.kill()
        else:
            proc.terminate()
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"stop turn={turn_id[:8]}: could not signal claude: {exc}")
        return False


def _close_acp_later(turn_id: str, agent) -> None:
    """Close an ACP agent that ignored `session/cancel` — on its own thread,
    because `close()` waits on the adapter and the caller may be the WS thread."""
    def _close():
        try:
            agent.close()
        except Exception as exc:  # noqa: BLE001
            _log(f"stop turn={turn_id[:8]}: could not close the ACP agent: {exc}")
    threading.Thread(target=_close, daemon=True, name=f"stop-{turn_id[:8]}").start()


def request_stop(turn_id: str, why: str = "") -> bool:
    """A person asked to stop this turn. True if it is running HERE.

    Idempotent and cheap, because it is called again on every heartbeat until
    the server sees the turn finish: the first call interrupts politely (ACP
    `session/cancel`, SIGTERM to `claude -p`); one arriving STOP_ESCALATE_SECONDS
    later escalates (close the adapter, SIGKILL). Never raises, never blocks.
    """
    turn_id = str(turn_id or "")
    with _TURNS_LOCK:
        if turn_id not in _IN_FLIGHT:
            return False
    now = time.time()
    with _STOP_LOCK:
        first_at = _STOP_REQUESTED.setdefault(turn_id, now)
        proc = _CLI_PROCS.get(turn_id)
    escalate = now - first_at >= STOP_ESCALATE_SECONDS
    if first_at == now:
        _log(f"stop requested for turn {turn_id[:8]}" + (f" ({why})" if why else ""))
    agent = _acp_agent_for(turn_id)
    if agent is not None:
        if escalate:
            _log(f"turn {turn_id[:8]} ignored its stop for {now - first_at:.0f}s; closing it")
            _close_acp_later(turn_id, agent)
        else:
            stop_turn(turn_id)
    if proc is not None:
        _signal_cli(turn_id, proc, escalate=escalate)
    return True


def stop_session(session_key: str, why: str = "") -> list[str]:
    """A session-shaped stop: stop whichever in-flight turn drives that CLI
    session. Returns the turn ids it reached."""
    if not session_key:
        return []
    with _STOP_LOCK:
        hits = [tid for tid, sid in _TURN_SESSIONS.items() if sid == session_key]
    return [tid for tid in hits if request_stop(tid, why or "session stop")]


def honour_stops(reply) -> None:
    """Act on `cancel_turn_ids` in a heartbeat reply (REST RunnerOut or WS ack)."""
    if not isinstance(reply, dict):
        return
    for tid in reply.get("cancel_turn_ids") or []:
        request_stop(str(tid), "heartbeat")


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
    core = _acp_core()
    if core is None:
        _log("warn: canopy_acp unavailable; the ACP executor will fall back to claude -p")
        return
    # The install itself is canopy_acp's, shared with the laptop runner.
    prefix = pathlib.Path.home() / ".local"
    outcome = core.install_adapter(prefix)
    if outcome == "installed":
        _log(f"installed {ACP_ADAPTER_PACKAGE} into {prefix}")
    elif outcome == "present":
        _log(f"ACP adapter already installed under {prefix}")
    elif outcome == "no-node":
        _log("warn: node/npm not on PATH; the ACP executor will fall back to claude -p")
    else:
        _log(f"warn: could not install the ACP adapter ({outcome}); "
             "the ACP executor will fall back to claude -p")


#: The whole agent bootstrap (bootstrap_agents.sh) may take this long.
BOOTSTRAP_TIMEOUT_SECONDS = 900
#: How long the bootstrap waits for canopy-web to accept this box's token before it
#: goes ahead anyway (a refusal a person must fix — a revoked key — would otherwise
#: hold the box forever), and how often it asks meanwhile.
BOOTSTRAP_AUTH_WAIT_SECONDS = int(os.environ.get("BOOTSTRAP_AUTH_WAIT_SECONDS", "1800"))
BOOTSTRAP_AUTH_POLL_SECONDS = 30


def _signed_in() -> bool:
    """Does canopy-web accept this box's token right now? One cheap runner-protocol
    read (`_api` already retries a 401 once with a fresh runner token). Anything but
    a refusal — a network blip, a 5xx — is not a reason to hold the bootstrap."""
    if not _RUNNER_ID:
        return True
    status, _ = _api("GET", f"/runners/{_RUNNER_ID}/agents")
    return status not in (401, 403)


def _await_signed_in(why: str, *, max_wait: float | None = None,
                     poll: float | None = None) -> bool:
    """Block until canopy-web accepts this box's token, or `max_wait` passes. True
    when accepted. Logs once on entering the wait and once on leaving it."""
    max_wait = BOOTSTRAP_AUTH_WAIT_SECONDS if max_wait is None else max_wait
    poll = BOOTSTRAP_AUTH_POLL_SECONDS if poll is None else poll
    if _signed_in():
        return True
    _log(f"canopy-web refuses this box's token — waiting up to {int(max_wait // 60)} min "
         f"for it {why} (a device key waiting on approval, or a deploy in progress)")
    deadline = time.monotonic() + max_wait
    while not _stop and time.monotonic() < deadline:
        time.sleep(poll)
        if _signed_in():
            _log(f"canopy-web accepts this box's token — going on {why}")
            return True
    _log(f"canopy-web still refuses this box's token — going on {why} anyway")
    return False


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
    global _BOOTSTRAPPED_AT, _HEALTH_SLOW_AT
    try:
        # Every agent's prepare reads its credentials and posts its report as this
        # box, so a bootstrap run while canopy-web refuses the box's token fails for
        # the WHOLE fleet at once (2026-10-18 02:28: 16 minutes refused, and every
        # agent's prepare inside it). Wait for a token canopy-web accepts first; and
        # if it was refused DURING the pass, run the pass again once it is back.
        _await_signed_in("before the agent bootstrap")
        _run_bootstrap()
        if not _signed_in():
            _log("canopy-web refused this box's token during the agent bootstrap — "
                 "every agent's prepare may have failed; re-running it once the token is "
                 "accepted again")
            _set_check("bootstrap", "warn", "canopy-web refused this box's token during the "
                       "agent bootstrap; waiting to re-run it")
            if _await_signed_in("to re-run the agent bootstrap"):
                _run_bootstrap()
        _github_readiness()
        _install_repo_worktree_shim()
        _guard_shared_clone(pathlib.Path(CANOPY_WEB_REPO_DIR))
        if secrets_off_disk():
            # The bootstrap above wrote no secret files; the ones an older runner
            # wrote are only a liability now (spec §4).
            remove_legacy_secret_files([s for s in _known_agent_slugs() if off_disk_proven(s)])
    finally:
        # Stamped however it went: this is "a bootstrap was ATTEMPTED at", which
        # is what discharges a refresh request. The checks say how it went.
        _BOOTSTRAPPED_AT = time.time()
        # The bootstrap may have installed a newer canopy CLI (the off-disk gate).
        _forget_cli_version()
        # Versions may have just moved (bootstrap updates Claude Code and the
        # canopy CLI): re-read them on the next beat rather than in 10 minutes.
        _HEALTH_SLOW_AT = 0.0


#: Where the auto-updater learns this runner keeps secrets off disk, so its
#: credentials pass runs `canopy agent prepare` the same way (no env files).
TURN_ENV_ROOT_MARKER = pathlib.Path(os.environ.get(
    "TURN_ENV_ROOT_MARKER", str(IN_FLIGHT_FILE.with_name("turn-env-root"))))


def _off_disk_env(env: dict) -> dict:
    """`env` for a child that prepares agents: with secrets off disk, the tmpfs root
    (`CANOPY_TURN_ENV_ROOT`) that makes `canopy agent prepare --runner` write no
    `~/.<slug>/.env`, `<clone>/.env` or `.gws-sa-key.json`. Records the root for the
    updater. Unchanged on the legacy path."""
    if not secrets_off_disk():
        try:
            TURN_ENV_ROOT_MARKER.unlink(missing_ok=True)
        except OSError:
            pass
        return env
    root = _turn_env_root()
    if root is None:
        return env
    try:
        TURN_ENV_ROOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        TURN_ENV_ROOT_MARKER.write_text(str(root) + "\n")
    except OSError:
        pass
    return {**env, "CANOPY_TURN_ENV_ROOT": str(root)}


def _canopy_cli() -> str:
    """The canopy CLI bootstrap installs with `uv tool` (~/.local/bin), even when the
    service's PATH does not carry that directory."""
    local = str(pathlib.Path.home() / ".local" / "bin")
    path = os.environ.get("PATH", "")
    return shutil.which("canopy", path=f"{path}:{local}" if path else local) or f"{local}/canopy"


def prepare_agent(slug: str) -> None:
    """Prepare ONE agent with `canopy agent prepare --runner` — the lazy path for an agent
    this box has never seen. Only that agent, not the fleet: a new agent's first turn waits
    for itself, not for every other agent's clone, plugins and Gmail check. Best-effort,
    like the fleet bootstrap; the agent's readiness report says what it could do."""
    env = _off_disk_env(dict(os.environ))
    env.setdefault("AGENT_ROOT", AGENT_ROOT)
    # bootstrap_agents.sh and `canopy agent prepare --runner` read the agent
    # roster and credentials AS THIS BOX: hand them its own token, not a PAT.
    env["CANOPY_TOKEN"] = _bearer(min_life=900)
    try:
        rc, tail = _run_teed([_canopy_cli(), "agent", "prepare", "--runner", "--slug", slug],
                             env=env, timeout=900)
        _set_check(f"prepare.{slug}", "ok" if rc == 0 else "warn",
                   "" if rc == 0 else f"canopy agent prepare exited {rc}: " + " | ".join(tail))
    except Exception as exc:  # noqa: BLE001 — never block the claim loop
        _log(f"warn: canopy agent prepare {slug} did not run: {exc}")
        _set_check(f"prepare.{slug}", "fail", f"canopy agent prepare did not run: {exc}")


#: What `_ensure_agent_clone` runs, given the agent's slug. A name of its own so the
#: test loader can swap in a no-op: no test may start a real bootstrap (it clones
#: private repos).
_LAZY_BOOTSTRAPPER = prepare_agent


#: This box's runner id, once paired — for the few calls made outside a loop
#: that already carries it (the GitHub readiness check after a bootstrap).
_RUNNER_ID = ""


def _github_readiness() -> None:
    """Can each agent this box serves open a pull request? Asked of canopy-web,
    which checks the owner's token against GitHub right now — so a missing,
    expired or under-scoped grant turns a health check red at boot instead of
    surfacing as a 403 halfway through a turn. One check per agent."""
    if not _RUNNER_ID:
        return
    status, rows = _api("GET", f"/runners/{_RUNNER_ID}/github-readiness")
    if status != 200 or not isinstance(rows, list):
        _set_check("github", "warn", f"could not ask canopy-web about GitHub (HTTP {status})")
        return
    for row in rows:
        slug = row.get("agent_slug") or "?"
        _set_check(f"github.{slug}", row.get("status") or "warn", row.get("detail") or "")
        _log(f"github {slug}: {row.get('status')} — {row.get('detail')}")


def _run_teed(cmd: list[str], *, env: dict, timeout: float, keep: int = 4) -> tuple[int, list[str]]:
    """Run `cmd`, passing every output line through to our own stdout (so it
    still lands in `journalctl -u canopy-runner` exactly as before) while
    keeping the last `keep` non-empty lines for the health check."""
    import collections

    tail: collections.deque = collections.deque(maxlen=keep)
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    # A watchdog rather than a check per line: a bootstrap that hangs SILENTLY
    # never yields a line to check on.
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line.strip():
                tail.append(line.strip()[:300])
        rc = proc.wait()
    finally:
        watchdog.cancel()
    if timed_out.is_set():
        tail.append(f"killed after {int(timeout)}s")
    return rc, list(tail)


def ensure_updater_clone() -> bool:
    """Make sure CANOPY_WEB_REPO_DIR is a clone: the auto-updater reads
    update_runner.sh from it (`git show origin/main:...` in the cloud-init shim).

    Nothing created it once the runner moved to its own clone (RUNNER_SRC_DIR):
    cloud-init only makes the empty directory, so a box built after that logged
    "no update_runner.sh in /opt/canopy-web yet; skipping" on every tick and
    stayed on its build-day code while looking current enough — cloud-ec2-2,
    2026-10-05. Only boxes old enough to have the clone could update. Reported as
    the `updater_clone` check, so a box that cannot update says so."""
    repo = pathlib.Path(CANOPY_WEB_REPO_DIR)
    if (repo / ".git").is_dir():
        # Move origin/main, which is what the shim runs. The updater fetches the
        # deployed commit BY SHA, which never moves origin/main, so a box kept
        # running whatever update_runner.sh main held when it was last fetched by
        # name — cloud-ec2-2 ran 2026-10-05's for a day, missing #1231. The runner
        # restarts on every update, so this keeps the updater current too.
        try:
            subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"],
                           check=True, timeout=120, capture_output=True)
        except Exception as exc:  # noqa: BLE001 — a stale updater beats no runner
            _log(f"warn: could not refresh origin/main in {repo}: {exc}")
        _set_check("updater_clone", "ok", f"{repo}")
        return True
    if repo.exists() and any(repo.iterdir()):
        _set_check("updater_clone", "fail",
                   f"{repo} is not a clone and not empty — the auto-updater cannot run")
        return False
    repo.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "clone", "--quiet", CANOPY_WEB_REPO_URL, str(repo)],
                       check=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: could not clone {CANOPY_WEB_REPO_URL} to {repo}: {exc}")
        _set_check("updater_clone", "fail", f"no clone at {repo} ({exc}); this box will not auto-update")
        return False
    _log(f"cloned {CANOPY_WEB_REPO_URL} to {repo} for the auto-updater")
    _set_check("updater_clone", "ok", f"{repo} (cloned)")
    return True


def _run_bootstrap() -> None:
    ensure_updater_clone()
    if not sync_runner_src():
        _set_check("bootstrap", "fail", f"no runner clone at {RUNNER_SRC_DIR}; agent bootstrap skipped")
        return
    script = pathlib.Path(RUNNER_SRC_DIR) / "runner" / "ec2" / "bootstrap_agents.sh"
    if not script.exists():
        _log(f"warn: {script} not found — skipping agent bootstrap")
        _set_check("bootstrap", "fail", f"{script} not found")
        return
    env = _off_disk_env(dict(os.environ))
    env.setdefault("AGENT_ROOT", AGENT_ROOT)
    # The roster read and every `canopy agent prepare` it runs act AS THIS BOX, as
    # prepare_agent's do — including each agent's readiness report, a WRITE that
    # canopy does not retry as the box. Inheriting os.environ handed them
    # runner.env's PAT, revoked on 2026-10-17: the roster came back empty and every
    # report 401'd. The child cannot renew the token, so it gets one that outlives it.
    bearer = _bearer(min_life=BOOTSTRAP_TIMEOUT_SECONDS)
    if bearer:
        env["CANOPY_TOKEN"] = bearer
    _log(f"running {script}")
    try:
        rc, tail = _run_teed(["bash", str(script)], env=env, timeout=BOOTSTRAP_TIMEOUT_SECONDS)
        _log(f"bootstrap_agents.sh exited {rc}")
        # The last lines carry the reason. "exited 1" alone still sent someone
        # to journald on 2026-09-22 (`line 1024: ace: unbound variable`), which
        # is the trip this check exists to save.
        _set_check("bootstrap", *(("ok", "") if rc == 0 else
                   ("fail", f"bootstrap_agents.sh exited {rc}: " + " | ".join(tail))))
    except Exception as exc:
        _log(f"warn: bootstrap_agents.sh failed to run: {exc}")
        _set_check("bootstrap", "fail", f"bootstrap_agents.sh did not run: {exc}")


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

#: The box's OWN full Claude login — `claude auth login`, kept in the CLI's
#: credentials file on this box. It is the only kind of login that carries the
#: claude.ai connectors (Claude Docs and the rest): a setup-token is
#: inference-only by design (the CLI says so — "Long-lived tokens … are limited
#: to inference-only"), so a turn running on one cannot see a connector at all,
#: however the doc is shared. Read out of `claude` 2.1.300, 2026-10-14: the
#: connector loader refuses with "Missing user:mcp_servers scope".
#:
#: It is NOT a value in the bundle. The entry carries no env var; selecting it
#: CLEARS both auth vars, so the CLI falls through to its stored login. Its
#: refresh token rotates on every refresh, so it lives on exactly this box and
#: never travels to canopy-web — a copied rotating token breaks both copies.
BOX_LOGIN_LABEL = "box-login"
_BOX_LOGIN_SCOPE = "user:mcp_servers"
_BUNDLE_SLOTS = (
    ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "claude_token"),
    ("subscription-2", "CLAUDE_CODE_OAUTH_TOKEN", "claude_token_secondary"),
    ("api-key", "ANTHROPIC_API_KEY", "claude_api_key"),
)


#: A box holds as many full logins as a person adds on its credentials page,
#: each in ITS OWN config directory: the CLI keeps one login per directory, and
#: turns run concurrently, so swapping one credentials file in and out would hand
#: a running turn's refresh to the wrong account. `default` is the CLI's own
#: directory (where the one login lived before there was a list); every other
#: login is `~/.claude-logins/<key>`, with everything but its credentials and
#: state symlinked back to the default directory, so transcripts, plugins and
#: settings stay in one place whichever login a turn ran on.
DEFAULT_LOGIN = "default"
#: The CLI's own config dir as this process found it — the default login's home.
#: Captured once: selecting a login rewrites CLAUDE_CONFIG_DIR in this process.
_BASE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR", "")
_LOGINS_ROOT = pathlib.Path(
    os.environ.get("CLAUDE_LOGINS_ROOT") or (pathlib.Path.home() / ".claude-logins"))
#: Written once canopy-web holds the default login in its list. After that, the
#: default login missing from the list means a person REMOVED it — before, it
#: means the list has not heard of it yet.
_DEFAULT_ADOPTED_MARKER = ".default-adopted"
_LOGIN_KEY_RE = re.compile(r"^[a-z0-9]{1,32}$")
#: Not ours to link into a login's directory: its own login and its own state.
_LOGIN_PRIVATE = (".credentials.json", ".claude.json")


def _login_dir(key: str = DEFAULT_LOGIN) -> pathlib.Path:
    if key == DEFAULT_LOGIN:
        return pathlib.Path(_BASE_CONFIG_DIR) if _BASE_CONFIG_DIR else pathlib.Path.home() / ".claude"
    return _LOGINS_ROOT / key


def _login_config_env(key: str = DEFAULT_LOGIN) -> str:
    """CLAUDE_CONFIG_DIR for a login; "" means unset (the CLI's own default,
    whose state file sits BESIDE ~/.claude, not in it)."""
    return _BASE_CONFIG_DIR if key == DEFAULT_LOGIN else str(_login_dir(key))


def _login_label(key: str) -> str:
    return BOX_LOGIN_LABEL if key == DEFAULT_LOGIN else f"{BOX_LOGIN_LABEL}:{key}"


def _is_login(label: str) -> bool:
    return label == BOX_LOGIN_LABEL or label.startswith(BOX_LOGIN_LABEL + ":")


def _login_key_of(label: str) -> str:
    return label.partition(":")[2] or DEFAULT_LOGIN


def _claude_credentials_file(key: str = DEFAULT_LOGIN) -> pathlib.Path:
    """Where the CLI keeps a stored login on Linux (no keychain on the box)."""
    return _login_dir(key) / ".credentials.json"


def _prepare_login_dir(key: str) -> pathlib.Path:
    """Make a login's directory: its own credentials and state, the rest linked.

    Re-run on every reconcile, so something the default directory grows later (a
    new plugin cache) shows up in every login. `projects` is made first: it is
    where transcripts land, and a login that made its own would put a turn's
    transcript where nothing reads it.
    """
    d = _login_dir(key)
    if key == DEFAULT_LOGIN:
        return d
    base = _login_dir(DEFAULT_LOGIN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "projects").mkdir(exist_ok=True)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in base.iterdir():
        link = d / entry.name
        if entry.name in _LOGIN_PRIVATE or link.exists() or link.is_symlink():
            continue
        link.symlink_to(entry)
    state = d / ".claude.json"
    if not state.exists():
        # Onboarding, trust and MCP config carry over; whose login it is does not.
        try:
            data = json.loads(_claude_state_file().read_text())
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            data.pop("oauthAccount", None)
        else:
            data = {}
        state.write_text(json.dumps(data))
        state.chmod(0o600)
    return d


def box_login_ready(path: pathlib.Path | None = None, *, key: str = DEFAULT_LOGIN) -> bool:
    """True when this box holds a full Claude login that carries the connectors.

    Both halves matter: a refresh token (so it survives its first hour) and the
    connector scope (an older or narrower login would sit first in the cascade
    and still not see Claude Docs, which is the only reason it is first).
    """
    try:
        data = json.loads((path or _claude_credentials_file(key)).read_text())
    except (OSError, ValueError):
        return False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return False
    return _BOX_LOGIN_SCOPE in (oauth.get("scopes") or [])


def _claude_state_file(key: str = DEFAULT_LOGIN) -> pathlib.Path:
    """The CLI's state file: `$CLAUDE_CONFIG_DIR/.claude.json`, else `~/.claude.json`
    (beside `~/.claude/`, not in it)."""
    root = _login_config_env(key)
    return pathlib.Path(root) / ".claude.json" if root else pathlib.Path.home() / ".claude.json"


def box_login_account(path: pathlib.Path | None = None, *, key: str = DEFAULT_LOGIN) -> str:
    """The account the box's own login belongs to, as `claude auth login` recorded it.

    This is what the credentials page shows on the sign-in row INSTEAD of a name
    an operator types: the box knows who it signed in as, and a hand-typed label
    is exactly how a row came to say one account while holding another
    (2026-10-14). Empty when the CLI has not recorded one.
    """
    try:
        data = json.loads((path or _claude_state_file(key)).read_text())
    except (OSError, ValueError):
        return ""
    acct = data.get("oauthAccount") if isinstance(data, dict) else None
    email = acct.get("emailAddress") if isinstance(acct, dict) else None
    return email.strip() if isinstance(email, str) else ""


def _default_adopted() -> bool:
    return (_LOGINS_ROOT / _DEFAULT_ADOPTED_MARKER).exists()


def _login_keys(cred: dict | None) -> list[str]:
    """The logins this box should use, in order.

    From canopy-web's list. A bundle with no list at all is an older canopy-web:
    the default login, as before. The default login missing from a list that
    has never held it is one signed in before there was a list — kept first,
    where it always sat, and reported so the list adopts it.
    """
    if not isinstance(cred, dict) or "logins" not in cred:
        return [DEFAULT_LOGIN]
    keys = [str(lg.get("key", "")) for lg in cred.get("logins") or [] if isinstance(lg, dict)]
    keys = [k for k in keys if k == DEFAULT_LOGIN or _LOGIN_KEY_RE.match(k)]
    if DEFAULT_LOGIN not in keys and not _default_adopted():
        keys.insert(0, DEFAULT_LOGIN)
    return keys


def _cascade_from(cred: dict | None) -> list[tuple[str, str, str]]:
    """The ordered credential list: the box's own full logins first, in the
    order a person set (they are the only entries that see claude.ai
    connectors), then the bundle's backup tokens."""
    out: list[tuple[str, str, str]] = [
        (_login_label(k), "CLAUDE_CONFIG_DIR", _login_config_env(k))
        for k in _login_keys(cred) if box_login_ready(key=k)]
    for label, var, key in _BUNDLE_SLOTS:
        if (cred or {}).get(key):
            out.append((label, var, cred[key]))
    return out


def _adopt_box_login() -> bool:
    """Put a freshly signed-in DEFAULT login at the head of the live cascade
    (an older canopy-web, which has no list to re-read it from)."""
    if not box_login_ready():
        return False
    if not any(c[0] == BOX_LOGIN_LABEL for c in _CLAUDE_CREDS):
        _CLAUDE_CREDS.insert(0, (BOX_LOGIN_LABEL, "CLAUDE_CONFIG_DIR", _login_config_env()))
    _apply_claude_credential(0)
    _reprobe([BOX_LOGIN_LABEL])
    return True


#: What this box last told canopy-web about its logins, so an unchanged state
#: is not re-posted every re-read.
_LAST_LOGIN_REPORT: object = None
#: The login a sign-in is filling right now — never cleaned up mid-sign-in.
_MINT_LOGIN_KEY = ""


def _reconcile_logins(runner_id: str, cred: dict | None) -> None:
    """Make the box's logins match canopy-web's list, then say how they stand.

    A login a person removed is signed out here: its directory is deleted (the
    default login keeps its directory, which holds everything else, and loses
    only its credentials). Then the box reports which listed logins are signed
    in and as whom, which is all the credentials page knows about them.
    """
    global _LAST_LOGIN_REPORT
    if not isinstance(cred, dict) or "logins" not in cred:
        return  # an older canopy-web: no list to reconcile against
    listed = [str(lg.get("key", "")) for lg in cred.get("logins") or [] if isinstance(lg, dict)]
    listed = [k for k in listed if k == DEFAULT_LOGIN or _LOGIN_KEY_RE.match(k)]
    try:
        _LOGINS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = _LOGINS_ROOT / _DEFAULT_ADOPTED_MARKER
        if DEFAULT_LOGIN in listed:
            marker.touch()
        elif marker.exists() and _MINT_LOGIN_KEY != DEFAULT_LOGIN:
            creds = _claude_credentials_file(DEFAULT_LOGIN)
            if creds.exists():
                creds.unlink()
                _log("removed on canopy-web: signed the default login out of this box")
        for d in _LOGINS_ROOT.iterdir():
            if d.name.startswith(".") or d.name in listed or d.name == _MINT_LOGIN_KEY:
                continue
            if d.is_dir() and not d.is_symlink():
                shutil.rmtree(d, ignore_errors=True)
            else:
                d.unlink()
            _log(f"removed on canopy-web: signed login {d.name} out of this box")
        for k in listed:
            if k != DEFAULT_LOGIN and _login_dir(k).is_dir():
                _prepare_login_dir(k)
    except OSError as exc:
        _log(f"warn: could not reconcile this box's logins ({exc})")
    adopt = DEFAULT_LOGIN not in listed and not _default_adopted() and box_login_ready()
    report = {
        "adopt_default": adopt,
        "logins": [{"key": k, "signed_in": box_login_ready(key=k),
                    "account": box_login_account(key=k)}
                   for k in ([DEFAULT_LOGIN] if adopt else []) + listed],
    }
    if report == _LAST_LOGIN_REPORT:
        return
    try:
        status, _ = _api("POST", f"/runners/{runner_id}/logins/report", report)
    except Exception as exc:  # noqa: BLE001 — a report is never worth a turn
        _log(f"warn: could not report this box's logins ({exc})")
        return
    if status == 200:
        _LAST_LOGIN_REPORT = report
        if adopt:
            (_LOGINS_ROOT / _DEFAULT_ADOPTED_MARKER).touch()

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
    # The CLI's wording when its stored claude.ai login can no longer refresh.
    # cloud-ec2-2, 2026-10-15 (turn 9fbe2240, failure investigation 5): echo's
    # scheduled turn died on exactly this, and none of the markers above matched.
    # So the box sent no auth-required alert, the turn note never said "not signed
    # in", and the only trace was a bare "Authentication required".
    "oauth session expired",
)

#: The ACP executor's own failure note when the adapter refuses the session with
#: ACP's `auth_required` error. It is matched with the runner's prefix, never as
#: a bare phrase: "authentication required" is ordinary prose an agent writes
#: about any API, and that must not mark a box dead. The prefixed form is written
#: only by `_execute_once_acp`'s except branch, never by an agent (investigation 5).
_ACP_AUTH_REQUIRED_NOTE = "runner error (acp): authentication required"


def _is_usage_cap(text: str) -> bool:
    low = (text or "").lower()
    return bool(_USAGE_CAP_RE.search(low)) or any(m in low for m in _USAGE_CAP_MARKERS)


def _is_auth_required(text: str) -> bool:
    """True when the credential needs a human to sign in again.

    Checked ALONGSIDE `_is_usage_cap`, never instead of it — the two states are
    disjoint and want opposite responses (wait vs. fetch a person).
    """
    low = (text or "").lower()
    return (any(m in low for m in _AUTH_REQUIRED_MARKERS)
            or low.lstrip().startswith(_ACP_AUTH_REQUIRED_NOTE))


def _is_bare_acp_refusal(text: str) -> bool:
    """True when the only sign of a dead credential is ACP's bare `auth_required`.

    The ACP adapter answers it for a token that is out of usage as well as for one
    that is signed out, so on its own it does not say which (investigation 9). The
    CLI's own sentences (`_AUTH_REQUIRED_MARKERS`) do say, and win.
    """
    low = (text or "").lower()
    return (low.lstrip().startswith(_ACP_AUTH_REQUIRED_NOTE)
            and not any(m in low for m in _AUTH_REQUIRED_MARKERS))


#: turn id -> the reset (epoch s) the ACP adapter reported in `_claude/rateLimit`
#: meta. Exact where the text form is a parse; popped by `execute_prompt`.
_CAP_RESET_HINT: dict[str, float] = {}

#: How long to park when no cap named a reset we could read. Short on purpose: a
#: box parked too long is idle for nothing, one parked too briefly just caps again
#: on its next turn and re-parks — with, by then, a message we may be able to read.
CAP_PAUSE_FALLBACK_SECONDS = 30 * 60


def _cap_reset_epoch(text: str) -> float | None:
    """The reset a cap message names, as epoch seconds — or None. The parser lives
    in canopy_transcript (shared with the laptop runner); without it this box
    still parks, for the fallback window."""
    core = _transcript_core()
    if core is None:
        return None
    try:
        from canopy_transcript.usage_limit import reset_at  # noqa: PLC0415
        when = reset_at(text)
    except Exception:  # noqa: BLE001 — a misread reset must not fail the turn
        return None
    return when.timestamp() if when else None


def _park_until_cap_resets(resets: list[float], text: str, turn_id: str) -> str:
    """Every credential on this box is capped: pause the runner on canopy-web and
    schedule the unpause for the EARLIEST reset among them — the first moment any
    login can work again. Routing then sends this box's work elsewhere instead of
    feeding turns into the wall. Returns the reset as text for the turn's note.

    The cascade goes back to the primary: when the pause lifts, the login that
    capped first is the likeliest to have reset, and starting from the last one
    would only rediscover its cap.
    """
    until = min(resets) if resets else time.time() + CAP_PAUSE_FALLBACK_SECONDS
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
    first = next((ln.strip() for ln in (text or "").splitlines() if _is_usage_cap(ln)), "")
    note = (f"{SELF_PAUSE_PREFIX}Claude usage cap — resumes {stamp}"
            + ("" if resets else " (reset unreadable; retrying then)")
            + (f": {first}" if first else ""))[:200]
    if _RUNNER_ID:
        try:
            status, _ = _api("POST", f"/runners/{_RUNNER_ID}/pause",
                             {"note": note, "until": stamp})
            if status == 200:
                _SELF_PAUSED["set"] = True
                _log(f"turn {turn_id[:8]}: every credential capped — paused until {stamp}")
            else:
                _log(f"warn: pausing for the usage cap returned {status}")
        except Exception as exc:  # noqa: BLE001 — the turn's failure still reports
            _log(f"warn: could not pause for the usage cap ({exc})")
    if _CLAUDE_CREDS:
        _apply_claude_credential(0)
    return stamp


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
    # Every entry starts from the CLI's own directory; a login entry then points
    # it at that login's. "" is the default directory, which means UNSET.
    if _BASE_CONFIG_DIR:
        os.environ["CLAUDE_CONFIG_DIR"] = _BASE_CONFIG_DIR
    else:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    if var and value:
        os.environ[var] = value
    _log(f"claude credential: using {label} ({var or 'the stored login on this box'})")


# ── the credential probe ─────────────────────────────────────────────────────
# "Ready" used to mean "a value is stored". On 2026-10-18 cloud-ec2-fork read
# ready with one setup-token that was at its weekly cap, and every turn it took
# failed as "not signed in". So each credential is asked one short question with
# no tools, and what it answers is what the runner page and readiness report.
#
# Probes spend real usage, so they run only when a credential is staged or
# changes, when a capped one's reset has passed, and when a turn's own failure is
# ambiguous (a bare ACP refusal). Never on a plain heartbeat.

#: How long one probe may take. A hung CLI gives `unknown`, which counts as
#: possibly-working: a probe must never be what keeps a box from serving.
PROBE_TIMEOUT_SECONDS = 90
PROBE_PROMPT = "Reply with OK"
#: Off switch for an operator (CLAUDE_PROBE=0): every probe answers `unknown`.
PROBE_ENABLED = _bool_env("CLAUDE_PROBE", True)
#: The latest probe per credential label: {label, status, detail, reset, at}.
_PROBES: dict[str, dict] = {}
_PROBES_LOCK = threading.Lock()


def _env_for_credential(entry: tuple[str, str, str]) -> dict:
    """The environment `_apply_claude_credential` would give this credential, as
    a COPY. Turns run concurrently, so a probe must never touch `os.environ`."""
    _label, var, value = entry
    env = _child_safe_env()
    for other in _CLAUDE_AUTH_VARS:
        env.pop(other, None)
    if _BASE_CONFIG_DIR:
        env["CLAUDE_CONFIG_DIR"] = _BASE_CONFIG_DIR
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    if var and value:
        env[var] = value
    return env


def _credential_env(index: int) -> dict:
    return _env_for_credential(_CLAUDE_CREDS[index])


def _probe_entry(entry: tuple[str, str, str]) -> dict:
    """Ask one credential one question. Never raises."""
    label = entry[0]
    out = {"label": label, "status": "unknown", "detail": "", "reset": None, "at": time.time()}
    if not PROBE_ENABLED:
        out["detail"] = "probing is off on this box (CLAUDE_PROBE=0)"
        return out
    try:
        with tempfile.TemporaryDirectory(prefix="claude-probe-") as scratch:
            proc = subprocess.run(
                [CLAUDE_BIN, "-p", PROBE_PROMPT, "--output-format", "json", "--max-turns", "1"],
                cwd=scratch, env=_env_for_credential(entry), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        out["detail"] = f"no answer within {PROBE_TIMEOUT_SECONDS}s"
        return out
    except Exception as exc:  # noqa: BLE001 — a probe that cannot run is unknown
        out["detail"] = f"the probe could not run ({exc})"[:300]
        return out
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    result, is_error = "", True
    try:
        doc = json.loads(stdout)
        if isinstance(doc, dict):
            result = str(doc.get("result") or "")
            is_error = bool(doc.get("is_error"))
    except ValueError:
        pass
    text = "\n".join(t for t in (result, stdout, stderr) if t)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if _is_usage_cap(text):
        out["status"] = "capped"
        out["detail"] = next((ln for ln in lines if _is_usage_cap(ln)), "")[:300]
        out["reset"] = _cap_reset_epoch(text)
    elif _is_auth_required(text):
        out["status"] = "dead"
        out["detail"] = next((ln for ln in lines if _is_auth_required(ln)), "")[:300]
    elif proc.returncode == 0 and not is_error:
        out["status"] = "ok"
        out["detail"] = "answered a prompt"
    else:
        out["detail"] = (f"exit {proc.returncode}: " + (lines[-1] if lines else "no output"))[:300]
    return out


def _probe_credential(index: int) -> dict:
    return _probe_entry(_CLAUDE_CREDS[index])


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _record_probe(result: dict) -> None:
    """Keep a probe's answer and publish it as the `claude.probe.<label>` check."""
    label, status = result["label"], result["status"]
    with _PROBES_LOCK:
        _PROBES[label] = dict(result)
    # When it was asked, at the end of every detail: wire.sh waits for a probe
    # taken after it staged, so a result left from an earlier staging cannot pass.
    when = f" · probed {_iso(result.get('at') or time.time())}"
    if status == "ok":
        _set_check(f"claude.probe.{label}", "ok", "answered a prompt" + when)
    elif status == "capped":
        reset = result.get("reset")
        _set_check(f"claude.probe.{label}", "warn",
                   (f"at its usage cap — resets {_iso(reset)}" if reset
                    else "at its usage cap (reset unreadable)") + when)
    elif status == "dead":
        _set_check(f"claude.probe.{label}", "fail",
                   "not signed in — someone has to sign it in again" + when)
    else:
        _set_check(f"claude.probe.{label}", "warn",
                   f"could not check: {result.get('detail') or 'no answer'}" + when)


def _record_outcome(label: str, status: str, *, detail: str = "", reset: float | None = None) -> None:
    """A turn's own outcome is a probe that cost nothing extra."""
    if label and label != "none":
        _record_probe({"label": label, "status": status, "detail": detail,
                       "reset": reset, "at": time.time()})


def _probe_all(labels: list[str] | None = None) -> None:
    """Probe the named credentials (all when None), one after another."""
    for entry in list(_CLAUDE_CREDS):
        if labels is None or entry[0] in labels:
            result = _probe_entry(entry)
            _record_probe(result)
            _log(f"claude credential probe: {entry[0]} {result['status']}"
                 + (f" ({result['detail']})" if result["detail"] else ""))


#: One probe thread at a time: two would spend usage asking the same question.
_PROBE_RUNNING = threading.Lock()


#: Labels waiting for the background prober. A request made while it runs is
#: queued here and picked up by the same thread, never dropped.
_PROBE_PENDING: set = set()


def _probe_job() -> None:
    while True:
        try:
            while True:
                with _PROBES_LOCK:
                    labels = sorted(_PROBE_PENDING)
                    _PROBE_PENDING.clear()
                if not labels:
                    break
                _probe_all(labels)
        except Exception as exc:  # noqa: BLE001 — a probe never takes the box down
            _log(f"warn: credential probe failed ({exc})")
        finally:
            _PROBE_RUNNING.release()
        # Something queued between the last drain and the release: go again.
        with _PROBES_LOCK:
            more = bool(_PROBE_PENDING)
        if not more or not _PROBE_RUNNING.acquire(blocking=False):
            return


def _probe_in_background(labels: list[str]) -> None:
    with _PROBES_LOCK:
        _PROBE_PENDING.update(labels)
    if not _PROBE_RUNNING.acquire(blocking=False):
        return   # the running prober drains the queue
    threading.Thread(target=_probe_job, daemon=True, name="claude-probe").start()


def _reprobe(labels: list[str]) -> None:
    """Forget what these credentials last proved and ask them again — after a
    person signs one in anew, which leaves its value (a config dir) unchanged."""
    with _PROBES_LOCK:
        for label in labels:
            _PROBES.pop(label, None)
    with _HEALTH_LOCK:
        for label in labels:
            _HEALTH.pop(f"claude.probe.{label}", None)
    _probe_in_background(labels)


def _probe_reset(p: dict) -> float:
    """When a capped credential is worth asking again: its reset, or the fallback
    window after the probe when the reset was unreadable."""
    return p.get("reset") or (p.get("at", 0.0) + CAP_PAUSE_FALLBACK_SECONDS)


def _credential_readiness(now: float | None = None) -> tuple[bool, str]:
    """(ready, note). Not ready only when EVERY staged credential has been probed
    and none is `ok`, `unknown`, or capped with its reset already passed — a box
    that might work is never held back on a guess."""
    now = time.time() if now is None else now
    staged = [c[0] for c in _CLAUDE_CREDS]
    if not staged:
        return True, ""
    with _PROBES_LOCK:
        probes = {lb: dict(_PROBES[lb]) for lb in staged if lb in _PROBES}
    if len(probes) < len(staged):
        return True, ""
    parts, resets = [], []
    for lb in staged:
        p = probes[lb]
        if p["status"] in ("ok", "unknown"):
            return True, ""
        if p["status"] == "capped":
            reset = _probe_reset(p)
            if reset <= now:
                return True, ""
            resets.append(reset)
            parts.append(f"{lb}: at its usage cap until {_iso(reset)}")
        else:
            parts.append(f"{lb}: not signed in")
    note = "no working Claude credential — " + "; ".join(parts)
    if len(resets) == len(staged):
        note += f"; earliest reset {_iso(min(resets))}"
    return False, note


#: Marks a pause THIS box set (credentials, or the usage-cap park). The box lifts
#: only a pause whose note starts with it; a person's pause is theirs to lift.
SELF_PAUSE_PREFIX = "[box] "
#: Whether the runner is under a pause this box set, as the last heartbeat reply
#: said (so a server-side scheduled wake clears it too).
_SELF_PAUSED = {"set": False}
#: The last heartbeat reply (REST RunnerOut or the WS ack): pause state rides it.
#: Empty when the last beat got no usable reply — then the pause is not touched.
_LAST_BEAT: dict = {}
#: The pause state the box last saw, and when it last saw it CHANGE. A pause or
#: unpause by anyone (a person included) is a decision; the box re-pauses only on
#: probe evidence newer than it.
_PAUSE_SEEN: dict = {"paused": None, "at": 0.0}
#: How often a credential proven dead is asked again — the only way a dead
#: credential fixed out of band (a token rotated by hand) is noticed on a box
#: that, being paused, runs no turns.
DEAD_REPROBE_SECONDS = int(os.environ.get("DEAD_REPROBE_SECONDS", "600"))


def _observe_beat(reply) -> None:
    """Take in one heartbeat reply. A reply without pause state (none, an error,
    an older server) leaves `_LAST_BEAT` empty, so the pause is left alone."""
    _LAST_BEAT.clear()
    if not isinstance(reply, dict) or "paused" not in reply:
        return
    _LAST_BEAT.update(reply)
    paused = bool(reply.get("paused"))
    _SELF_PAUSED["set"] = paused and str(reply.get("paused_note") or "").startswith(SELF_PAUSE_PREFIX)
    if _PAUSE_SEEN["paused"] is not None and _PAUSE_SEEN["paused"] != paused:
        _PAUSE_SEEN["at"] = time.time()
    _PAUSE_SEEN["paused"] = paused


def _sync_self_pause(beat: dict | None = None) -> None:
    """Step aside when no credential works; step back when one does.

    `ready` alone does not stop claims (it only lets a lower-ranked box go
    first), so a box whose probes find nothing working PAUSES itself — what the
    claim, the chat banner and turn_reach already understand. Until the earliest
    reset when every credential is capped; open-ended when any is dead.
    """
    if not _RUNNER_ID:
        return
    beat = _LAST_BEAT if beat is None else (beat or {})
    if "paused" not in beat:
        return   # no reply this beat: never guess at the pause
    paused = bool(beat.get("paused"))
    mine = str(beat.get("paused_note") or "").startswith(SELF_PAUSE_PREFIX)
    ready, note = _credential_readiness()
    try:
        if not ready:
            if paused:
                return   # already parked — by itself, or by a person whose note stays
            with _PROBES_LOCK:
                probes = [dict(_PROBES[c[0]]) for c in _CLAUDE_CREDS if c[0] in _PROBES]
            if any(p.get("at", 0.0) <= _PAUSE_SEEN["at"] for p in probes):
                # Someone (a person, or the clock) changed the pause after this
                # evidence was taken: their decision stands until a probe newer
                # than it says otherwise.
                return
            body: dict = {"note": (SELF_PAUSE_PREFIX + note)[:200]}
            if probes and all(p["status"] == "capped" for p in probes):
                body["until"] = _iso(min(_probe_reset(p) for p in probes))
            status, _ = _api("POST", f"/runners/{_RUNNER_ID}/pause", body)
            if status == 200:
                _SELF_PAUSED["set"] = True
                _log(f"paused: {note}")
            else:
                _log(f"warn: pausing for credentials returned {status}")
            return
        with _PROBES_LOCK:
            proven = any(p["status"] == "ok" for p in _PROBES.values())
        if paused and mine and proven:
            status, _ = _api("POST", f"/runners/{_RUNNER_ID}/unpause")
            if status == 200:
                _SELF_PAUSED["set"] = False
                _log("unpaused: a Claude credential answered a prompt again")
            else:
                _log(f"warn: unpausing after a working probe returned {status}")
    except Exception as exc:  # noqa: BLE001 — never take the main loop down
        _log(f"warn: could not sync the credential pause ({exc})")


_CRED_TICK_AT = 0.0


def _credential_tick(beat: dict | None = None) -> None:
    """The main loop's credential step, at most once per POLL_SECONDS: ask a
    capped credential again once its reset has passed, then pause or unpause."""
    global _CRED_TICK_AT
    now = time.monotonic()
    if _CRED_TICK_AT and now - _CRED_TICK_AT < POLL_SECONDS:
        return
    _CRED_TICK_AT = now
    wall = time.time()
    with _PROBES_LOCK:
        due = [lb for lb, p in _PROBES.items()
               if (p["status"] == "capped" and _probe_reset(p) <= wall
                   and p.get("at", 0.0) < _probe_reset(p))
               or (p["status"] == "dead" and wall - p.get("at", 0.0) >= DEAD_REPROBE_SECONDS)]
    if due:
        _probe_in_background(due)
    _sync_self_pause(beat)


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


def _promote_working_subscription(turn_id: str = "") -> None:
    """The fallback just ran a turn the primary could not: make it the primary.

    Without this the switch lived only in this process. Every restart (and the
    box restarts itself on every update) went back to the capped primary and
    spent one failed attempt rediscovering the cap, and the settings page kept
    calling the dead login "primary". Swapping on canopy-web makes the page show
    what the box is actually using, and puts the capped login second, where it is
    tried again once the new primary caps, which is roughly when its own weekly
    cap has had time to reset. Before this, the box went straight from the
    fallback to the metered API key and never looked at the primary again.

    Subscriptions only. The API key is last on purpose and never promoted.
    """
    # The box's logins at the head shift the subscriptions down.
    base = sum(1 for c in _CLAUDE_CREDS if _is_login(c[0]))
    if (not _CLAUDE_CRED_RUNNER_ID or _CLAUDE_CRED_I != base + 1
            or len(_CLAUDE_CREDS) < base + 2):
        return
    if ((_CLAUDE_CREDS[base][0], _CLAUDE_CREDS[base + 1][0])
            != ("subscription-1", "subscription-2")):
        return
    try:
        status, _ = _api("POST", f"/runners/{_CLAUDE_CRED_RUNNER_ID}/credential/swap")
    except Exception as exc:  # noqa: BLE001 — the turn already succeeded
        _log(f"warn: could not promote the working login ({exc})")
        return
    if status != 200:
        _log(f"warn: promoting the working login returned {status}")
        return
    # Mirror the swap locally so the periodic re-read sees no change (and does
    # not reset to index 0, which would now be the capped login again).
    (l1, v1, t1), (l2, v2, t2) = _CLAUDE_CREDS[base], _CLAUDE_CREDS[base + 1]
    _CLAUDE_CREDS[base], _CLAUDE_CREDS[base + 1] = (l1, v1, t2), (l2, v2, t1)
    # A probe is about a VALUE, and the values just changed slots.
    with _PROBES_LOCK:
        p1, p2 = _PROBES.pop(l1, None), _PROBES.pop(l2, None)
    for label, probe in ((l1, p2), (l2, p1)):
        if probe is not None:
            _record_probe({**probe, "label": label})
        else:
            with _HEALTH_LOCK:
                _HEALTH.pop(f"claude.probe.{label}", None)
    _apply_claude_credential(base)
    _log(f"turn {turn_id[:8]}: the fallback login worked while the primary is capped "
         "— swapped them on canopy-web")


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


def _notify_out_of_usage(capped: list[str], dead: list[str], until: str,
                         turn_id: str) -> None:
    """Tell canopy-web that every Claude credential on this box is out of usage.

    Running out is expected and clears on its own, but nothing on this box runs
    until it does, and before this the only sign was the runner quietly pausing.
    So it is an alert with the reset time in it: a person can see why the work
    stopped and when it starts again, without reading a turn's note
    (investigation 9). Best-effort and logged on failure, like its siblings.
    """
    summary = (f"{RUNNER_NAME or 'This runner'} is out of Claude usage: every "
               f"credential is at its cap ({', '.join(capped)}). It has paused itself "
               f"and resumes on its own at {until}.")
    if dead:
        summary += f" {', '.join(dead)} is also not signed in and needs a person."
    body = {
        "items": [{
            "source": "runner.credential",
            "kind": "claude_usage_exhausted",
            "level": "error",
            "summary": summary,
            # One row per runner per day, like the other credential alerts.
            "key": f"usage-exhausted:{RUNNER_NAME}",
        }],
    }
    try:
        status, _ = _api("POST", "/", body, prefix="/api/events")
        if status not in (200, 201):
            _log(f"warn: out-of-usage notify returned {status} — a human may not know "
                 "that this box has stopped until its caps reset")
    except Exception as exc:  # noqa: BLE001 — notifying must never break the turn
        _log(f"warn: could not notify that the box is out of usage ({exc})")


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


#: The bundle's `updated_at` as last read: a write that changes no value still
#: moves it, and is a request to prove the bundle again.
_BUNDLE_STAMP = {"updated_at": ""}


def _reload_claude_credentials() -> bool:
    """Re-read the credential bundle mid-run. True when it yielded something new.

    Only called once the in-memory cascade is spent, so the cost is paid exactly
    when a box is otherwise dead. Compares VALUES, not just count: rotating a
    capped subscription in place is the common rescue, and that leaves the length
    unchanged.
    """
    if not _CLAUDE_CRED_RUNNER_ID:
        return False
    before = [(c[0], c[2]) for c in _CLAUDE_CREDS]
    try:
        status, cred = _api("GET", f"/runners/{_CLAUDE_CRED_RUNNER_ID}/credential")
    except Exception as exc:  # noqa: BLE001 — a failed reload must not mask the cap
        _log(f"warn: could not re-read credentials ({exc})")
        return False
    if status != 200 or not cred:
        return False
    _reconcile_logins(_CLAUDE_CRED_RUNNER_ID, cred)
    fresh = _cascade_from(cred)
    restaged = (cred.get("updated_at") or "") != _BUNDLE_STAMP["updated_at"]
    _BUNDLE_STAMP["updated_at"] = cred.get("updated_at") or ""
    if not fresh or [(c[0], c[2]) for c in fresh] == before:
        if restaged and fresh and before:
            # Someone wrote the bundle again with the same values (wire.sh on a
            # running box): they are waiting to hear whether it works.
            _reprobe([c[0] for c in fresh])
        return False
    changed = [c[0] for c in fresh if (c[0], c[2]) not in before]
    with _PROBES_LOCK:
        for label in changed:
            _PROBES.pop(label, None)     # what the old value proved says nothing of the new
    with _HEALTH_LOCK:
        for label in changed:
            _HEALTH.pop(f"claude.probe.{label}", None)
    _CLAUDE_CREDS.clear()
    _CLAUDE_CREDS.extend(fresh)
    _apply_claude_credential(0)
    _log("re-read credential bundle: " + ", ".join(c[0] for c in _CLAUDE_CREDS))
    if changed:
        # In the background: this runs on the main loop, or mid-turn.
        _probe_in_background(changed)
    return True


#: What a box waiting for a Claude credential says on the runner page.
WAITING_FOR_CLAUDE_NOTE = (
    "needs a Claude sign-in: no long-lived token, and no working Claude login on this "
    "box. Sign in with Claude (or create a long-lived token) on the runner's credentials.")


def _beat_while_waiting(runner_id: str) -> None:
    """Heartbeat from the credential wait, as online but NOT ready.

    Silence here read as "stale" on the runner page (cloud-ec2-2, 2026-10-15),
    which says the box is down — when it was up and needed one thing, a sign-in.
    Not ready keeps routing off it; the note says what to do.
    """
    try:
        _api("POST", f"/runners/{runner_id}/heartbeat",
             _heartbeat_body([], ready=False, ready_note=WAITING_FOR_CLAUDE_NOTE))
    except Exception as exc:  # noqa: BLE001 — the wait itself must go on
        _log(f"warn: heartbeat while waiting for a credential failed ({exc})")


def fetch_and_stage_credential(runner_id: str) -> bool:
    """A CLOUD runner owns no secrets at boot beyond its PAT — it fetches its
    credential bundle from canopy-web (the per-runner hub) and stages it into the
    environment. Blocks (polling) until the Claude token is set, so the operator can
    provision the runner AFTER it has paired and appeared in the fleet. A laptop
    runner never does this — it uses emdash's ambient auth.
    """
    while not _stop:
        status, cred = _api("GET", f"/runners/{runner_id}/credential")
        if status == 200:
            _reconcile_logins(runner_id, cred)
        if status == 200 and (any(box_login_ready(key=k) for k in _login_keys(cred)) or (
                cred and (cred.get("claude_token") or cred.get("claude_api_key")))):
            global _CLAUDE_CRED_RUNNER_ID
            _CLAUDE_CRED_RUNNER_ID = runner_id
            _BUNDLE_STAMP["updated_at"] = (cred or {}).get("updated_at") or ""
            _CLAUDE_CREDS.clear()
            _CLAUDE_CREDS.extend(_cascade_from(cred))
            _apply_claude_credential(0)
            _log(f"claude credential cascade: {', '.join(c[0] for c in _CLAUDE_CREDS)}")
            if len(_CLAUDE_CREDS) == 1:
                _log("warn: only ONE Claude credential is set — a usage cap will stop "
                     "every agent on this box with nothing to fail over to "
                     "(`canopy runner credential` adds a fallback)")
            # There is no box-wide 1Password key any more, and canopy-web no
            # longer serves one. A key here would be staged into this process's
            # environment, which every turn inherits — one credential reading
            # EVERY agent's vault, handed to each of them. A turn gets its own
            # agent's key (`_agent_env`), and bootstrap reads each vault with
            # the key for that vault.
            _log("staged credential bundle from canopy-web (claude)")
            # Before the first ready heartbeat, so "ready" means a credential
            # answered. The box is not serving yet, so nothing waits on this; a
            # hung CLI costs PROBE_TIMEOUT_SECONDS and reads `unknown`.
            _probe_all()
            # Report them now. The next beat is otherwise the first after the
            # bootstrap (up to 40 min), and wire.sh is waiting on these checks.
            try:
                _, reply = _api("POST", f"/runners/{runner_id}/heartbeat", _heartbeat_body([]))
                _observe_beat(reply)
            except Exception as exc:  # noqa: BLE001 — the beat is a report, not a gate
                _log(f"warn: heartbeat after the credential probe failed ({exc})")
            return True
        _log("waiting for this runner's credential bundle to be set on canopy-web…")
        _beat_while_waiting(runner_id)
        # The browser sign-in is how a person gives this box a credential, and it
        # is served here too. A box whose only credential was its own login, and
        # whose login expired, restarts into this loop (cloud-ec2-2, 2026-10-15:
        # an auto-update restart left it dark for hours); if only the main loop
        # drained sign-ins, the one fix would never reach it. A finished sign-in
        # makes `box_login_ready` true, and the next pass stages it.
        _drain_mint(runner_id)
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


#: `claude auth login` — the FULL login (`--claudeai` = the subscription, not a
#: Console API key). Same authorize URL shape as setup-token and the same "Paste
#: code here if prompted" prompt under a pty (checked against `claude` 2.1.300,
#: 2026-10-14), so `MintSession` drives it unchanged up to the code; only what
#: success LOOKS like differs.
LOGIN_ARGV = ("claude", "auth", "login", "--claudeai")
_LOGIN_OK = re.compile(r"Login\s*successful", re.I)


def _login_env(key: str = DEFAULT_LOGIN) -> dict[str, str]:
    """The environment for `claude auth login`: no inherited Claude identity.

    Either auth var set here would make the CLI treat itself as already signed
    in by that credential, and the inherited session markers would make it
    behave as a child of whatever started the runner.
    """
    env = _child_safe_env()
    for var in _CLAUDE_AUTH_VARS:
        env.pop(var, None)
    # Into THIS login's directory, whichever login the runner is using now.
    config = _login_config_env(key)
    if config:
        env["CLAUDE_CONFIG_DIR"] = config
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


class LoginSession(MintSession):
    """A `claude auth login` run: the box keeps the credential it produces.

    The opposite contract from a setup-token mint, on purpose. A setup-token is
    a value we carry to canopy-web; this login's refresh token ROTATES each time
    the CLI refreshes it, so a copy anywhere else goes stale on first use and
    takes the box's own copy down with it. So nothing is read out of the
    terminal: success is the CLI saying so AND the credentials file now holding
    a login with the connector scope.
    """

    def __init__(self, key: str = DEFAULT_LOGIN) -> None:
        self.key = key
        _prepare_login_dir(key)
        super().__init__(argv=LOGIN_ARGV, env=_login_env(key))

    def submit_code(self, code: str, timeout: float = 90.0) -> str:
        if self._fd is None:
            raise MintTimeout("this sign-in session is not running")
        os.write(self._fd, code.strip().encode())
        time.sleep(_PASTE_SETTLE_SECONDS)
        os.write(self._fd, b"\r")
        said_ok = self._pump(
            timeout, lambda buf: True if _LOGIN_OK.search(strip_terminal(buf)) else None)
        tail = _diagnostic_tail(self._buf)
        # The CLI prints its verdict after writing the file, but give the write
        # a moment rather than racing it.
        deadline = time.time() + 10
        while said_ok and not box_login_ready(key=self.key) and time.time() < deadline:
            time.sleep(0.5)
        self.close()
        if not said_ok:
            raise MintTimeout(
                "claude auth login did not finish. What it said: " + (tail or "(nothing)"))
        if not box_login_ready(key=self.key):
            raise MintTimeout(
                "claude auth login said it succeeded, but this box has no stored "
                "login with the claude.ai connector scope (user:mcp_servers) — "
                "check which account signed in. What it said: " + (tail or "(nothing)"))
        return ""


#: The sign-in in flight on this box, if any. One at a time by construction —
#: canopy-web supersedes an unfinished mint when a new one is requested, so a
#: second concurrent pty would only be a leak.
_MINT_SESSION = None
_MINT_ID = ""

#: How often an idle box re-reads its credential bundle, so a swap or a pasted
#: token on the settings page takes effect without a restart.
CREDENTIAL_REREAD_SECONDS = 300
_CRED_REREAD_AT = 0.0


def _maybe_reread_credentials() -> None:
    global _CRED_REREAD_AT
    now = time.monotonic()
    if now - _CRED_REREAD_AT < CREDENTIAL_REREAD_SECONDS:
        return
    _CRED_REREAD_AT = now
    try:
        if _reload_claude_credentials():
            _log("credential bundle changed on canopy-web — using the new order")
    except Exception as exc:  # noqa: BLE001 — never take the main loop down
        _log(f"credential re-read error: {exc}")


def _drain_mint(runner_id: str) -> None:
    """Advance a browser-driven sign-in by one step, on the poll tick.

    POLLED rather than pushed, the same lesson as menu-answers: a control frame
    published while the WS channel is down reaches a group with no consumer and
    is silently dropped, while the runner keeps heartbeating and reads ONLINE.

    Every failure path reports back. A mint that dies quietly leaves a human
    watching a spinner for a URL that is never coming, which is worse than the
    terminal they were trying to avoid.
    """
    global _MINT_SESSION, _MINT_ID, _MINT_LOGIN_KEY, _LAST_LOGIN_REPORT
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
            _MINT_LOGIN_KEY = ""
            return
        mint_id, state = mint.get("id", ""), mint.get("status", "")

        if state == "requested":
            if _MINT_SESSION is not None:
                _MINT_SESSION.close()
            box = mint.get("slot") == "box"
            key = str(mint.get("login_key") or DEFAULT_LOGIN)
            if key != DEFAULT_LOGIN and not _LOGIN_KEY_RE.match(key):
                _api("POST", f"/runners/{runner_id}/mint/result",
                     {"detail": f"this box cannot sign in a login named {key!r}"})
                return
            _log(f"mint {mint_id[:8]}: starting "
                 f"{f'claude auth login for login {key} (kept on this box)' if box else 'claude setup-token'}")
            _MINT_LOGIN_KEY = key if box else ""
            try:
                _MINT_SESSION, _MINT_ID = (LoginSession(key) if box else MintSession()), mint_id
                url = _MINT_SESSION.start()
            except Exception as exc:  # noqa: BLE001 — always tell the waiting human
                _MINT_SESSION = None
                _MINT_ID = ""
                _MINT_LOGIN_KEY = ""
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
                if isinstance(_MINT_SESSION, LoginSession):
                    # Nothing to deliver: the login stays on this box (see
                    # LoginSession). Report success and whose it is, then take
                    # it into the cascade where the list puts it.
                    key = _MINT_SESSION.key
                    _api("POST", f"/runners/{runner_id}/mint/result",
                         {"logged_in": True, "account": box_login_account(key=key),
                          "detail": "signed in on the box — full claude.ai login, "
                                    "claude.ai connectors available to turns here"})
                    _log(f"mint {mint_id[:8]}: full login {key} stored on this box")
                    _LAST_LOGIN_REPORT = None
                    if _reload_claude_credentials():
                        _log("picked up the new login in the credential cascade")
                    elif key == DEFAULT_LOGIN and _adopt_box_login():
                        _log("box login is now first in the credential cascade")
                    # Its value (a config dir) did not change, so the re-read
                    # probes nothing: ask this login directly, whatever it
                    # proved before the sign-in.
                    _reprobe([_login_label(key)])
                    return
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
                _MINT_LOGIN_KEY = ""
    except Exception as exc:  # noqa: BLE001 — never take the main loop down
        _log(f"mint drain error: {exc}")


# ── inbox (the Gmail doorbell, and the timer that audits it) ───────────────
#
# Until now this box had no inbox code at all, so a `check_inbox` frame reached a
# runner that could not act on it. That was invisible because the LAPTOPS' 300s
# timer found the same mail a few minutes later and enqueued it — push looked
# slow rather than dead. canopy-web logged the truth all along:
#
#   gmail.push.missed x18, since 2026-08-01
#   "the 300s poll found mail that push never rang for — push is registered but
#    not delivering"
#
# The box HAS the credentials (`canopy agent prepare` imports each agent's gmail
# token at bootstrap, and a live `gog gmail search` succeeds here); it
# only ever lacked the code. So: same modules, same behaviour, same tests.
_INBOX_CORE: object = False


def _inbox_core():
    """`canopy_runner.inbox` + `.inbox_due`, imported lazily off the clone."""
    global _INBOX_CORE
    if _INBOX_CORE is not False:
        return _INBOX_CORE
    try:
        from canopy_runner import inbox, inbox_due  # noqa: PLC0415
        _INBOX_CORE = (inbox, inbox_due)
    except Exception as exc:  # noqa: BLE001
        _log(f"inbox reader unavailable ({exc}); this box will not read mail")
        _INBOX_CORE = None
    return _INBOX_CORE


class _InboxClient:
    """The one method `check_inbox` needs, over this runner's own `_api`.

    Deliberately a shim rather than a shared client class: the two runners differ in
    transport (this one has `_api`, the laptop has a Client object), and the
    thing worth sharing is the READING logic, not the plumbing under it.
    """

    def __init__(self, runner_id: str) -> None:
        self._runner_id = runner_id

    def enqueue_turn(self, agent_slug: str, origin: str, idempotency_key: str, *,
                     prompt: str = "", origin_ref: dict | None = None,
                     routing: str = "prefer_local") -> dict:
        status, payload = _api("POST", "/turns/", {
            "agent_slug": agent_slug, "origin": origin,
            "idempotency_key": idempotency_key, "prompt": prompt,
            "origin_ref": origin_ref or {}, "routing": routing,
        })
        # 201 = new, 200 = idempotent hit. The caller logs the difference so a
        # re-poll of the same unread mail reads as "nothing new", not fresh work.
        return {**(payload or {}), "_created": status == 201}

    # The automatic replies canopy-web could not send (canopy-web#1416), leased
    # to this box for the mailboxes it reads; `canopy_runner.admission_notices`.
    def claim_admission_notices(self, runner_id: str, mailboxes: dict) -> list:
        status, payload = _api("POST", f"/runners/{runner_id}/admission-notices",
                               {"mailboxes": mailboxes})
        if status != 200:
            raise RuntimeError(f"admission-notices lease: HTTP {status}")
        return (payload or {}).get("notices", [])

    def report_admission_notice(self, runner_id: str, notice_id: int, outcome: str,
                                detail: str = "") -> bool:
        status, payload = _api("POST", f"/runners/{runner_id}/admission-notices/{notice_id}",
                               {"outcome": outcome, "detail": detail})
        return status == 200 and bool((payload or {}).get("ok"))


#: slug -> (fetched_at, the INSTANCE's mailbox per canopy-web, "" for none).
_INSTANCE_MAILBOX: dict = {}
_INSTANCE_MAILBOX_TTL_SECONDS = 600


def _instance_mailbox(slug: str) -> str:
    """The mailbox canopy-web records for THIS instance of the agent
    (`Agent.email`), or "" when it records none. Cached briefly; on a failed
    lookup the last known answer is kept rather than dropping a live inbox."""
    now = time.time()
    hit = _INSTANCE_MAILBOX.get(slug)
    if hit and now - hit[0] < _INSTANCE_MAILBOX_TTL_SECONDS:
        return hit[1]
    status, body = _api("GET", f"/{slug}/", prefix="/api/agents")
    if status != 200 or not isinstance(body, dict):
        return hit[1] if hit else ""
    mailbox = str(body.get("email") or "").strip()
    _INSTANCE_MAILBOX[slug] = (now, mailbox)
    return mailbox


def _agent_mailboxes() -> dict:
    """{agent_slug: {"account": ..., "client": ...}} for the agents this box runs.

    THE MAILBOX IS THE INSTANCE'S, from canopy-web (`Agent.email`) — never the
    repo's. An agent's repo is its DEFINITION, shared by every instance of it
    (apps/agents/definition.py), so its `config/agent.json` names the same
    address for all of them: two ACE instances on two boxes would both poll
    ace@ and enqueue its mail as their own turns (canopy-web#984). An instance
    with no mailbox recorded in canopy is not polled at all, rather than
    inheriting the definition's.

    The gog CLIENT still comes from the clone: that is the definition's intent
    (which OAuth app its turns present), and `_resolve_mailbox_clients` replaces
    it with the one whose token actually authenticates before anything is read.
    """
    boxes: dict = {}
    for slug in [s.strip() for s in AGENT_SLUGS.split(",") if s.strip()]:
        cfg = pathlib.Path(AGENT_ROOT) / slug / "config" / "agent.json"
        try:
            data = json.loads(cfg.read_text())
        except Exception:  # noqa: BLE001 — no clone yet: nothing provisioned to read with
            continue
        account = _instance_mailbox(slug)
        if not account:
            continue
        declared = (data.get("email") or "").strip()
        if declared and declared.lower() != account.lower():
            _log(f"inbox {slug}: its repo names {declared}, but this instance's mailbox in "
                 f"canopy-web is {account} — polling {account}")
        # No declaration means the fleet's shared client, never one named after
        # the agent — that exists for nobody (2026-10-04).
        client = (data.get("gog_client") or "").strip() or FLEET_GOG_CLIENTS[0]
        boxes[slug] = {"account": account, "client": client}
    return boxes


_INBOX_STAMPS: dict = {}

#: account -> the gog client whose token PROVED it can read that mailbox. Filled
#: by `_resolve_mailbox_clients`, evicted by `_drain_inbox` on a "No auth" failure
#: so a rotated token is re-probed on the next tick rather than mourned forever.
_GOG_CLIENT_LIVE: dict = {}

#: account -> whether the last probe of it found a client that reads it. Empty
#: until the first probe, which is what lets the heartbeat say "unknown" (omit
#: the field) rather than "reads nothing" before the box has looked.
_MAILBOX_READABLE: dict = {}


def mailboxes_readable() -> list | None:
    """The addresses this box PROVED it can read, for the heartbeat; None before
    the first probe. The doorbell rings only runners whose list holds the rung
    address (or that never reported one) — canopy-web#1087."""
    if not _MAILBOX_READABLE:
        return None
    return sorted(a.lower() for a, ok in _MAILBOX_READABLE.items() if ok)


def _gog_config_dir(slug: str = "") -> pathlib.Path:
    """gog's own config dir on Linux — `slug`'s own home on a box with a unit secret
    (`gog_env`), else $GOG_HOME, else $XDG_CONFIG_HOME/gogcli, else ~/.config/gogcli.
    Mirrors bootstrap_agents.sh `gog_config_dir`."""
    own = gog_env(slug).get("GOG_HOME") if slug else ""
    if own:
        return pathlib.Path(own)
    home = os.environ.get("GOG_HOME", "").strip()
    if home:
        return pathlib.Path(os.path.expanduser(home))
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.join(
        os.path.expanduser("~"), ".config")
    return pathlib.Path(base) / "gogcli"


#: The fleet's two Google OAuth clients — one app, two ways to sign in, either of
#: which serves every agent (canopy agent_email.FLEET_CLIENTS, the same order).
FLEET_GOG_CLIENTS = ("canopy", "canopy-web")


def _candidate_gog_clients(account: str, declared: str, slug: str = "") -> list:
    """Every client this box could present for `account`, most likely first.

    Order: what the agent's config declares; what gog's own `account_clients`
    map says (bootstrap writes it); then every `credentials-<client>.json` in the
    config dir. None of these is authoritative on its own — a token is minted FOR
    a client and only that client can use it — so the list is a search order,
    not an answer. `_resolve_mailbox_clients` asks the token.
    """
    cfg_dir = _gog_config_dir(slug)
    # The fleet clients right behind the declaration — those this box holds the
    # credentials for: `canopy` (a laptop's gog login) and `canopy-web` (canopy-web's
    # "Connect Google mailbox" button) are one app, and either serves the agent.
    order = [declared, *(c for c in FLEET_GOG_CLIENTS
                         if (cfg_dir / f"credentials-{c}.json").is_file())]
    try:
        data = json.loads((cfg_dir / "config.json").read_text())
        order.append(((data.get("account_clients") or {}).get(account) or ""))
    except Exception:  # noqa: BLE001 — no map yet is a normal first-boot state
        pass
    try:
        for f in sorted(cfg_dir.glob("credentials-*.json")):
            order.append(f.name[len("credentials-"):-len(".json")])
    except Exception:  # noqa: BLE001
        pass
    seen: list = []
    for c in order:
        c = (c or "").strip()
        if c and c not in seen:
            seen.append(c)
    return seen


def _resolve_mailbox_clients(boxes: dict, probe) -> dict:
    """Replace each mailbox's DECLARED gog client with the one that AUTHENTICATES.

    `probe(account, client)` makes one real gog call and raises if the token
    under `client` cannot read `account`. The first candidate that succeeds is
    cached in `_GOG_CLIENT_LIVE`; a mailbox no candidate can open is dropped
    from the returned map with one log line naming what was tried.

    Why a probe and not a lookup: every agent's config declares the shared
    `canopy` client, but on this box echo's live token sits under `echo` (and
    ace's `ace` token is expired, so ace only works under `canopy`). Neither
    config/agent.json nor gog's account_clients map gets both right —
    bootstrap_agents.sh spells out why, and canopy-web#661 (reverted the same
    day) is what trusting the declaration cost. The token is the only authority
    (canopy-web#738).
    """
    resolved: dict = {}
    for slug, box in boxes.items():
        account = (box.get("account") or "").strip()
        declared = (box.get("client") or "").strip()
        live = _GOG_CLIENT_LIVE.get(account)
        if not live:
            tried = _candidate_gog_clients(account, declared, slug)
            for client in tried:
                try:
                    probe(account, client)
                except Exception:  # noqa: BLE001 — this candidate cannot open it
                    continue
                live = client
                _GOG_CLIENT_LIVE[account] = client
                _MAILBOX_READABLE[account] = True
                if client != declared:
                    _log(f"inbox {slug}: token for {account} lives under client "
                         f"{client!r}, config declares {declared!r} — reading "
                         f"with {client!r}")
                break
            if not live:
                _MAILBOX_READABLE[account] = False
                _log(f"inbox {slug}: no gog client can read {account} "
                     f"(tried: {', '.join(tried) or 'none'})")
                continue
        resolved[slug] = {**box, "client": live}
    return resolved


def _drain_inbox(runner_id: str) -> None:
    """Check every mailbox that is due — rung by the doorbell, or timed out.

    Modelled on `_drain_mint`: polled from the same tick, and every failure is
    logged rather than raised, because a mailbox whose auth died must not take
    the turn loop down with it.
    """
    core = _inbox_core()
    if core is None:
        return
    inbox_mod, inbox_due = core
    boxes = _agent_mailboxes()
    if not boxes:
        return
    try:
        rung = inbox_due.take_pending()
        # Resolve only the mailboxes that are DUE, so a mailbox nobody rang and
        # whose timer has not elapsed costs no gog call this tick.
        due_declared = inbox_due.due(boxes, _INBOX_STAMPS, now=time.time(),
                                     interval=INBOX_POLL_SECONDS, rung=rung)
        boxes = {
            **{s: b for s, b in boxes.items() if s not in due_declared},
            # One agent at a time: each probe runs gog in THAT agent's own home with
            # its own keyring password (`_gog_runner`), never a box-wide one.
            **{k: v for s in due_declared for k, v in _resolve_mailbox_clients(
                {s: boxes[s]},
                lambda account, client, _s=s: inbox_mod.search_threads(
                    account, client, max_threads=1,
                    **({"runner": _gog_runner(_s)} if gog_env(_s) else {})),
            ).items()},
        }
        # A mailbox no client could open is stamped like any other failure, or
        # it is re-probed every tick.
        for slug in due_declared:
            if slug not in boxes:
                _INBOX_STAMPS[slug] = time.time()
        due = inbox_due.due(boxes, _INBOX_STAMPS, now=time.time(),
                            interval=INBOX_POLL_SECONDS, rung=rung)
        rung_slugs = {
            slug for slug in boxes
            if (boxes[slug].get("account") or "").strip().lower() in rung
        }
        client = _InboxClient(runner_id)
        for slug in due:
            box = boxes[slug]
            try:
                res = inbox_mod.check_inbox(
                    client, slug, mailbox=box["account"], gog_client=box["client"],
                    discovered_by=inbox_due.discovered_by(slug, rung_slugs),
                    **({"runner": _gog_runner(slug)} if gog_env(slug) else {}),
                )
                if res.get("new"):
                    _log(f"inbox {slug}: {len(res['new'])} new turn(s) "
                         f"({inbox_due.discovered_by(slug, rung_slugs)})")
                rows = res.get("rows") or {}
                for tid in res.get("archived") or []:
                    _log(f"inbox {slug}: {tid} archived -> {rows.get(tid, '?')}")
                for tid, err in (res.get("archive_errors") or {}).items():
                    _log(f"inbox {slug}: {tid} -> {rows.get(tid, '?')}: archive failed, "
                         f"left unread ({err})")
            except Exception as exc:  # noqa: BLE001
                _log(f"inbox {slug}: check failed ({exc})")
                # A token that stopped authenticating (rotated, revoked) must be
                # re-probed next time, not presented forever from the cache.
                if "no auth" in str(exc).lower():
                    _GOG_CLIENT_LIVE.pop(box.get("account"), None)
                    _MAILBOX_READABLE[box.get("account")] = False
            finally:
                # Stamp even on failure, or a broken mailbox is retried every tick.
                _INBOX_STAMPS[slug] = time.time()
        # The automatic replies canopy-web could not send for these mailboxes
        # (canopy-web#1416), with the client each was just proved to read with.
        if due:
            try:
                from canopy_runner import admission_notices  # noqa: PLC0415

                if any(gog_env(s) for s in due):
                    for s in due:  # each agent's mail with its own gog home and password
                        admission_notices.deliver(client, runner_id, {s: boxes[s]}, log=_log,
                                                  runner=_gog_runner(s))
                else:
                    admission_notices.deliver(client, runner_id, {s: boxes[s] for s in due}, log=_log)
            except Exception as exc:  # noqa: BLE001
                _log(f"admission notices: {exc}")
    except Exception as exc:  # noqa: BLE001
        _log(f"inbox drain error: {exc}")


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


def _agent_session_title(turn: dict) -> str:
    """A readable name for an agent turn's session: its email's subject, else its
    schedule's name, else the first line of its prompt with any leading slash
    command and its flags removed ("/ace:turn --thread 1a0f…" is not a name —
    it was one on 2026-10-03). "" when nothing readable is left; the server
    names it then (harness.services.turn_session_title, which also decides)."""
    ref = turn.get("origin_ref") or {}
    for key in ("subject", "schedule_name"):
        name = ref.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()[:200]
    for line in str(turn.get("prompt") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            words = line.split()[1:]
            while words and words[0].startswith("-"):
                words = words[2:] if len(words) > 1 and "=" not in words[0] and not words[1].startswith("-") else words[1:]
            line = " ".join(words).lstrip("—–-:· ").strip()
        return line[:200]
    return ""


def _agent_thread_key(turn: dict) -> str:
    """The thread an AGENT turn records its session under: `<agent>:<turn id>`,
    the laptop runner's own fallback (execute.py `_thread_key`), so a cloud
    agent turn gets a canopy Session exactly as a laptop one does. "" for a
    chat or project turn.

    Always one session PER TURN, even when origin_ref names a thread: a cloud
    agent turn never resumes (it runs in its own per-turn worktree, not a
    stable per-session cwd), so a second turn on a named thread would only re-point the
    first one's binding at a different transcript and interleave two
    conversations' ordinals in one Session."""
    if _chat_session_id(turn):
        return ""
    slug = turn.get("agent_slug") or ""
    turn_id = str(turn.get("id") or "")
    return f"{slug}:{turn_id}" if slug and turn_id else ""


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
    why the duplication exists). '\\' and ':' are Claude Code's Windows
    separators (failure investigation 6, 2026-10-16 — see paths.py).
    """
    return (str(cwd).replace("\\", "-").replace(":", "-")
            .replace("/", "-").replace(".", "-").replace("_", "-"))


def _adopt_resume_transcript(cwd: pathlib.Path, session_id: str, agent_slug: str) -> bool:
    """Make a session started by an AGENT turn resumable from a CHAT turn.

    An agent turn runs in its own worktree of the agent's clone (before #1141,
    in the clone itself); a reply sent into that turn's chat is a session turn, which runs in the chat's own
    WORK_DIR/sessions/<id>. Claude Code finds a `--resume` / `session/load`
    target by the cwd-derived project directory, so the session the agent turn
    wrote was invisible from there: `_resume_target_exists` said no, and the
    reply started fresh with none of the turn's context.

    Copies the transcript into this cwd's project directory when it is missing
    here and present where the agent turn ran. A copy, not a move: that copy is
    the record the original turn's stream was read from. After
    this, the resume, the stream tail (`_session_transcript_path` checks this
    cwd first) and every later reply all use this cwd's copy. True if copied."""
    if not session_id or _resume_target_exists(cwd, session_id):
        return False
    if not agent_slug or _safe_session_dirname(agent_slug) != agent_slug:
        return False
    # Looked up, not derived: since #1141 each agent turn ran in its own
    # worktree (`_agent_session_transcript`), and older ones in the clone.
    src = _agent_session_transcript(agent_slug, session_id)
    dst = CLAUDE_PROJECTS_HOME / _encode_project_dir(cwd) / f"{session_id}.jsonl"
    try:
        if src is None:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as exc:
        _log(f"warn: could not adopt session {session_id[:8]} into {cwd}: {exc}")
        return False
    _log(f"adopted session {session_id[:8]} from {agent_slug}'s turn into {cwd}")
    return True


class ForkError(RuntimeError):
    """A fork could not be cut on this box; the message says why."""


def _cut_fork(cwd: pathlib.Path, fork: dict, agent_slug: str) -> str:
    """Start a forked chat's Claude session: the first `fork["records"]` records of
    the source session's transcript (counted as canopy_transcript.read_records
    counts them — canopy-web's ordinals), written under a new session id in this
    cwd's project directory, where `--resume` / `session/load` look. Returns the id.

    The source ran in this same directory (`_turn_cwd` keys a fork on it), so its
    transcript is here — or, for a session an agent turn started and no chat reply
    has adopted yet, in that turn's worktree."""
    ct = _transcript_core()
    source, records = fork.get("session_key") or "", int(fork.get("records") or 0)
    if ct is None:
        raise ForkError("canopy_transcript is not installed on this runner")
    src = ct.resolve_cli_transcript(cwd, source, claude_home=CLAUDE_PROJECTS_HOME)
    if src is None and agent_slug and _safe_session_dirname(agent_slug) == agent_slug:
        src = _agent_session_transcript(agent_slug, source)
    if src is None:
        raise ForkError(f"session {source} has no transcript on this box")
    kept = ct.read_records(src)[:records]
    if len(kept) < records:
        raise ForkError(f"session {source}'s transcript has {len(kept)} records, not {records}")
    sid = str(uuid.uuid4())
    dst = CLAUDE_PROJECTS_HOME / _encode_project_dir(cwd) / f"{sid}.jsonl"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("".join(json.dumps({**r, "sessionId": sid} if "sessionId" in r else r) + "\n"
                           for r in kept))
    _log(f"forked session {source[:8]} at {records} records into {sid[:8]} in {cwd}")
    return sid


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
    # A forked chat: which chat's directory it runs in (its source's, so they share
    # a worktree), and on its first turn the cut to start from (_cut_fork).
    if plan.get("worktree_of"):
        turn["_worktree_of"] = plan["worktree_of"]
    if plan.get("fork"):
        turn["_fork"] = plan["fork"]
    key = plan.get("session_key") or plan.get("emdash_task_id") or ""
    if plan.get("reuse") and key:
        return key
    return ""


def _record_session_resume(runner_id: str, turn: dict, cli_session_id: str) -> None:
    """Persist this turn's real CLI session id as the thread's engine handle, so
    a LATER turn on the same canopy Session can `--resume` it (see
    `_session_resume_plan`). Best-effort: a failure here only degrades the NEXT
    turn to a fresh spawn, never this one — logged, never raised.

    For an AGENT turn (`_agent_thread_key`) this is what gives the turn a canopy
    Session at all: record-session creates an origin=runner Session + binding
    for a thread it has not seen. Called the moment the executor learns the CLI
    session id (`_announce_session`), not after the turn, so the binding exists
    while the turn runs and `_sync_session_streams` tails it live.

    Sends BOTH `session_key` (what's actually read back today, via
    RunnerBinding.session_key) and `session_id` (the wire-compat field already
    threaded through RecordSessionIn -> services.record_session, currently
    accepted and silently discarded there) — if a `Session`-level column for
    this is ever added and wired server-side, this call starts populating it
    with zero runner-side changes; see `_session_resume_plan`'s docstring for
    why no such column exists today despite the originating plan naming one.
    """
    agent_thread = _agent_thread_key(turn)
    thread_key = _session_thread_key(turn) or agent_thread
    if not thread_key or not cli_session_id:
        return
    agent_slug = turn.get("agent_slug") or ""
    project = turn.get("project") or ""
    if not agent_slug and not project:
        return
    body: dict = {
        "thread_key": thread_key,
        "session_key": cli_session_id,
        "session_id": cli_session_id,
    }
    if agent_thread and thread_key == agent_thread:
        # The key is a Claude session UUID; give the new Session a name a person
        # can find in Chats. (A chat turn's session already has its own title.)
        body["title"] = _agent_session_title(turn)
    if turn.get("id"):
        # Stamp the TURN's key now, not at finish: the agent posts its close-out
        # before the turn ends, and the close-out finds its turn by this key.
        body["turn_id"] = str(turn["id"])
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
#
# MEMORY is the real ceiling, not the turn count. A turn is Claude Code plus its
# ACP adapter (600-700 MB measured) plus whatever the agent runs — a test suite,
# an npm build. On 2026-10-05 cloud-ec2-1 (t3.medium: 3.8 GB, no swap) ran out
# of memory twice with a flat cap of 4, and each kernel OOM kill took every
# in-flight turn down with it. So the default cap is derived from the box's RAM,
# and a claim is skipped while available memory is below a floor.
#: Memory one turn is budgeted at, for deriving the default cap.
TURN_MEMORY_BUDGET_KB = int(os.environ.get("TURN_MEMORY_BUDGET_KB", str(1536 * 1024)))
#: Don't START a turn below this much MemAvailable — the turns already running
#: need it more than a new one does.
CLAIM_MIN_AVAILABLE_KB = int(os.environ.get("CLAIM_MIN_AVAILABLE_KB", str(1024 * 1024)))


def _read_meminfo() -> str:
    try:
        return pathlib.Path("/proc/meminfo").read_text()
    except OSError:
        return ""


def _meminfo_kb(meminfo: str, key: str) -> int | None:
    for line in meminfo.splitlines():
        if line.startswith(f"{key}:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _default_max_concurrent(meminfo: str) -> int:
    """One turn per TURN_MEMORY_BUDGET_KB of RAM, at least 1; 4 (the old flat
    default) when the box's memory can't be read."""
    total = _meminfo_kb(meminfo, "MemTotal")
    if not total:
        return 4
    return max(1, total // TURN_MEMORY_BUDGET_KB)


def _memory_headroom_ok() -> bool:
    """Enough MemAvailable to start another turn. Unknown never blocks — the cap
    still applies, and a box that can't read /proc must keep working."""
    avail = _meminfo_kb(_read_meminfo(), "MemAvailable")
    return avail is None or avail >= CLAIM_MIN_AVAILABLE_KB


MAX_CONCURRENT_TURNS = int(os.environ.get("MAX_CONCURRENT_TURNS", "")
                           or _default_max_concurrent(_read_meminfo()))

_TURNS_LOCK = threading.Lock()
_IN_FLIGHT: dict[str, object] = {}


def _in_flight_ids() -> list[str]:
    """Turn ids currently executing — reported on every heartbeat so the server
    renews all of their leases, not just the newest."""
    with _TURNS_LOCK:
        return sorted(_IN_FLIGHT)


def _transcript_core():
    """canopy_transcript, imported lazily and cached.

    Lazy because this module ALSO clones canopy-web (sync_runner_src),
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
    events = [ct.stream_event(r) for r in rows]
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
# monotonic() when a stream last shipped or caught up rows; drives _stream_interval.
_LAST_STREAM_ACTIVITY = 0.0


def _stream_interval(now: float) -> float:
    """How long to wait between stream syncs (#647): the live clock while
    transcripts have grown recently, the idle clock once they have not."""
    if now - _LAST_STREAM_ACTIVITY < STREAM_IDLE_AFTER_SECONDS:
        return STREAM_POLL_SECONDS
    return max(STREAM_POLL_SECONDS, STREAM_IDLE_POLL_SECONDS)


def _session_transcript_path(session_id: str, session_key: str, project: str = ""):
    """The CLI transcript backing a canopy session, or None if not resolvable yet.

    Two places a session can have run: a CHAT session in its own stable
    WORK_DIR/sessions/<id> (see _turn_cwd), an AGENT turn's session in that
    turn's worktree of the agent's clone (or, before #1141, the clone itself) —
    found by `_agent_session_transcript` from the descriptor's `project`, which
    is the session's emdash_project (the agent's slug). `session_key` is the CLI session
    uuid, so whichever directory holds that file is the one; there is nothing to
    guess between them."""
    ct = _transcript_core()
    if ct is None or not (session_id and session_key):
        return None
    # A local session sent up from Health (`claude:<uuid>`) names its file.
    sent_up = getattr(ct, "transcript_for_key", None)
    if sent_up is not None and (path := sent_up(session_key, claude_home=CLAUDE_PROJECTS_HOME)) is not None:
        return path
    cwd = pathlib.Path(WORK_DIR) / "sessions" / _safe_session_dirname(session_id)
    path = ct.resolve_cli_transcript(cwd, session_key, claude_home=CLAUDE_PROJECTS_HOME)
    if path is not None:
        return path
    if project and _safe_session_dirname(project) == project:
        if (path := _agent_session_transcript(project, session_key)) is not None:
            return path
    return _forked_chat_transcript(session_key)


def _forked_chat_transcript(session_key: str) -> pathlib.Path | None:
    """A forked chat runs in its SOURCE's directory (`_turn_cwd`), not its own, so
    its transcript is looked for by name across the chat directories — and only
    there: a Claude session uuid is unique on this box, and nothing outside
    WORK_DIR/sessions is reachable this way."""
    if not session_key or _safe_session_dirname(session_key) != session_key:
        return None
    chats = _encode_project_dir(pathlib.Path(WORK_DIR) / "sessions")
    try:
        found = sorted(CLAUDE_PROJECTS_HOME.glob(f"{chats}-*/{session_key}.jsonl"))
    except OSError:
        return None
    return found[0] if found else None


def _post_stream_rows(runner_id: str, session_id: str, rows: list,
                      transcript_id: str = "") -> bool:
    """Ship conversational rows as live events. `seq == index` (the composite
    transcript ordinal): monotonic per session forever, so WS-derived `seq:<n>`
    message ids cannot collide across detaches, restarts or failovers."""
    ct = _transcript_core()
    if ct is None:
        return False
    events = [ct.stream_event(r) for r in rows]
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
    global _LAST_STREAM_ACTIVITY
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
            path = _session_transcript_path(sid, st["session_key"],
                                            descriptor.get("project") or "")
            if path is None:
                continue  # not spawned yet, or a different box owns it
            transcript_id = path.stem  # the CLI session uuid — the conversation's identity
            # Re-attach on a new PATH, not only a new stem: a chat reply adopts an
            # agent turn's transcript into its own cwd (_adopt_resume_transcript)
            # under the SAME session id, then appends to that copy. Keyed on the
            # stem, the tailer stayed on the abandoned copy and the viewer saw
            # nothing until the turn-end ship.
            if st["reader"] is not None and (st.get("transcript_id") != transcript_id
                                             or st.get("path") != str(path)):
                st["reader"], st["count"] = None, 0  # session replaced or moved under us
            st["transcript_id"] = transcript_id
            st["path"] = str(path)
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
                if rows:
                    _LAST_STREAM_ACTIVITY = time.monotonic()
                continue
            new_records = st["reader"].read_new()
            if not new_records:
                continue
            _LAST_STREAM_ACTIVITY = time.monotonic()
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
        path = _session_transcript_path(sid, b.get("session_key") or "",
                                        b.get("project") or "")
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
            _maybe_report_usage(runner_id)
        _sync_session_streams(runner_id)
    except Exception as exc:  # noqa: BLE001 — never take the main loop down
        _log(f"session view sync error: {exc}")


# ── usage report (canopy-web events.usage) ───────────────────────────────────
# What this box's Claude Code sessions spent, per session per UTC day, no content
# — `canopy_transcript.send_usage_report`, the same report the laptop runner makes.
# The scope is read every USAGE_SCOPE_SECONDS so a requested full rescan ("Check for
# missing sessions" on Health) is answered within about a minute.
USAGE_SCOPE_SECONDS = 60
USAGE_INTERVAL_SECONDS = 600
_USAGE = {"scope_at": 0.0, "scan_at": 0.0}


def _maybe_report_usage(runner_id: str) -> None:
    ct = _transcript_core()
    now = time.monotonic()
    if ct is None or not hasattr(ct, "send_usage_report") or now < _USAGE["scope_at"]:
        return
    _USAGE["scope_at"] = now + USAGE_SCOPE_SECONDS
    status, scope = _api("GET", f"/runners/{runner_id}/usage-scope")
    if status != 200 or not isinstance(scope, dict):
        return
    full = bool(scope.get("full"))
    if not full and now < _USAGE["scan_at"]:
        return

    def post(chunk, last):
        code, _ = _api("POST", f"/runners/{runner_id}/usage", {"sessions": chunk, "full": last})
        if code != 200:
            raise RuntimeError(f"usage report refused: {code}")

    try:
        n = ct.send_usage_report(post, home=pathlib.Path.home(), repos=scope.get("repos") or [], full=full)
    except Exception as exc:  # noqa: BLE001
        _log(f"usage report failed, will retry: {exc}")
        return
    _USAGE["scan_at"] = now + USAGE_INTERVAL_SECONDS
    if n or full:
        _log(f"usage report: {n} session(s){' (full rescan)' if full else ''}")


# ── confined turns (who-is-asking phase 5, cloud) ────────────────────────────
# A CALLER's turn — someone who is not the agent's owner or an admin — runs
# confined to the capability canopy granted them. On a laptop the runner cannot
# set a session's environment (emdash spawns it), so canopy's profile_guard finds
# the profile from the `cx-` session name. Here the runner spawns claude itself,
# so it says so directly: CANOPY_PROFILE=<profile file>, inherited by every hook
# and by the canopy plugin's MCP headers helper. The agent cannot unset it for
# them — they are started by claude, not by the agent's shell.
PROFILE_ROOT = pathlib.Path.home() / ".canopy" / "profiles"
CALLER_ROOT = pathlib.Path.home() / ".canopy" / "caller"
#: The format version written into each profile file's `"version"` field (2 = the
#: shape this box writes for a guard that honours CANOPY_PROFILE). Nothing reads it
#: today; what the box reports is the guard's own version (`profiles_supported`).
PROFILE_DOC_VERSION = 2
#: The oldest guard this box runs a caller's turn under, and what it reports is the
#: guard's own version — see canopy_runner caller.MIN_GUARD_VERSION (3 = writes
#: checked against write_paths).
MIN_GUARD_VERSION = 3


#: The caller-envelope version this code reads, reported on every beat.
ENVELOPE_VERSION = 2
#: `profile` values meaning "confined to a capability": the current word and the
#: one an envelope from an older canopy-web carries.
CONFINED_PROFILES = frozenset({"confined", "restricted"})


class ConfineError(RuntimeError):
    pass


# What a confined session may inherit from this process. An ALLOWLIST, not a strip
# list: this process holds the runner owner's canopy PAT (CANOPY_TOKEN), every
# agent turn used to get its agent's 1Password key and its whole ~/.<slug>/.env
# (CANOPY_WEB_PAT among it) and the owner's GitHub token — and profile_guard was the
# only thing between a prompt-injected caller session and all of them. A variable
# nobody listed here is one a confined session does not get.
#
# Claude's own login stays: the session cannot run without it. Everything else is
# locale, paths and TLS — what a process needs to start, never to act as anyone.
_CONFINED_ENV_KEEP = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "CANOPY_BASE_URL", "CLAUDE_CONFIG_DIR",
    *_CLAUDE_AUTH_VARS,
})
_CONFINED_ENV_KEEP_PREFIXES = ("LC_",)

#: The one secret a capability may need from this process: the password to gog's
#: `file` keyring, without which an `ask` capability can neither read the caller's
#: thread nor reply to it. Given ONLY to a capability whose own bash allowlist runs
#: the mail path (`_capability_uses_mail`) — derived from the declaration, so no live
#: interface has to change. Note what it opens: the box's one gog keyring, which
#: holds every agent's mailbox token on this box (the agent's identity comes from
#: its repo's config/agent.json, not from here).
_CONFINED_MAIL_ENV = ("GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND", "GOG_HOME")
#: A bash rule that runs the mail path: `canopy email …` or an agent's own
#: `bin/<slug>-email` shim over it.
_MAIL_RULE = re.compile(r"^(?:canopy\s+email\s|(?:\./)?bin/[A-Za-z0-9_-]+-email(?:\s|$))")
#: Per-turn variables that are credentials. A confined turn's extra never carries
#: them (`_run_turn` does not build them for it); dropped again here so a future
#: caller of `_TURN_ENV.extra` cannot reintroduce one.
_CONFINED_EXTRA_DENY = frozenset({*_GITHUB_ENV_KEYS, "CANOPY_CHAT_KEY"})


def _capability_uses_mail(cap: dict) -> bool:
    return any(_MAIL_RULE.match(str(rule).strip()) for rule in (cap.get("bash") or []))


def _confined_env(slug: str | None, cap: dict, extra: dict) -> dict:
    """A caller's turn environment: the allowlist above, the mail keyring only if
    the capability sends mail, which agent and turn this is, and the per-turn
    pointers (CANOPY_PROFILE, CANOPY_CALLER, …). Never the agent's `.env`, never
    its 1Password key, never a GitHub token, never this process's canopy PAT —
    the session reaches canopy's MCP with its caller token, from its profile."""
    src = _child_safe_env()
    env = {k: v for k, v in src.items()
           if k in _CONFINED_ENV_KEEP or k.startswith(_CONFINED_ENV_KEEP_PREFIXES)}
    if _capability_uses_mail(cap):
        if _gog_secret() is not None:
            # A unit (spec §6): ONLY this agent's gog home and password, derived from
            # the unit secret — never the secret's path, never another agent's pair.
            # A secret that cannot be read gives nothing.
            env.update(gog_env(slug or ""))
        else:
            env.update({k: src[k] for k in _CONFINED_MAIL_ENV if k in src})
    if slug:
        env["CANOPY_AGENT"] = slug  # a name, not a secret — see `_agent_env`
    env.update({k: v for k, v in extra.items() if k not in _CONFINED_EXTRA_DENY})
    return env


def _capability(turn: dict) -> dict | None:
    env = turn.get("caller_context") or {}
    if env.get("profile") not in CONFINED_PROFILES:
        return None
    cap = env.get("capability")
    return cap if isinstance(cap, dict) else {"name": "none", "tools": [], "bash": [],
                                              "read_paths": [], "write_paths": [], "entry": None}


def _confined_prompt(turn: dict) -> str:
    cap = _capability(turn) or {}
    prompt = turn.get("prompt", "")
    entry = cap.get("entry")
    if not entry:
        return prompt
    tid = str(((turn.get("caller_context") or {}).get("conversation") or {}).get("thread_id")
              or (turn.get("origin_ref") or {}).get("thread_id") or "")
    if "{thread_id}" in entry and not tid:
        # A thread-bound entry is how an EMAIL caller's session starts. A chat or
        # Slack turn has no thread, and its prompt is the person's own question —
        # which the laptop runner delivers as-is on that path. Refusing here
        # broke every confined chat turn on this box; substituting an entry with
        # no thread would silently drop the question. The profile confines the
        # session either way. Anything that is not a chat turn still refuses.
        if _chat_session_id(turn):
            return prompt
        raise ConfineError(f"capability '{cap.get('name')}' starts on a thread, and this turn has none")
    return entry.replace("{thread_id}", tid)


def _write_private(path: pathlib.Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)


def _confine(turn: dict) -> tuple[dict, str]:
    """Write the turn's caller envelope and profile. Returns (env, caller_path):
    the env points the session at its profile; the envelope is what the agent's
    `answer-caller` skill reads first (`--caller <path>`), and the profile names
    it so the guard allows reading exactly that one file."""
    turn_id = str(turn["id"])
    cap = _capability(turn)
    caller_path = CALLER_ROOT / f"{turn_id}.json"
    doc = {"version": PROFILE_DOC_VERSION, "turn_id": turn_id, "capability": cap,
           "caller_path": str(caller_path),
           "thread_id": str(((turn.get("caller_context") or {}).get("conversation") or {})
                            .get("thread_id") or "") or None,
           "mcp_token": turn.get("mcp_token") or None,
           }
    path = PROFILE_ROOT / f"cloud-{turn_id}.json"
    try:
        _write_private(caller_path, turn.get("caller_context") or {})
        _write_private(path, doc)
    except OSError as exc:
        raise ConfineError(f"could not write the session profile: {exc}") from exc
    return {"CANOPY_PROFILE": str(path)}, str(caller_path)


def _write_envelope(turn: dict) -> dict:
    """Every turn's caller envelope, and the env that points the canopy plugin's
    UserPromptSubmit hook at it: `CANOPY_CALLER=<path>`. The laptop leaves a pointer
    keyed by emdash task; here the runner spawns claude itself, so it just says so.
    The hook then puts a short "who is asking" summary beside the prompt — a
    free-text prompt carries no `--caller`, and without this the agent never knew.
    Best-effort: {} when there is no envelope or it cannot be written."""
    env = turn.get("caller_context")
    turn_id = str(turn.get("id") or "")
    if not isinstance(env, dict) or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", turn_id):
        return {}
    path = CALLER_ROOT / f"{turn_id}.json"
    try:
        _write_private(path, env)
    except OSError as exc:
        _log(f"warn: could not write the caller envelope for {turn_id[:8]}: {exc}")
        return {}
    return {"CANOPY_CALLER": str(path)}


def _provenance_brief(turn: dict) -> str:
    """`origin=… who=… credential=… client=… parent=…` from the claimed turn (the
    server's TurnOut.initiator / provenance / parent_*), mirroring the laptop
    runner's canopy_runner/whois.py — this file is stdlib-only and cannot import
    it. `-` for anything an older server did not send."""
    ini = turn.get("initiator") or {}
    person = ini.get("user") or ini.get("contact") or {}
    ident = person.get("email") or ini.get("agent") or ""
    who = f"{ini.get('kind') or 'unknown'}:{ident}" if ident else (ini.get("kind") or "unknown")
    cred = ini.get("credential") or (turn.get("provenance") or {}).get("credential") or {}
    cred_s = ":".join([str(cred.get("type") or "?"), "" if cred.get("id") is None
                       else str(cred["id"]), str(cred.get("label") or "")]) if cred else ""
    parent = ""
    for key in ("parent_turn_id", "parent_session_id", "parent_task", "parent_claude_session"):
        if turn.get(key):
            parent = f"{key[len('parent_'):].removesuffix('_id')}:{turn[key]}"
            break
    client = ini.get("client") or (turn.get("provenance") or {}).get("client") or ""

    def v(x):
        x = str(x or "").strip()
        return json.dumps(x) if any(c in x for c in ' "=\n') else (x or "-")
    head = json.dumps(str(turn.get("prompt") or "")[:80])
    return (f"origin={v(turn.get('origin'))} who={v(who)} credential={v(cred_s)} "
            f"client={v(client)} parent={v(parent)} prompt_head={head}")


def _lineage_env(turn: dict) -> dict:
    """WHICH turn / session this Claude session runs for, so anything it asks
    canopy for in turn names it as the parent. The `canopy` CLI forwards these as
    `X-Canopy-Parent-Turn` / `-Session` (canopy-web
    apps/common/request_context.py), and canopy records them on the turn or
    session the request creates. Without them a script run inside an agent's
    session creates work that looks like its owner typed it. Nothing
    runner-specific: the server reads this runner off the parent turn.

    CANOPY_TURN_ID is also what binds a cloud-capability lease to this turn
    (`POST /api/capabilities/leases {turn_id}`), so canopy-web releases the
    machine when the turn ends. Every turn gets it — full and confined alike.

    Not secrets — ids. Only well-formed values are exported."""
    out = {}
    turn_id = str(turn.get("id") or "")
    if re.fullmatch(r"[0-9a-fA-F-]{8,64}", turn_id):
        out["CANOPY_TURN_ID"] = turn_id
    chat_id = _chat_session_id(turn)
    if re.fullmatch(r"[0-9a-fA-F-]{8,64}", chat_id or ""):
        out["CANOPY_SESSION_ID"] = chat_id
    return out


def _native_settings(turn: dict, cwd: pathlib.Path, caller_path: str) -> str | None:
    """A confined turn's capability as Claude Code's OWN permission rules, passed
    with `--settings` — the second layer under profile_guard (canopy_runner's
    `native_permissions` holds the translation and the reasoning). Best-effort:
    profile_guard is the fail-closed layer, so a missing module or an unwritable
    file costs the second layer, never the turn — and says so."""
    try:
        from canopy_runner import native_permissions  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: native permission layer unavailable ({exc}); profile_guard alone confines "
             f"turn {str(turn.get('id'))[:8]}")
        return None
    cap = _capability(turn) or {}
    tid = str(((turn.get("caller_context") or {}).get("conversation") or {}).get("thread_id")
              or (turn.get("origin_ref") or {}).get("thread_id") or "")
    path = PROFILE_ROOT / f"cloud-{turn['id']}.settings.json"
    try:
        doc = native_permissions.cli_settings(cap, cwd=str(cwd), caller_path=caller_path,
                                              thread_id=tid)
        _write_private(path, doc)
    except Exception as exc:  # noqa: BLE001
        _log(f"warn: could not write the native permission layer ({exc}); profile_guard "
             f"alone confines turn {str(turn.get('id'))[:8]}")
        return None
    return str(path)


def _canopy_plugin_root() -> pathlib.Path | None:
    try:
        d = json.loads((pathlib.Path.home() / ".claude" / "plugins" /
                        "installed_plugins.json").read_text())
        return pathlib.Path(d["plugins"]["canopy@canopy"][0]["installPath"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def profiles_supported(plugin_root: pathlib.Path | None = None) -> int:
    """The installed guard's version when it honours CANOPY_PROFILE and checks
    writes against write_paths (≥ MIN_GUARD_VERSION), else 0 — and 0 means canopy
    never gives this box a caller's turn."""
    import re as _re

    root = plugin_root or _canopy_plugin_root()
    if root is None:
        return 0
    try:
        guard = (root / "hooks" / "profile_guard.py").read_text()
        hooks = (root / "hooks" / "hooks.json").read_text()
    except OSError:
        return 0
    m = _re.search(r"^PROFILE_ENFORCEMENT_VERSION\s*=\s*(\d+)", guard, _re.M)
    ok = m and int(m.group(1)) >= MIN_GUARD_VERSION and "profile_guard.py" in hooks
    return int(m.group(1)) if ok else 0


#: Every turn this runner executes is ONE-SHOT: when the model ends its turn the
#: session closes (ACP `agent.close()`, or `claude -p` exits), so a background task's
#: completion notification has nobody to wake. canopy's one_shot_turn_guard hook
#: (canopy #755) blocks background Bash/subagents/waits when this is set — stated
#: explicitly here rather than inferred from an unrelated variable.
ONE_SHOT_TURN_ENV = {"CANOPY_ONE_SHOT_TURN": "1"}


def _run_turn(runner_id: str, turn: dict) -> None:
    """Execute one claimed turn to completion. Runs on its own thread."""
    turn_id = turn["id"]
    try:
        confined = _capability(turn) is not None
        # Decided FIRST, before any environment is built: a confined (caller's)
        # turn's env is an allowlist (`_confined_env`), so nothing below — the
        # cwd's own git pull included — can hand it a credential.
        _TURN_ENV.confined = _capability(turn) if confined else None
        if confined:
            # No GitHub identity (canopy refuses one for a caller's turn anyway),
            # no chat key, no repo worktrees: only what says who and which turn.
            per_turn = {**_write_envelope(turn), **_lineage_env(turn), **ONE_SHOT_TURN_ENV}
        else:
            # What this turn alone carries: its GitHub identity (first — the cwd's
            # own git pull needs it), its caller envelope and, for a chat, that
            # chat's key.
            try:
                scoped_env = _scoped_token_env(turn)
            except ConfineError as exc:
                _api("POST", f"/turns/{turn_id}/finish",
                     {"status": "failed", "result_note": f"turn not run: {exc}"})
                _log(f"turn {turn_id[:8]} NOT run — {exc}")
                return
            per_turn = {**_github_turn_env(runner_id, turn), **_write_envelope(turn),
                        **_chat_key_env(turn), **_lineage_env(turn), **scoped_env,
                        **ONE_SHOT_TURN_ENV}
        _TURN_ENV.extra = dict(per_turn)
        _TURN_ENV.settings = None
        slug = _turn_agent_slug(turn)
        off_disk_env = False
        if not confined and slug and secrets_off_disk():
            # This turn's secrets, on tmpfs, for this turn only (spec §4). Removed in
            # the `finally` below however the turn ends, and swept at start. A turn
            # whose env cannot be had is NOT run without one: it fails with the
            # reason and no session, which the server re-queues (a sessionless
            # failure is a non-attempt, bounded by MAX_SESSIONLESS_RETRIES).
            # The clone first: whether the agent has an env to resolve at all is read
            # from its template (an agent this box has never seen is bootstrapped now).
            _ensure_agent_clone(slug)
            if _turn_values(slug) is None:
                note = (f"[runner] could not resolve {slug}'s credentials: "
                        f"{_TURN_ENV_ERRORS.get(slug) or 'no answer from the canopy CLI'}")
                until = note_cred_failure(slug)
                _log(f"{slug}: not claiming its turns until "
                     f"{time.strftime('%H:%M:%S', time.gmtime(until))}Z (credentials)")
                _api("POST", f"/turns/{turn_id}/finish", {"status": "failed", "result_note": note})
                _log(f"turn {turn_id[:8]} NOT run — {note}")
                return
            env_path = write_turn_env(slug, str(turn_id))
            if env_path is None:
                note = (f"[runner] could not write {slug}'s per-turn credentials file: "
                        f"{_TURN_ENV_ERRORS.get(slug) or 'no tmpfs root'}")
                _api("POST", f"/turns/{turn_id}/finish", {"status": "failed", "result_note": note})
                _log(f"turn {turn_id[:8]} NOT run — {note}")
                return
            per_turn["CANOPY_AGENT_ENV_FILE"] = str(env_path)
            _TURN_ENV.extra["CANOPY_AGENT_ENV_FILE"] = str(env_path)
            off_disk_env = True
        cwd = _turn_cwd(turn, turn_id, env=_agent_env(slug))
        resume_id = turn.get("_resume_id") or None
        prompt = turn.get("prompt", "")
        if not confined:
            # A caller's confined turn gets no repo checkouts; everyone else may ask.
            _TURN_ENV.extra.update(_repo_worktree_env(turn, turn_id))
            prompt = _with_attachments(prompt, *_fetch_attachments(runner_id, turn))
        if confined:
            # Never resume, never be resumed: an email thread can hold a staff turn
            # (full) and a partner's (confined) on ONE canopy Session, and resuming
            # would carry the full turn's context into the caller's session.
            resume_id = None
            try:
                prompt = _confined_prompt(turn)
                # A confined session reads only its capability's paths, so the
                # files are not fetched — but they are NAMED, never silently lost.
                withheld = [_attachment_name(r.get("filename", "")) for r in
                            (turn.get("origin_ref") or {}).get("attachments") or [] if r.get("id")]
                prompt = _with_attachments(prompt, [], withheld)
                confine_env, caller_path = _confine(turn)
                _TURN_ENV.extra = {**per_turn, **confine_env}
                _TURN_ENV.settings = _native_settings(
                    turn, cwd if cwd is not None else pathlib.Path(WORK_DIR) / turn_id[:8],
                    caller_path)
                if prompt.startswith("/"):
                    first, sep, rest = prompt.partition("\n")
                    prompt = f"{first} --caller {caller_path}{sep}{rest}"
            except ConfineError as exc:
                _api("POST", f"/turns/{turn_id}/finish",
                     {"status": "failed", "result_note": f"a caller's turn was not run: {exc}"})
                _log(f"turn {turn_id[:8]} NOT run — cannot confine: {exc}")
                return

        if turn.get("_fork") and not resume_id and not confined and cwd is not None:
            try:
                resume_id = _cut_fork(cwd, turn["_fork"], _turn_agent_slug(turn))
            except ForkError as exc:
                _api("POST", f"/turns/{turn_id}/finish",
                     {"status": "failed", "result_note": f"Could not fork: {exc}"})
                _log(f"turn {turn_id[:8]} NOT run — could not fork: {exc}")
                return
            _record_session_resume(runner_id, turn, resume_id)

        if resume_id and cwd is not None:
            _adopt_resume_transcript(cwd, resume_id, _turn_agent_slug(turn))

        def emit(events, _tid=turn_id):
            _api("POST", f"/turns/{_tid}/events", {"events": events})

        # Record the turn's session the moment it exists, so the binding is live
        # while the turn runs and the stream tail follows it (_announce_session).
        # A confined turn is never recorded: see the confined branch above.
        recorded = {"sid": ""}

        def _on_session(sid, _turn=turn, _cwd=cwd):
            if sid != recorded["sid"]:
                _record_session_cwd(sid, _cwd)
                _record_session_resume(runner_id, _turn, sid)
                recorded["sid"] = sid

        if not confined:
            with _SESSION_HOOKS_LOCK:
                _SESSION_HOOKS[turn_id] = _on_session
        lease_stop = _start_lease_renewal(runner_id, turn_id)
        try:
            try:
                ok, text, cli_session_id = execute_prompt(
                    prompt, turn_id, emit, cwd=cwd,
                    agent_slug=_turn_agent_slug(turn), resume_session_id=resume_id,
                )
            except Exception as exc:  # never let one turn kill the runner
                ok, text, cli_session_id = False, f"runner error: {exc}", ""
        finally:
            lease_stop.set()
            with _SESSION_HOOKS_LOCK:
                _SESSION_HOOKS.pop(turn_id, None)
        _TURN_ENV.extra = {}
        _TURN_ENV.settings = None
        _TURN_ENV.confined = None
        if cli_session_id and cwd is not None:
            _record_session_cwd(cli_session_id, cwd)
        if cli_session_id:
            # Never let bookkeeping cost us the finish below — an exception here
            # used to strand the turn exactly like a dead socket did (#448).
            for step, fn in (("record session resume", _record_session_resume),
                             ("ship transcript rows", None)):
                if confined and fn is _record_session_resume:
                    continue  # never a resume target: see the confined branch above
                if fn is _record_session_resume and recorded["sid"] == cli_session_id:
                    continue  # already recorded when the session started
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
        if not ok and stop_requested(turn_id):
            # Stopped on request — CANCELLED, never FAILED: a failure starts a
            # debugger turn and, sessionless, would be re-queued and run again.
            # A turn that completed anyway stays DONE; the server notes the
            # ignored stop on its own (services.finish_turn).
            finish = "cancelled"
            text = f"stopped on request — {text}" if text else "stopped on request"
        finish_body = {"status": finish, "result_note": text[:2000]}
        if cli_session_id:
            # The session this turn drove — the key its close-out report and the
            # Turns page's link join on (Turn.session_key, which holds an
            # emdash task name on a laptop and the CLI session id here). Also
            # what tells the server a FAILED turn did reach an agent, so it is
            # never re-run blind as "sessionless" (services.finish_turn).
            finish_body["session_key"] = cli_session_id
        fstatus, _ = _api("POST", f"/turns/{turn_id}/finish", finish_body)
        if ok and off_disk_env:
            # Proven: this agent works off disk, so its legacy files can go.
            note_off_disk_success(slug)
        if fstatus == 200:
            _log(f"finished turn {turn_id[:8]}: {finish}")
        else:
            _log(f"COULD NOT FINISH turn {turn_id[:8]} ({finish} here; canopy-web said "
                 f"{fstatus}) — it will sit EXECUTING until the lease sweep")
        if confined:
            # AFTER the finish, which writes result_note: a caller's turn hands its
            # `For <Agent>'s owner:` line to the note the owner reads, even when the
            # reply above it ran past the 2000 characters kept (canopy-web#1416).
            try:
                from canopy_transcript.owner_ask import find  # noqa: PLC0415

                line = find(text)
                if line:
                    _api("POST", f"/turns/{turn_id}/events",
                         {"events": [{"kind": "owner_ask", "payload": {"line": line}}]})
            except Exception as exc:  # noqa: BLE001 — the transcript is still the record
                _log(f"warn: could not send turn {turn_id[:8]} its owner line: {exc}")
        if cwd is not None:
            # After the finish, never before it: the transcript rows above are
            # read from ~/.claude/projects, not the worktree, but a release that
            # raised must not cost the turn its finish.
            try:
                _release_agent_worktree(cwd)
            except Exception as exc:  # noqa: BLE001
                _log(f"warn: could not release {cwd}: {exc}")
    except Exception as exc:  # noqa: BLE001 — a worker must never take the loop down
        _log(f"turn {turn_id[:8]} worker crashed: {exc}")
    finally:
        # The turn's secrets end with it — done, failed, raised, stopped or timed out.
        remove_turn_env(str(turn_id))
        with _TURNS_LOCK:
            _IN_FLIGHT.pop(turn_id, None)
        _clear_stop(turn_id)


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
        _, beat = _api("POST", f"/runners/{runner_id}/heartbeat", _heartbeat_body(_in_flight_ids()))
        _observe_beat(beat)
        honour_stops(beat)
        maybe_self_refresh(bool((beat or {}).get("refresh_pending")))
        # Attached viewers are served on this path too — a runner that fell back
        # to REST must not also silently stop being watchable.
        _sync_session_views(runner_id)
        # Likewise a sign-in: a box on the REST fallback is EXACTLY the box most
        # likely to need one, and leaving this on the WS path only would strand
        # the operator on the runners that fail most.
        _drain_mint(runner_id)
        _maybe_reread_credentials()
        _credential_tick()
        _drain_inbox(runner_id)
        if (len(_in_flight_ids()) >= MAX_CONCURRENT_TURNS or not _memory_headroom_ok()
                or _token_refused()):
            time.sleep(POLL_SECONDS)
            continue
        paused = backed_off_slugs()
        status, turn = _api("POST", f"/runners/{runner_id}/claim"
                            + (f"?paused={','.join(paused)}" if paused else ""))
        if status != 200 or not turn:
            time.sleep(POLL_SECONDS)
            continue
        turn_id = turn["id"]
        _log(f"claimed turn {turn_id[:8]} target={turn.get('target')} (REST) "
             f"{_provenance_brief(turn)}")
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
    unrelated frames (a wake that arrives mid-request is not what we're waiting
    on right now). Returns the matched frame, or None on close/timeout."""
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
    if not _memory_headroom_ok():
        return False  # the poll tick retries; the queued turn waits, nothing is lost
    if _token_refused():
        return False  # it could never report back; leave it for a box that can
    paused = backed_off_slugs()
    res = _ws_request(ws, {"action": "claim", **({"paused": paused} if paused else {})}, "claim.result")
    turn = res.get("turn") if res else None
    if not turn:
        return False
    tid = turn["id"]
    _log(f"claimed turn {tid[:8]} target={turn.get('target')} (WS) {_provenance_brief(turn)}")
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
                url, header=[f"Authorization: Bearer {_bearer()}"], timeout=HEARTBEAT_SECONDS,
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
            ack = _ws_request(ws, {"action": "heartbeat", **_heartbeat_body(_in_flight_ids())},
                              "heartbeat.ack", timeout=15)
            _observe_beat(ack)
            honour_stops(ack)
            # This thread is the one that claims, so "idle" holds until the
            # restart; see maybe_self_refresh.
            maybe_self_refresh(bool((ack or {}).get("refresh_pending")))

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
                    elif mtype == "cancel":
                        # A person stopped a turn. Only signals the executor;
                        # the turn's worker finishes it CANCELLED.
                        request_stop(str(msg.get("turn_id") or ""), "cancel frame")
                    elif mtype == "session_interrupt":
                        # A stop aimed at a session: the turn driving it, if any.
                        stop_session(str(msg.get("session_key") or ""), "session interrupt")
                    elif mtype == "check_inbox":
                        # Mark it due; the POLL thread does the read. Same split
                        # the laptop uses, for the same reason: a `gog` subprocess
                        # on the wake-listener would block the socket that keeps
                        # this runner alive.
                        core = _inbox_core()
                        if core is not None:
                            core[1].ring(str(msg.get("mailbox") or ""))
                            _log(f"doorbell: {msg.get('mailbox')} marked due")
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
                    _maybe_reread_credentials()
                    _credential_tick()
                    _drain_inbox(runner_id)
                    last_poll = time.monotonic()
                    last_stream = time.monotonic()
                elif time.monotonic() - last_stream >= _stream_interval(time.monotonic()):
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
    if not BASE_URL:
        # A paired box signs in with its device key, and a new one pairs by its
        # instance role (pair_by_role): neither needs CANOPY_TOKEN.
        _log("FATAL: CANOPY_BASE_URL is required")
        sys.exit(1)
    runner_id = pair_or_load()
    global _RUNNER_ID
    _RUNNER_ID = runner_id
    # No turn runs yet: any per-turn env a previous process left is nobody's.
    sweep_turn_envs()
    if UNIT:
        # A unit's key was registered by canopy-pair; it has no instance role to enroll.
        enrollment = threading.Thread(target=lambda: None, daemon=True)
        enrollment.start()
    else:
        # Canopy Vault: register this box's instance role as its device key
        # (background, fail-open; logs the approve URL while pending). canopy-web#1549.
        enrollment = start_aws_device_key_enrollment(runner_id)
    _retire_shared_github_credentials()
    if not fetch_and_stage_credential(runner_id):
        return  # stopped before a credential was provisioned
    # Registering the key can change what canopy-web will accept from this box (a
    # new key waits on a person): settle that BEFORE the bootstrap checks the token,
    # not halfway through it, as on 2026-10-18 02:28.
    enrollment.join(timeout=60)
    bootstrap_agent_fleet()
    # Prefer the WS control channel; fall back to REST polling if it can't be used.
    if not run_over_ws(runner_id):
        run_over_rest(runner_id)


def _handle_stop(*_a):
    global _stop
    _stop = True
    _log("stopping after current turn")


def _units_cli(argv: list[str]) -> int:
    """Root's box-wide steps for isolation units (systemd runs them; see runner.cfn.yaml):
    `--install-units` (the systemd units, once — cloud-init, or an operator moving a
    live box), `--setup-units` (users, env files, secrets, the IMDS rule; every
    start) and `--pair-units` (canopy-pair: every unit in one pair-by-role call)."""
    if os.geteuid() != 0:
        print("--install-units / --setup-units / --pair-units run as root", file=sys.stderr)
        return 2
    if argv[0] == "--install-units":
        services = install_units()
        _log(f"install-units: {', '.join(services) or 'no units'} enabled")
        return 0
    if argv[0] == "--setup-units":
        box = read_env_file(BOX_ENV_FILE)
        units = setup_units(box, role_name=_role_name())
        imds_rules()
        _log("setup-units: " + ", ".join(f"{u['id']} ({u['user']}, {u['name']})" for u in units))
        # A unit added to RunnerUnits later: start its runner, and pair it.
        for u in units:
            subprocess.run(["systemctl", "enable", "--now", "--no-block",
                            f"canopy-runner@{u['id']}.service"], capture_output=True)
        if any(not _unit_state(u).get("runner_id") for u in units):
            subprocess.run(["systemctl", "restart", "--no-block", "canopy-pair.service"], capture_output=True)
        return 0
    return pair_units_main()


if __name__ == "__main__":
    if sys.argv[1:2] == ["repo-worktree"]:
        sys.exit(_repo_worktree_cli(sys.argv[2:]))
    if sys.argv[1:2] in (["--install-units"], ["--setup-units"], ["--pair-units"]):
        sys.exit(_units_cli(sys.argv[1:]))
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    main()
