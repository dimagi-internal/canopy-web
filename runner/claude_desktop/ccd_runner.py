#!/usr/bin/env python3
"""canopy desktop runner — runs canopy turns as Claude desktop-app Code sessions.

The emdash runner (runner/canopy_runner) drives emdash over CDP; the cloud runner
(runner/ec2/cloud_runner.py) runs `claude` headless. This one gives a turn the
Claude desktop app's Code tab: a sidebar session per turn, in its own git
worktree, that a person can open, watch, steer and interrupt, with the app's own
needs-input flags and diffs. It is a separate, stdlib-only program, like the
cloud runner, so the emdash runner is untouched. Spec and test evidence:
canopy-web#1188.

How a turn runs (each step was proven on a background macOS user, see #1188):

1. Claim a turn from canopy-web (kind=desktop runner).
2. A NEW thread: `git worktree add` from the project's checkout, write the
   worktree's `.claude/settings.local.json` (the tool allow-list and the
   `canopy-desktop` mod), and seed a headless `claude -p` session there to get
   a CLI session id. Then hand that session to the app with
   `claude://resume?session=<id>`, which imports it into the sidebar.
   A CONTINUED thread (same thread_key, resolve-session says reuse) skips all
   of that and delivers into the session it already has.
3. The mod, inside the app's session process, finds `<worktree>/.canopy-desktop/`
   and submits `task.txt` (or a `fu-<n>.txt` follow-up) as the person's prompt.
4. This daemon tails the session's CLI transcript
   (~/.claude/projects/*/<session>.jsonl) into canopy-web as turn events and
   the raw transcript, and finishes the turn when the mod reports turn.complete.

Things the design relies on, all measured rather than assumed (#1188):
- The deep link ACTIVATES Claude.app in its own macOS session, even with
  `open -g` and even when hidden. On a dedicated runner account that nobody is
  looking at, that is invisible; on the console user's account it steals focus
  on every new session. Run this on a dedicated account.
- Imported sessions start in permission mode `default` whatever the seed used;
  `permissions.defaultMode` is overridden by the app. `permissions.allow` rules
  in the worktree's settings ARE honoured, so they are how a turn runs unattended.
- The imported session keeps the seed's model, so the seed runs with the
  configured model.
- Quitting the app stops every session process; it must stay running (a login
  item). It may be hidden.

Usage:
  ccd_runner.py pair --config ~/.canopy/desktop/runner.json --token @<file> \
      --workspace dimagi --name <name> --project <name>=<repo path> [...]
  ccd_runner.py run  --config ~/.canopy/desktop/runner.json
  ccd_runner.py install-mod      # register the canopy-desktop marketplace (once)
"""
from __future__ import annotations

import argparse
import getpass
import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIND = "desktop"
MARKETPLACE = "canopy-desktop"
PLUGIN = f"canopy-desktop@{MARKETPLACE}"
CHANNEL = ".canopy-desktop"
CLAUDE_HOME = Path.home() / ".claude" / "projects"

#: A session the mod has not touched `alive` in for this long has no process.
ALIVE_SECONDS = 15
#: How long a turn may run before the runner stops waiting for it. The session
#: keeps running in the app; only canopy's turn is closed out.
TURN_TIMEOUT_SECONDS = int(os.environ.get("CCD_TURN_TIMEOUT", str(4 * 3600)))
#: How long to wait for the app to adopt a session and the mod to submit.
SUBMIT_TIMEOUT_SECONDS = 120
#: Per-request transcript cap is 1 MiB server-side; stay well under it.
TRANSCRIPT_BATCH_BYTES = 512 * 1024

DEFAULT_ALLOW = ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash",
                 "WebFetch", "WebSearch", "TodoWrite", "Task", "Skill"]
SEED_PROMPT = "This session was prepared by canopy. Reply with exactly: ready"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ── config ──────────────────────────────────────────────────────────────────

def read_token(ref: str) -> str:
    """`@path` reads the file (or a KEY=VALUE line named by `@path#KEY`); anything
    else is the token itself."""
    if not ref.startswith("@"):
        return ref.strip()
    path, _, key = ref[1:].partition("#")
    text = Path(os.path.expanduser(path)).read_text()
    if not key:
        return text.strip()
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip().removeprefix("export ").strip() == key:
            return v.strip().strip('"').strip("'")
    raise SystemExit(f"{key} not found in {path}")


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    cfg.setdefault("poll_seconds", 5)
    cfg.setdefault("max_concurrent", 3)
    cfg.setdefault("workdir", str(Path.home() / ".canopy" / "desktop"))
    cfg.setdefault("allow", DEFAULT_ALLOW)
    cfg.setdefault("model", "")
    cfg.setdefault("projects", {})
    return cfg


