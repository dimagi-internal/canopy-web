"""The Claude desktop session runtime — a second backend behind `execute_turn`.

A laptop runner opens new sessions in emdash by default. When its owner sets the
runner's runtime to `claude-desktop` on canopy-web (Supervisor → the runner →
Session runtime), new sessions open in the Claude desktop app's Code tab instead:
a sidebar session per thread, in its own git worktree, with the app's own
needs-input flags and diffs. The runner reads the setting off every heartbeat
response, so a flip needs no restart. Design and the focus investigation:
canopy-web#1188.

WHICH BACKEND A TURN GETS
  - A thread whose session is a desktop session continues there, whatever the
    current runtime — flipping back to emdash never strands a live desktop thread.
  - A thread whose session is an emdash task continues in emdash, the same way —
    so flipping to claude-desktop never strands a live emdash session either
    (including the one you may be typing in).
  - Only a NEW thread takes the current runtime.
  - A caller's confined turn always goes to emdash: confinement is enforced on the
    emdash path (profile files + native deny rules), and this backend has none.

HOW A DESKTOP SESSION IS DRIVEN (each step proven on a background macOS user, #1188)
  1. `git worktree add` from the project's checkout (the repo emdash already
     registers for that project, so the runner reports and routes exactly the
     same projects either way), at the project's base ref.
  2. The worktree's `.claude/settings.local.json` gets the `canopy-desktop` mod and
     the tool allow-list. The app launches sessions with `--permission-mode
     default` whatever the settings say, so `permissions.allow` is what lets a
     turn run unattended; anything else asks, and the ask is relayed.
  3. A headless `claude -p` seed gives a CLI session id; `claude://resume?session=`
     hands it to the app, which imports it into the sidebar.
  4. The mod, inside the app's session process, submits `task.txt` (a follow-up:
     `fu-<n>.txt`) as the person's own words, stamps `alive`, and reports
     `turn.complete` and permission asks — all through files in
     `<worktree>/.canopy-desktop/`. It holds no credential.
  5. A follow-up into a session whose process has stopped (idle timeout, app
     restart: `alive` goes stale) wakes it with the same deep link.

THE DEEP LINK ACTIVATES CLAUDE.APP in its macOS session, even with `open -g` and
even when hidden. On a fast-user-switched account nobody is looking at, that is
invisible; on the account a person is typing in, it takes focus once per NEW
session (follow-ups into a live session do not). That is why this runtime suits
the background runner accounts.

TURN LIFECYCLE matches the emdash backend: an agent/project turn finishes once
its prompt is delivered (the work continues in the visible session, and the
session's transcript streams through the usual session-stream path); a chat turn
stays open, streams the reply as turn events + raw transcript, and finishes on
the session's turn.complete.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import caller, session_naming

logger = logging.getLogger("canopy_runner.desktop")

EMDASH = "emdash"
CLAUDE_DESKTOP = "claude-desktop"
RUNTIMES = (EMDASH, CLAUDE_DESKTOP)

MARKETPLACE = "canopy-desktop"
PLUGIN = f"canopy-desktop@{MARKETPLACE}"
CHANNEL = ".canopy-desktop"
MOD_SRC = Path(__file__).resolve().parent / "desktop_mod"

#: A session the mod has not stamped `alive` in for this long has no process.
ALIVE_SECONDS = 15
#: How long to wait for the app to adopt a session and the mod to submit.
SUBMIT_TIMEOUT_SECONDS = 180
#: How long a chat turn may run before the runner stops waiting (the session
#: keeps running in the app; only canopy's turn is closed out).
CHAT_TIMEOUT_SECONDS = 4 * 3600
#: Per-request transcript cap is 1 MiB server-side; stay well under it.
TRANSCRIPT_BATCH_BYTES = 512 * 1024

DEFAULT_ALLOW = ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash",
                 "WebFetch", "WebSearch", "TodoWrite", "Task", "Skill", "mcp__*"]
SEED_PROMPT = "This session was prepared by canopy. Reply with exactly: ready"

# The runtime canopy-web last told us (heartbeat response `engine`). Starts at the
# default so a server that predates the field leaves the runner on emdash.
_current = EMDASH
_lock = threading.Lock()
#: turn_id -> thread, for desktop turns still being followed (rides the heartbeat
#: so their leases are renewed).
IN_FLIGHT: dict[str, threading.Thread] = {}


# ── runtime state ───────────────────────────────────────────────────────────

def current() -> str:
    return _current


def observe(me: dict | None) -> None:
    """Adopt the runtime canopy-web reports on the heartbeat response. Absent or
    unknown leaves it unchanged — an old server must never flip a box."""
    global _current
    engine = (me or {}).get("engine")
    if engine in RUNTIMES and engine != _current:
        logger.info("session runtime: %s -> %s (new sessions only; live ones stay put)",
                    _current, engine)
        _current = engine
        if engine == CLAUDE_DESKTOP:
            try:
                ensure_mod()
            except Exception:  # noqa: BLE001 — a turn will say so if the mod is missing
                logger.warning("could not register the canopy-desktop mod", exc_info=True)


def in_flight() -> list[str]:
    with _lock:
        for tid in [t for t, th in IN_FLIGHT.items() if not th.is_alive()]:
            IN_FLIGHT.pop(tid, None)
        return sorted(IN_FLIGHT)


# ── paths and the session index ─────────────────────────────────────────────

def _root(cfg) -> Path:
    return Path(getattr(cfg, "desktop_dir", "") or (Path.home() / ".canopy" / "desktop"))


def _index_path(cfg) -> Path:
    return _root(cfg) / "sessions.json"


def _index(cfg) -> dict:
    try:
        return json.loads(_index_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def _remember(cfg, sid: str, wt: Path, project: str) -> None:
    with _lock:
        data = _index(cfg)
        data[sid] = {"worktree": str(wt), "project": project, "created_at": int(time.time())}
        path = _index_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)


def worktree_for(cfg, sid: str) -> Path | None:
    entry = _index(cfg).get(sid or "")
    if not entry:
        return None
    wt = Path(entry["worktree"])
    return wt if wt.exists() else None


def is_desktop_session(cfg, sid: str) -> bool:
    return bool(sid) and sid in _index(cfg)


def transcript_path(sid: str, claude_home: Path | None = None) -> Path | None:
    home = claude_home or (Path.home() / ".claude" / "projects")
    hits = glob.glob(str(home / "*" / f"{sid}.jsonl"))
    return Path(hits[0]) if hits else None


def transcript_for(session_key: str, *, home: Path, claude_home: Path) -> Path | None:
    """The transcript of a DESKTOP session, or None for anything else — the hook
    `transcript.resolve_transcript` asks first, so session streams and backfills
    work for desktop sessions with no other change. Reads the index under `home`
    (the default desktop dir) because that resolver is handed no config."""
    try:
        index = json.loads((home / ".canopy" / "desktop" / "sessions.json").read_text())
    except (OSError, ValueError):
        return None
    if not session_key or session_key not in index:
        return None
    return transcript_path(session_key, claude_home)


# ── project checkouts ───────────────────────────────────────────────────────

def project_repo(cfg, project: str) -> tuple[Path, str]:
    """(checkout, base ref) for a project — the repo emdash already registers for
    it, so this runtime drives exactly the projects the runner reports. A
    `desktop_projects` map in runner.json overrides (name -> path)."""
    override = (getattr(cfg, "desktop_projects", None) or {}).get(project)
    if override:
        return Path(os.path.expanduser(override)), "HEAD"
    db = getattr(cfg, "emdash_db", "")
    if db and Path(db).exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT w.path, p.base_ref FROM projects p JOIN workspaces w "
                "ON w.id = p.repository_workspace_id "
                "WHERE p.name = ? AND p.deleted_at IS NULL", (project,)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            return Path(row[0]), (row[1] or "HEAD")
    raise RuntimeError(f"no checkout for project '{project}' on this runner")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True, timeout=120).stdout.strip()


def make_worktree(cfg, repo: Path, base_ref: str, name: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:80] or uuid.uuid4().hex[:8]
    wt = _root(cfg) / "worktrees" / repo.name / slug
    wt.parent.mkdir(parents=True, exist_ok=True)
    if "/" in base_ref:  # origin/main: bring it current, best-effort
        remote, _, branch = base_ref.partition("/")
        _git(repo, "fetch", "-q", remote, branch, check=False)
    _git(repo, "worktree", "add", "-q", "-b", f"canopy-desktop/{slug}", str(wt), base_ref)
    # Keep the runtime's own files out of the repo's status and diffs.
    common = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text() if exclude.exists() else ""
    for pat in (f"{CHANNEL}/", ".claude/settings.local.json"):
        if pat not in have.splitlines():
            have += ("" if not have or have.endswith("\n") else "\n") + pat + "\n"
    exclude.write_text(have)
    return wt


def worktree_settings(allow: list[str], existing: dict | None = None) -> dict:
    out = dict(existing or {})
    perms = dict(out.get("permissions") or {})
    perms["allow"] = sorted(set(perms.get("allow") or []) | set(allow))
    out["permissions"] = perms
    plugins = dict(out.get("enabledPlugins") or {})
    plugins[PLUGIN] = True
    out["enabledPlugins"] = plugins
    return out


def prepare_settings(cfg, wt: Path) -> None:
    # `install` alone reuses whatever version is already in Claude Code's plugin
    # cache; `update` is what brings in the mod a runner upgrade shipped.
    for verb in ("install", "update"):
        subprocess.run(["claude", "plugin", verb, PLUGIN, "--scope", "local"],
                       cwd=wt, capture_output=True, text=True, check=False, timeout=120)
    path = wt / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text()) if path.exists() else {}
    allow = getattr(cfg, "desktop_allow", None) or DEFAULT_ALLOW
    path.write_text(json.dumps(worktree_settings(allow, existing), indent=2) + "\n")


def ensure_mod() -> None:
    """Register the mod's marketplace from a FIXED copy under ~/.canopy/desktop, so a
    runner upgrade (a new install path) never leaves Claude Code pointing at a
    deleted directory; `marketplace update` refreshes the plugin cache."""
    dest = Path.home() / ".canopy" / "desktop" / "marketplace"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(MOD_SRC, dest)
    listed = subprocess.run(["claude", "plugin", "marketplace", "list"],
                            capture_output=True, text=True, timeout=60).stdout
    if MARKETPLACE not in listed:
        subprocess.run(["claude", "plugin", "marketplace", "add", str(dest)],
                       capture_output=True, text=True, timeout=120)
    subprocess.run(["claude", "plugin", "marketplace", "update", MARKETPLACE],
                   capture_output=True, text=True, timeout=120)


def seed_session(cfg, wt: Path) -> str:
    argv = ["claude", "-p", SEED_PROMPT, "--output-format", "json"]
    model = getattr(cfg, "desktop_model", "")
    if model:
        argv += ["--model", model]
    out = subprocess.run(argv, cwd=wt, capture_output=True, text=True, timeout=300)
    try:
        return json.loads(out.stdout)["session_id"]
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"seeding the session failed: {(out.stderr or out.stdout)[-400:]}") from exc


# ── the app ─────────────────────────────────────────────────────────────────

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


def open_session(sid: str) -> None:
    subprocess.run(["open", "-g", f"claude://resume?session={sid}"], check=False)


def channel_alive(channel: Path) -> bool:
    try:
        return time.time() - int((channel / "alive").read_text()) / 1000 < ALIVE_SECONDS
    except (OSError, ValueError):
        return False


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


# ── transcript → turn events ────────────────────────────────────────────────

def events_from_record(rec: dict) -> list[dict]:
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


# ── routing a claimed turn ──────────────────────────────────────────────────

def maybe_execute(cfg, client, runner_id: str, turn: dict, thread_key: str) -> str | None:
    """Take this turn into the desktop runtime and return an action string, or
    return None to leave it to the emdash backend (see the module docstring for
    which turns are which)."""
    if caller.capability(turn) is not None:
        return None
    if _current == EMDASH and not _index(cfg):
        return None  # an emdash-only box: no extra round trip, no behaviour change
    plan = client.resolve_session(runner_id, turn.get("agent_slug") or "", thread_key,
                                  project=turn.get("project") or "",
                                  workspace=turn.get("workspace_slug") or "")
    key = plan.get("emdash_task_id") if plan.get("reuse") else ""
    if key and is_desktop_session(cfg, key):
        run = TurnRun(cfg, client, runner_id, turn, thread_key, plan, reuse=key)
    elif key or _current != CLAUDE_DESKTOP:
        return None  # an emdash thread continues in emdash
    else:
        run = TurnRun(cfg, client, runner_id, turn, thread_key, plan, reuse="")
    th = threading.Thread(target=run.run, daemon=True, name=f"desktop-{str(turn['id'])[:8]}")
    with _lock:
        IN_FLIGHT[str(turn["id"])] = th
    th.start()
    return f"desktop:{'reuse' if key else 'create'}:{turn['id']}"


class TurnRun:
    def __init__(self, cfg, client, runner_id: str, turn: dict, thread_key: str,
                 plan: dict, reuse: str):
        self.cfg, self.client, self.rid = cfg, client, runner_id
        self.turn, self.thread_key, self.plan, self.reuse = turn, thread_key, plan, reuse
        self.id = str(turn["id"])
        self.chat = bool((turn.get("origin_ref") or {}).get("chat_session_id"))
        self.target = turn.get("agent_slug") or turn.get("project") or ""

    def status(self, status: str, **extra) -> None:
        try:
            self.client.post_events(self.id, [{"kind": "status", "payload": {"status": status, **extra}}])
        except Exception:  # noqa: BLE001 — narration must not fail a live turn
            logger.warning("status event failed for turn=%s", self.id, exc_info=True)

    def finish(self, ok: bool, note: str, sid: str = "") -> None:
        if ok:
            self.client.finish(self.id, note[:2000], status="done", emdash_task_id=sid)
        elif sid:
            # Failed AFTER reaching a session: say which, so the server never
            # re-runs it blind (same rule as the emdash path).
            self.client.finish(self.id, note[:2000], status="failed", emdash_task_id=sid)
        else:
            self.client.fail_turn(self.id, note[:2000])
        logger.info("desktop turn=%s %s: %s", self.id, "done" if ok else "failed", note[:160])

    def _prompt(self) -> str:
        prompt = self.turn.get("prompt") or (f"/{self.target}:turn" if not self.chat else "")
        envelope = caller.write_caller_file(self.turn)
        return caller.with_caller_flag(prompt, envelope) if not self.chat else prompt

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 — one turn must never take the loop down
            logger.exception("desktop turn=%s crashed", self.id)
            try:
                self.finish(False, f"Claude desktop runtime error: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        if not ensure_app():
            self.client.start(self.id)
            self.finish(False, "Claude.app is not running and could not be started")
            return
        prompt = self._prompt()
        offset = 0
        if self.reuse:
            sid = self.reuse
            wt = worktree_for(self.cfg, sid)
            if wt is None:
                self.client.start(self.id, sid)
                self.finish(False, f"desktop session {sid} has no worktree on this box any more", sid)
                return
            channel = wt / CHANNEL
            path = transcript_path(sid)
            offset = path.stat().st_size if path else 0  # this turn's record starts here
            n = next_followup_index(channel)
            (channel / f"fu-{n}.txt").write_text(prompt)
            which = f"fu-{n}"
            self.client.start(self.id, sid)
            woke = not channel_alive(channel)
            if woke:
                open_session(sid)  # its process stopped: idle timeout or app restart
            self.status("reused_session", runtime=CLAUDE_DESKTOP, session=sid, woke=woke)
        else:
            repo, base = project_repo(self.cfg, self.target)
            name = session_naming.build_task_name(self.target, self.turn)
            wt = make_worktree(self.cfg, repo, base, name)
            prepare_settings(self.cfg, wt)
            channel = wt / CHANNEL
            channel.mkdir(exist_ok=True)
            summary = self.plan.get("summary") or ""
            if summary:
                prompt = (f"[Continuing prior work on this thread — context from earlier sessions "
                          f"(a fresh session, possibly a different machine):]\n{summary}\n\n{prompt}")
            sid = seed_session(self.cfg, wt)
            (channel / "task.txt").write_text(prompt)
            (channel / "seeded").write_text(sid)
            which = "task"
            _remember(self.cfg, sid, wt, self.target)
            self.client.start(self.id, sid)
            self.client.record_session(
                self.rid, self.turn.get("agent_slug") or "", self.thread_key,
                project=self.turn.get("project") or "",
                workspace=self.turn.get("workspace_slug") or "",
                emdash_task_id=sid, session_id=sid, summary=None)
            open_session(sid)
            self.status("created_session", runtime=CLAUDE_DESKTOP, session=sid, worktree=str(wt))
        self._follow(sid, channel, which, offset)

    def _follow(self, sid: str, channel: Path, which: str, offset: int) -> None:
        submit_by = time.time() + SUBMIT_TIMEOUT_SECONDS
        deadline = time.time() + CHAT_TIMEOUT_SECONDS
        submitted_at = None
        asks: set[str] = set()
        while time.time() < deadline:
            evs = read_events(channel)
            if submitted_at is None:
                err = next((e for e in evs if e.get("kind") == "submit.error"
                            and (e.get("extra") or {}).get("which") == which), None)
                if err:
                    self.finish(False, f"the app refused the prompt: {(err.get('extra') or {}).get('err')}", sid)
                    return
                hit = next((e for e in evs if e.get("kind") == "submitted"
                            and (e.get("extra") or {}).get("which") == which), None)
                if hit:
                    submitted_at = hit["t"]
                    self.status("submitted", session=sid)
                    if not self.chat:
                        # Agent/project turn: delivered is done, as on emdash — the
                        # work continues in the visible session.
                        self.finish(True, f"delivered to Claude desktop session {sid}", sid)
                        return
                elif time.time() > submit_by:
                    self.finish(False, "the Claude desktop session never picked up the prompt "
                                "(is Claude.app signed in, and the canopy-desktop mod installed?)", sid)
                    return
            offset = self._ship(sid, offset)
            if submitted_at is not None:
                for e in evs:
                    if e.get("kind") == "ask" and e.get("t", 0) >= submitted_at:
                        key = f"{e['t']}:{(e.get('extra') or {}).get('tool')}"
                        if key not in asks:
                            asks.add(key)
                            self.status("needs_input", session=sid,
                                        tool=(e.get("extra") or {}).get("tool"),
                                        note="a permission card is waiting in the Claude app")
                done = next((e for e in evs if e.get("kind") == "turn.complete"
                             and e.get("t", 0) >= submitted_at), None)
                if done:
                    time.sleep(1.5)  # let the transcript's last records land
                    self._ship(sid, offset)
                    extra = done.get("extra") or {}
                    ok = not extra.get("isAborted")
                    self.finish(ok, extra.get("answer") or ("done" if ok else "stopped"), sid)
                    return
            time.sleep(2)
        self.finish(False, "turn timed out; the session keeps running in the Claude app", sid)

    def _ship(self, sid: str, offset: int) -> int:
        path = transcript_path(sid)
        if path is None:
            return offset
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        complete = chunk[: chunk.rfind(b"\n") + 1] if chunk else b""
        if not complete:
            return offset
        lines = [ln for ln in complete.decode(errors="replace").splitlines() if ln.strip()]
        for batch in batches(lines):
            self.client.post_transcript(self.id, batch, batch_id=uuid.uuid4().hex)
        events: list[dict] = []
        for ln in lines:
            try:
                events += events_from_record(json.loads(ln))
            except ValueError:
                continue
        if events:
            self.client.post_events(self.id, events)
        return offset + len(complete)