# ── control plane ───────────────────────────────────────────────────────────

class Api:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/") + "/api/harness"
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None,
             tries: int = 3) -> tuple[int, dict | None]:
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "User-Agent": "canopy-desktop-runner/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt == tries - 1:
                    detail = exc.read()[:500].decode(errors="replace")
                    log(f"{method} {path} -> {exc.code} {detail}")
                    return exc.code, None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == tries - 1:
                    log(f"{method} {path} failed: {exc}")
                    return 0, None
            time.sleep(2 * (attempt + 1))
        return 0, None


# ── pure helpers (unit-tested) ──────────────────────────────────────────────

def thread_key(turn: dict) -> str:
    """Continuity is opt-in, as in the other runners: an explicit thread_key /
    thread_id continues a session; without one, a turn is its own session."""
    ref = turn.get("origin_ref") or {}
    return ref.get("thread_key") or ref.get("thread_id") or f"turn:{turn['id']}"


def slug_for(turn: dict) -> str:
    """A branch/dir-safe name for a new thread's worktree."""
    base = re.sub(r"[^a-z0-9]+", "-", (turn.get("project") or "turn").lower()).strip("-")
    return f"{base or 'turn'}-{str(turn['id'])[:8]}"


def worktree_settings(allow: list[str], existing: dict | None = None) -> dict:
    """The worktree's settings.local.json: the runner's allow-list merged over
    whatever is there (the plugin install writes enabledPlugins)."""
    out = dict(existing or {})
    perms = dict(out.get("permissions") or {})
    perms["allow"] = sorted(set(perms.get("allow") or []) | set(allow))
    out["permissions"] = perms
    plugins = dict(out.get("enabledPlugins") or {})
    plugins[PLUGIN] = True
    out["enabledPlugins"] = plugins
    return out


def events_from_record(rec: dict) -> list[dict]:
    """Turn events (status/assistant/tool_start/tool_end) from one transcript record."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    if rec.get("type") == "assistant":
        for block in content:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append({"kind": "assistant", "payload": {"text": block["text"]}})
            elif block.get("type") == "tool_use":
                out.append({"kind": "tool_start", "payload": {
                    "id": block.get("id", ""), "name": block.get("name", ""),
                    "input": block.get("input")}})
    elif rec.get("type") == "user":
        for block in content:
            if block.get("type") == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
                out.append({"kind": "tool_end", "payload": {
                    "tool_use_id": block.get("tool_use_id", ""),
                    "is_error": bool(block.get("is_error")),
                    "content": str(body or "")[:4000]}})
    return out


def batches(lines: list[str], limit: int = TRANSCRIPT_BATCH_BYTES) -> list[list[str]]:
    out, cur, size = [], [], 0
    for line in lines:
        n = len(line.encode())
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += n
    if cur:
        out.append(cur)
    return out


def read_events(channel: Path) -> list[dict]:
    evs = []
    for p in channel.glob("ev-*.json"):
        try:
            evs.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(evs, key=lambda e: e.get("t", 0))


def next_followup_index(channel: Path) -> int:
    n = 1
    while (channel / f"fu-{n}.txt").exists():
        n += 1
    return n


# ── the desktop app ─────────────────────────────────────────────────────────

def app_running() -> bool:
    r = subprocess.run(["pgrep", "-U", str(os.getuid()), "-f", "Claude.app/Contents/MacOS/Claude$"],
                       capture_output=True)
    return r.returncode == 0


def ensure_app() -> bool:
    if app_running():
        return True
    subprocess.run(["open", "-g", "-j", "-a", "Claude"], check=False)
    for _ in range(30):
        time.sleep(1)
        if app_running():
            return True
    return False


def open_session(cli_session_id: str) -> None:
    """Import (or wake) a CLI session in the app. Activates the app in THIS macOS
    session — see the module docstring for why that is acceptable here."""
    subprocess.run(["open", "-g", f"claude://resume?session={cli_session_id}"], check=False)


def transcript_path(cli_session_id: str) -> Path | None:
    hits = glob.glob(str(CLAUDE_HOME / "*" / f"{cli_session_id}.jsonl"))
    return Path(hits[0]) if hits else None


def channel_alive(channel: Path) -> bool:
    try:
        return time.time() - int((channel / "alive").read_text()) / 1000 < ALIVE_SECONDS
    except (OSError, ValueError):
        return False


# ── worktree + session creation ─────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_worktree(cfg: dict, repo: Path, slug: str) -> Path:
    wt = Path(cfg["workdir"]) / "worktrees" / repo.name / slug
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", f"canopy/{slug}", str(wt), "HEAD")
    # Keep the runner's own files out of the repo's status and diffs.
    common = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text() if exclude.exists() else ""
    for pat in (f"{CHANNEL}/", ".claude/settings.local.json"):
        if pat not in have.splitlines():
            have += ("" if have.endswith("\n") or not have else "\n") + pat + "\n"
    exclude.write_text(have)
    return wt


def prepare_settings(cfg: dict, wt: Path) -> None:
    subprocess.run(["claude", "plugin", "install", PLUGIN, "--scope", "local"],
                   cwd=wt, capture_output=True, text=True, check=False)
    path = wt / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps(worktree_settings(cfg["allow"], existing), indent=2) + "\n")


def seed_session(cfg: dict, wt: Path) -> str:
    argv = ["claude", "-p", SEED_PROMPT, "--output-format", "json"]
    if cfg.get("model"):
        argv += ["--model", cfg["model"]]
    out = subprocess.run(argv, cwd=wt, capture_output=True, text=True, timeout=300)
    try:
        return json.loads(out.stdout)["session_id"]
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"seed session failed: {out.stderr[-500:] or out.stdout[-500:]}") from exc


# ── one turn ────────────────────────────────────────────────────────────────

class TurnRun:
    def __init__(self, api: Api, cfg: dict, turn: dict):
        self.api, self.cfg, self.turn = api, cfg, turn
        self.id = str(turn["id"])
        self.sent_lines = 0

    def emit(self, events: list[dict]) -> None:
        if events:
            self.api.call("POST", f"/turns/{self.id}/events", {"events": events})

    def status(self, status: str, **extra) -> None:
        self.emit([{"kind": "status", "payload": {"status": status, **extra}}])

    def finish(self, ok: bool, note: str, session_key: str = "") -> None:
        body = {"status": "done" if ok else "failed", "result_note": note[:2000]}
        if session_key:
            body["session_key"] = session_key
        self.api.call("POST", f"/turns/{self.id}/finish", body)
        log(f"turn {self.id[:8]} finished: {body['status']} — {note[:120]}")

    def _project_repo(self) -> Path:
        project = self.turn.get("project") or self.turn.get("agent_slug") or ""
        repo = (self.cfg["projects"] or {}).get(project)
        if not repo:
            raise RuntimeError(f"this runner has no checkout for '{project}'")
        return Path(os.path.expanduser(repo))

    def _resolve(self) -> str:
        """The CLI session id this thread already has here, or ""."""
        body = {"thread_key": thread_key(self.turn)}
        if self.turn.get("project"):
            body.update(project=self.turn["project"], workspace=self.turn.get("workspace_slug") or "")
        else:
            body["agent_slug"] = self.turn.get("agent_slug") or ""
        status, plan = self.api.call("POST", f"/runners/{self.cfg['runner_id']}/resolve-session", body)
        if status == 200 and plan and plan.get("reuse"):
            return plan.get("session_key") or plan.get("emdash_task_id") or ""
        return ""

    def _record(self, sid: str, wt: Path) -> None:
        body = {"thread_key": thread_key(self.turn), "session_key": sid, "session_id": sid,
                "turn_id": self.id, "summary": f"desktop session in {wt}"}
        if self.turn.get("project"):
            body.update(project=self.turn["project"], workspace=self.turn.get("workspace_slug") or "")
        else:
            body["agent_slug"] = self.turn.get("agent_slug") or ""
        self.api.call("POST", f"/runners/{self.cfg['runner_id']}/record-session", body)

    def _sessions_index(self) -> Path:
        return Path(self.cfg["workdir"]) / "sessions.json"

    def _worktree_for(self, sid: str) -> Path | None:
        try:
            wt = json.loads(self._sessions_index().read_text()).get(sid)
        except (OSError, ValueError):
            return None
        return Path(wt) if wt and Path(wt).exists() else None

    def _remember(self, sid: str, wt: Path) -> None:
        idx = self._sessions_index()
        try:
            data = json.loads(idx.read_text())
        except (OSError, ValueError):
            data = {}
        data[sid] = str(wt)
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(json.dumps(data, indent=2))

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 — one turn must never take the loop down
            log(f"turn {self.id[:8]} crashed: {exc}")
            self.finish(False, f"desktop runner error: {exc}")

    def _run(self) -> None:
        prompt = self.turn.get("prompt") or ""
        if not prompt and self.turn.get("agent_slug"):
            prompt = f"/{self.turn['agent_slug']}:turn"
        if not ensure_app():
            self.finish(False, "Claude.app is not running and could not be started")
            return
        sid = self._resolve()
        wt = self._worktree_for(sid) if sid else None
        offset = 0
        if sid and wt:
            channel = wt / CHANNEL
            # This turn's record starts where the session's transcript ends now;
            # what is above it belongs to the turns before.
            path = transcript_path(sid)
            offset = path.stat().st_size if path else 0
            n = next_followup_index(channel)
            (channel / f"fu-{n}.txt").write_text(prompt)
            submitted_as = f"fu-{n}"
            self.api.call("POST", f"/turns/{self.id}/start", {"session_id": sid})
            woke = not channel_alive(channel)
            if woke:
                open_session(sid)  # its process was stopped (app restart, idle timeout)
            self.status("reused_session", session=sid, woke=woke)
        else:
            repo = self._project_repo()
            wt = make_worktree(self.cfg, repo, slug_for(self.turn))
            prepare_settings(self.cfg, wt)
            channel = wt / CHANNEL
            channel.mkdir(exist_ok=True)
            sid = seed_session(self.cfg, wt)
            (channel / "task.txt").write_text(prompt)
            (channel / "seeded").write_text(sid)
            submitted_as = "task"
            self._remember(sid, wt)
            self.api.call("POST", f"/turns/{self.id}/start", {"session_id": sid})
            self._record(sid, wt)
            open_session(sid)
            self.status("created_session", session=sid, worktree=str(wt))
        self._follow(sid, channel, submitted_as, offset)

    def _follow(self, sid: str, channel: Path, which: str, offset: int = 0) -> None:
        deadline_submit = time.time() + SUBMIT_TIMEOUT_SECONDS
        deadline = time.time() + TURN_TIMEOUT_SECONDS
        submitted_at = None
        reported_asks: set[str] = set()
        while time.time() < deadline:
            evs = read_events(channel)
            if submitted_at is None:
                hit = next((e for e in evs if e["kind"] == "submitted"
                            and (e.get("extra") or {}).get("which") == which), None)
                err = next((e for e in evs if e["kind"] == "submit.error"
                            and (e.get("extra") or {}).get("which") == which), None)
                if err:
                    self.finish(False, f"the app refused the prompt: {err['extra'].get('err')}", sid)
                    return
                if hit:
                    submitted_at = hit["t"]
                    self.status("submitted", session=sid)
                elif time.time() > deadline_submit:
                    self.finish(False, "the desktop session never picked up the prompt "
                                "(is the canopy-desktop mod installed and Claude.app signed in?)", sid)
                    return
            offset = self._ship_transcript(sid, offset)
            if submitted_at is not None:
                for e in evs:
                    if e["kind"] == "ask" and e["t"] >= submitted_at:
                        key = f"{e['t']}:{e['extra'].get('tool')}"
                        if key not in reported_asks:
                            reported_asks.add(key)
                            self.status("needs_input", session=sid, tool=e["extra"].get("tool"),
                                        note="a permission card is waiting in the Claude app")
                done = next((e for e in evs if e["kind"] == "turn.complete"
                             and e["t"] >= submitted_at), None)
                if done:
                    time.sleep(1.5)  # let the transcript's last records land
                    self._ship_transcript(sid, offset)
                    extra = done.get("extra") or {}
                    ok = not extra.get("isAborted")
                    self.finish(ok, extra.get("answer") or ("aborted" if not ok else "done"), sid)
                    return
            time.sleep(2)
        self.finish(False, "turn timed out; the session keeps running in the Claude app", sid)

    def _ship_transcript(self, sid: str, offset: int) -> int:
        """Ship new transcript records since `offset` (a byte offset), as both raw
        lines and readable turn events. Returns the new offset."""
        path = transcript_path(sid)
        if path is None:
            return offset
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        if not chunk:
            return offset
        complete = chunk[: chunk.rfind(b"\n") + 1]  # never ship a half-written line
        if not complete:
            return offset
        lines = [ln for ln in complete.decode(errors="replace").splitlines() if ln.strip()]
        for batch in batches(lines):
            self.api.call("POST", f"/turns/{self.id}/transcript",
                          {"lines": batch, "batch_id": uuid.uuid4().hex})
        events: list[dict] = []
        for ln in lines:
            try:
                events += events_from_record(json.loads(ln))
            except ValueError:
                continue
        self.emit(events)
        return offset + len(complete)


# ── loop ────────────────────────────────────────────────────────────────────

class Runner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api = Api(cfg["base_url"], read_token(cfg["token"]))
        self.rid = cfg["runner_id"]
        self.inflight: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def active(self) -> list[str]:
        with self.lock:
            for tid in [t for t, th in self.inflight.items() if not th.is_alive()]:
                self.inflight.pop(tid)
            return sorted(self.inflight)

    def heartbeat(self) -> None:
        ready = app_running()
        self.api.call("POST", f"/runners/{self.rid}/heartbeat", {
            "active_turn_ids": self.active(), "degraded": False, "note": "",
            "host": f"{getpass.getuser()}@{socket.gethostname()}",
            "ready": ready, "ready_note": "" if ready else "Claude.app is not running",
            "projects": sorted(self.cfg["projects"]),
            "code_branch": "", "code_version": "claude-desktop-0.1", "code_sha": "",
        })

    def tick(self) -> bool:
        self.heartbeat()
        if len(self.active()) >= self.cfg["max_concurrent"]:
            return False
        status, turn = self.api.call("POST", f"/runners/{self.rid}/claim", tries=1)
        if status != 200 or not turn:
            return False
        log(f"claimed turn {str(turn['id'])[:8]} project={turn.get('project')} "
            f"agent={turn.get('agent_slug')} thread={thread_key(turn)}")
        run = TurnRun(self.api, self.cfg, turn)
        th = threading.Thread(target=run.run, daemon=True, name=f"turn-{run.id[:8]}")
        with self.lock:
            self.inflight[run.id] = th
        th.start()
        return True

    def loop(self) -> None:
        log(f"desktop runner {self.rid} polling {self.cfg['base_url']} every "
            f"{self.cfg['poll_seconds']}s; projects={sorted(self.cfg['projects'])}")
        ensure_app()
        while True:
            try:
                claimed = self.tick()
            except Exception as exc:  # noqa: BLE001
                log(f"tick failed: {exc}")
                claimed = False
            if not claimed:
                time.sleep(self.cfg["poll_seconds"])


# ── commands ────────────────────────────────────────────────────────────────

def cmd_install_mod(_args) -> int:
    for argv in (["claude", "plugin", "marketplace", "add", str(HERE)],
                 ["claude", "plugin", "marketplace", "update", MARKETPLACE]):
        r = subprocess.run(argv, capture_output=True, text=True)
        print((r.stdout or r.stderr).strip().splitlines()[-1:] or "")
    return 0


def cmd_pair(args) -> int:
    path = Path(os.path.expanduser(args.config))
    projects = {}
    for spec in args.project:
        name, _, repo = spec.partition("=")
        if not repo or not Path(os.path.expanduser(repo)).exists():
            raise SystemExit(f"--project {spec!r}: want NAME=PATH to an existing checkout")
        projects[name] = repo
    if path.exists():
        cfg = load_config(path)
        cfg["projects"].update(projects)
        path.write_text(json.dumps(cfg, indent=2))
        print(f"already paired as {cfg['runner_id']}; projects now {sorted(cfg['projects'])}")
        return 0
    api = Api(args.base_url, read_token(args.token))
    status, runner = api.call("POST", "/runners/", {
        "name": args.name, "kind": KIND, "workspace": args.workspace,
        "host": f"{getpass.getuser()}@{socket.gethostname()}",
        # sessions:false keeps this box out of the open pool of unbound chat
        # sessions; it runs its projects' turns and anything pinned to it.
        "capabilities": {"projects": sorted(projects), "sessions": False}})
    if status != 201 or not runner:
        raise SystemExit(f"pairing failed ({status})")
    cfg = {"base_url": args.base_url, "token": args.token, "runner_id": runner["id"],
           "projects": projects, "model": args.model}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    os.chmod(path, 0o600)
    print(f"paired {args.name} as {runner['id']} (kind={KIND}); config {path}")
    return 0


def cmd_run(args) -> int:
    Runner(load_config(Path(os.path.expanduser(args.config)))).loop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("pair")
    pr.add_argument("--config", default="~/.canopy/desktop/runner.json")
    pr.add_argument("--base-url", default="https://labs.connect.dimagi.com/canopy")
    pr.add_argument("--token", required=True, help="a PERSON's canopy-web token: '@file', '@file#KEY' or the value")
    pr.add_argument("--workspace", default="dimagi")
    pr.add_argument("--name", default=f"{getpass.getuser()}-desktop")
    pr.add_argument("--project", action="append", default=[], help="NAME=PATH (repeatable)")
    pr.add_argument("--model", default="")
    pr.set_defaults(fn=cmd_pair)
    rn = sub.add_parser("run")
    rn.add_argument("--config", default="~/.canopy/desktop/runner.json")
    rn.set_defaults(fn=cmd_run)
    im = sub.add_parser("install-mod")
    im.set_defaults(fn=cmd_install_mod)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
