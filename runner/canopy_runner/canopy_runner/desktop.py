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

THE DEEP LINK ACTIVATES CLAUDE.APP in its macOS session, even with `open -g`,
NSWorkspace `activates = false`, or the app hidden: its open-url handler calls
show() + focus() for every claude:// link it handles, and no query parameter
skips that (Focus round 2, #1188). So `open_session` opens through
`desktop_quiet_open.js`, which gives focus straight back to the app the person
was in and re-hides Claude: Claude holds the front ~60ms per NEW session or wake.
That is a mitigation, not a fix: a keystroke typed in that window can land in
Claude. Follow-ups into a live session open no URL at all. Where the app's
`start_session` gate is on and a live canopy session can host it, a new session
opens with no URL either (`start_in_app`, canopy-web#1238); waking a stopped
session still takes the deep link.

THE `claude-cli` RUNTIME is this backend with the app taken out (headless.py): a new
thread gets the same worktree, settings, mod and index entry, but the runner runs
the turn itself over ACP and opens no claude:// link — so nothing ever takes the
focus. A person brings the session into the app from its resume picker when they
want it there; once the mod reports the app's process `alive`, follow-ups go in
through the mod exactly as above, and when that process is gone the runner runs
them headless again. One live process per session either way (headless.py says
why). Never a deep link: a Claude CLI session the app does not hold runs headless.

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

from canopy_transcript.result_note import cap, turn_url

from . import caller, claude_model, session_naming, transfer
from .cancel import CANCELLED_TURNS

logger = logging.getLogger("canopy_runner.desktop")

EMDASH = "emdash"
CLAUDE_DESKTOP = "claude-desktop"
CLAUDE_CLI = "claude-cli"
RUNTIMES = (EMDASH, CLAUDE_DESKTOP, CLAUDE_CLI)

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
#: The app's start_session tool, allowed by exact name when the start path is on
#: (start_session_enabled): in auto mode a call no rule allows goes to the
#: classifier, which refuses a call no model request asked for, and a wildcard
#: does not cover it. The mod denies the model's own calls, so this lets only the
#: runner's starts through.
START_RULE = "mcp__ccd_session__start_session"
SEED_PROMPT = "This session was prepared by canopy. Reply with exactly: ready"


def drop_seed(rows: list[dict]) -> list[dict]:
    """Conversational rows without the seed exchange (SEED_PROMPT and the `ready`
    reply after it). The seed is the headless turn that made the session id; it is
    in the CLI transcript, so every session view opened with it (found live after
    #1218, which kept it out of TURN transcripts only). Matches the exact prompt
    this module writes, so it can never drop a real message."""
    out, skip_reply = [], False
    for r in rows:
        text = (r.get("text") or "").strip()
        if r.get("role") == "user" and text == SEED_PROMPT:
            skip_reply = True
            continue
        if skip_reply and r.get("role") == "assistant" and text == "ready":
            skip_reply = False
            continue
        if r.get("role") == "assistant":
            skip_reply = False
        out.append(r)
    return out

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
        if engine in (CLAUDE_DESKTOP, CLAUDE_CLI):
            try:
                ensure_mod()
            except Exception:  # noqa: BLE001 — a turn will say so if the mod is missing
                logger.warning("could not register the canopy-desktop mod", exc_info=True)
        if engine == CLAUDE_CLI:
            # An npm install can take a minute; the heartbeat must not wait on it.
            from . import headless
            threading.Thread(target=headless.ensure_adapter, daemon=True,
                             name="claude-cli-adapter").start()


def in_flight() -> list[str]:
    with _lock:
        for tid in [t for t, th in IN_FLIGHT.items() if not th.is_alive()]:
            IN_FLIGHT.pop(tid, None)
        return sorted(IN_FLIGHT)


# ── paths and the session index ─────────────────────────────────────────────

#: Where this runtime keeps its worktrees and session index, unless the config
#: names `desktop_dir`. A module attribute so a test suite can point it at tmp.
DEFAULT_DIR: Path | None = None


def _root(cfg) -> Path:
    return Path(getattr(cfg, "desktop_dir", "") or DEFAULT_DIR or (Path.home() / ".canopy" / "desktop"))


def _index_path(cfg) -> Path:
    return _root(cfg) / "sessions.json"


def _index(cfg) -> dict:
    try:
        return json.loads(_index_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def _remember(cfg, sid: str, wt: Path, project: str, runtime: str = CLAUDE_DESKTOP) -> None:
    with _lock:
        data = _index(cfg)
        data[sid] = {"worktree": str(wt), "project": project, "created_at": int(time.time()),
                     "runtime": runtime}
        path = _index_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)


def worktree_for(cfg, sid: str) -> Path | None:
    entry = _index(cfg).get(sid or "")
    if not entry or entry.get("closed_at"):
        return None
    wt = Path(entry["worktree"])
    return wt if wt.exists() else None


def is_cli_session(cfg, sid: str) -> bool:
    return (_index(cfg).get(sid or "") or {}).get("runtime") == CLAUDE_CLI


def is_desktop_session(cfg, sid: str) -> bool:
    """A live desktop session of this runner's. A CLOSED one is not: nothing may
    be delivered into it, and the next turn on its thread starts a fresh one."""
    entry = _index(cfg).get(sid or "") if sid else None
    return bool(entry) and not entry.get("closed_at")


def _mark_closed(cfg, sid: str) -> None:
    with _lock:
        data = _index(cfg)
        if sid not in data:
            return
        data[sid]["closed_at"] = int(time.time())
        path = _index_path(cfg)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)


def close(cfg, sid: str) -> str:
    """Close a desktop session from the web — this runtime's half of canopy's close
    (`close.close_session` is the emdash half, which deletes the emdash task).

    Stop its running turn, retire it from the session report (the report's absence
    is how canopy-web learns it closed), and remove its worktree when that loses
    nothing. Returns "closed", or "absent" for a session already closed.

    The worktree is removed only when it has no uncommitted changes, and its branch
    is always kept: emdash's delete is a human's explicit act in emdash, this one
    is a tap on a phone, and committed-or-kept is the line that tap cannot cross.
    The session stays in the app's own sidebar — the app offers no way to archive
    it from outside — but it no longer runs, is reported, or takes turns."""
    entry = _index(cfg).get(sid)
    if not entry or entry.get("closed_at"):
        return "absent"
    wt = Path(entry.get("worktree", ""))
    if wt.exists() and channel_held_for(wt / CHANNEL, sid):
        try:
            res = stop(cfg, sid)
            logger.info("desktop close %s: stop -> %s", sid, res.get("action"))
        except Exception:  # noqa: BLE001 — a failed stop must not keep it open
            logger.warning("desktop close %s: stop failed; closing anyway", sid, exc_info=True)
    _mark_closed(cfg, sid)
    if wt.exists() and _shared_worktree(cfg, wt):
        logger.info("desktop close %s: worktree %s kept (another open session uses it)", sid, wt)
    elif wt.exists():
        # The runtime's own files are excluded from status (make_worktree), so
        # anything listed is the session's work. A status that FAILED (not a
        # checkout, a broken one) prints nothing too — it vouches for nothing, so
        # it keeps the worktree exactly like a dirty one.
        st = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=normal"],
                            capture_output=True, text=True, timeout=120)
        if st.returncode != 0 or st.stdout.strip():
            logger.info("desktop close %s: worktree %s kept (%s)", sid, wt,
                        "uncommitted changes" if st.returncode == 0 else "git status failed")
        else:
            # From the main checkout, not from inside the worktree being removed.
            common = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
            repo = Path(common).parent if common else wt
            _git(repo, "worktree", "remove", "--force", str(wt), check=False)
            logger.info("desktop close %s: worktree %s (branch kept)", sid,
                        "removed" if not wt.exists() else "could not be removed")
    return "closed"


def _shared_worktree(cfg, wt: Path) -> bool:
    """Another open session runs in `wt` (a fork and its source share one)."""
    return any(not e.get("closed_at") and Path(e.get("worktree", "")) == wt
               for e in _index(cfg).values())


class ForkError(RuntimeError):
    """A fork could not be cut on this box; the message says why."""


def fork_session(cfg, source: str, records: int, project: str) -> str:
    """Start a new Claude CLI session from the first `records` records of `source`'s
    transcript, in the same worktree, and return its id. Records are counted as
    canopy_transcript.read_records counts them, which is how canopy-web's ordinals
    count them. The copy sits beside the source (same cwd, same project directory),
    so resuming it from the worktree finds it."""
    from canopy_transcript import read_records

    wt = worktree_for(cfg, source)
    if wt is None or not is_cli_session(cfg, source):
        raise ForkError(f"session {source} is not a Claude CLI session with a worktree on this box")
    src = transcript_path(source)
    if src is None:
        raise ForkError(f"session {source} has no transcript on this box")
    kept = read_records(src)[:records]
    if len(kept) < records:
        raise ForkError(f"session {source}'s transcript has {len(kept)} records, not {records}")
    sid = str(uuid.uuid4())
    lines = [json.dumps({**r, "sessionId": sid} if "sessionId" in r else r) for r in kept]
    (src.parent / f"{sid}.jsonl").write_text("".join(f"{line}\n" for line in lines))
    _remember(cfg, sid, wt, project, runtime=CLAUDE_CLI)
    return sid


def transcript_path(sid: str, claude_home: Path | None = None) -> Path | None:
    from canopy_transcript import transcript_for_session_id

    return transcript_for_session_id(sid, claude_home=claude_home or (Path.home() / ".claude" / "projects"))


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
    """(checkout, base ref) for a project: runner.json's `project_paths` first,
    else the repo emdash registers for it — the same two sources the heartbeat
    reports (sessions.reported_projects), so a claimed turn always finds one."""
    override = project_paths(cfg).get(project)
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


def local_projects(cfg) -> list[str]:
    """The project names runner.json's `project_paths` adds — only where the
    runtime makes its own worktrees; emdash can drive only the projects it holds."""
    if _current == EMDASH:
        return []
    return sorted(n for n, path in project_paths(cfg).items() if n and path)


def project_paths(cfg) -> dict:
    """runner.json's `project_paths`, read from the file each time so that
    `canopy-runner projects add` reaches the running daemon on its next tick."""
    path = getattr(cfg, "config_path", "")
    if path:
        try:
            return dict(json.loads(Path(path).read_text()).get("project_paths") or {})
        except (OSError, ValueError, AttributeError):
            pass
    return dict(getattr(cfg, "project_paths", None) or {})


def set_project_path(config_path: str, name: str, checkout: str | None) -> dict:
    """Add (or, with None, remove) one entry of runner.json's `project_paths`,
    atomically and keeping the file's permissions. Returns the new map."""
    path = Path(config_path)
    raw = json.loads(path.read_text())
    paths = dict(raw.get("project_paths") or {})
    if checkout is None:
        paths.pop(name, None)
    else:
        paths[name] = checkout
    raw["project_paths"] = paths
    tmp = path.with_suffix(".json.projects")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)
    return paths


def needs_emdash() -> bool:
    """Whether a turn this box claims can need emdash. `claude-cli` runs Claude
    itself, so a box on it claims with emdash closed or absent (Windows, or a
    laptop that never installed it); a follow-up on a thread still held by an
    emdash session then fails with emdash's own error rather than waiting."""
    return _current != CLAUDE_CLI


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True, timeout=120).stdout.strip()


def make_worktree(cfg, repo: Path, base_ref: str, name: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:80] or uuid.uuid4().hex[:8]
    wt = _root(cfg) / "worktrees" / repo.name / slug
    branches = _git(repo, "branch", "--list", f"canopy-desktop/{slug}", check=False)
    if wt.exists() or branches:
        # The same name twice (same prompt, same thread key) must not crash the
        # turn on `worktree add`: a new session gets its own place (found in #1188).
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"
        wt = wt.with_name(slug)
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


def worktree_settings(allow: list[str], existing: dict | None = None, *, model: str = "") -> dict:
    out = dict(existing or {})
    if model:
        # The agent's model for this session (canopy-web's `cli_model`): the
        # worktree's local settings are read by the CLI and the desktop app alike.
        out["model"] = model
    perms = dict(out.get("permissions") or {})
    perms["allow"] = sorted(set(perms.get("allow") or []) | set(allow))
    out["permissions"] = perms
    plugins = dict(out.get("enabledPlugins") or {})
    plugins[PLUGIN] = True
    out["enabledPlugins"] = plugins
    return out


# ── the Claude Code CLI ─────────────────────────────────────────────────────
#
# The runner runs under launchd, whose PATH is the bare system one — it does NOT
# have ~/.local/bin, where the native installer puts `claude`. emdash never cared
# (it spawns Claude Code itself); this runtime shells out to the CLI to seed a
# session and to install the mod, so it resolves the binary explicitly instead of
# trusting PATH. Found the hard way: the first live turn on haldimagi crashed with
# FileNotFoundError: 'claude' (canopy-web#1188), though every test from a shell
# had passed — a shell's PATH has ~/.local/bin.

def claude_app_dir(home: Path | None = None) -> Path:
    """The Claude app's own userData folder on this box (macOS or Windows)."""
    from .platform_jobs import app_data_dir

    return app_data_dir(home or Path.home()) / "Claude"


def _cli_candidates() -> list[Path]:
    home = Path.home()
    found = [shutil.which("claude")]
    found += [str(home / ".local" / "bin" / "claude"), str(home / ".local" / "bin" / "claude.exe"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    # The CLI the desktop app bundles for its own sessions — always present where
    # the app is, so the runtime works even on a box that never installed the CLI.
    bundled = glob.glob(str(claude_app_dir(home) / "claude-code"
                            / "*" / "claude.app" / "Contents" / "MacOS" / "claude"))
    # Windows: the same versioned folder under %APPDATA%\Claude, holding claude.exe.
    bundled += glob.glob(str(claude_app_dir(home) / "claude-code" / "*" / "claude.exe"))
    found += sorted(bundled, key=lambda p: Path(p).stat().st_mtime, reverse=True)
    return [Path(p) for p in found if p]


def claude_cli() -> Path | None:
    """The Claude Code CLI this runtime runs, or None when there is none."""
    for p in _cli_candidates():
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def _cli() -> Path:
    cli = claude_cli()
    if cli is None:
        raise RuntimeError("no Claude Code CLI on this runner (looked on PATH, in ~/.local/bin, "
                           "Homebrew, and the Claude app's bundled copy)")
    return cli


def _cli_env(cli: Path) -> dict:
    """The CLI's own subprocesses (hooks, git, gh, uv) need a real PATH too."""
    home = Path.home()
    extra = [str(cli.parent), str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return {**os.environ, "PATH": os.pathsep.join(extra + [path])}


def prepare_settings(cfg, wt: Path, *, model: str = "") -> None:
    cli = _cli()
    # `install` alone reuses whatever version is already in Claude Code's plugin
    # cache; `update` is what brings in the mod a runner upgrade shipped.
    for verb in ("install", "update"):
        subprocess.run([str(cli), "plugin", verb, PLUGIN, "--scope", "local"], env=_cli_env(cli),
                       cwd=wt, capture_output=True, text=True, check=False, timeout=120)
    path = wt / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text()) if path.exists() else {}
    allow = list(getattr(cfg, "desktop_allow", None) or DEFAULT_ALLOW)
    if start_session_enabled():
        allow.append(START_RULE)
    path.write_text(json.dumps(worktree_settings(allow, existing, model=model), indent=2) + "\n")


def ensure_mod() -> None:
    """Register the mod's marketplace from a FIXED copy under ~/.canopy/desktop, so a
    runner upgrade (a new install path) never leaves Claude Code pointing at a
    deleted directory; `marketplace update` refreshes the plugin cache."""
    dest = Path.home() / ".canopy" / "desktop" / "marketplace"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(MOD_SRC, dest)
    cli = _cli()
    env = _cli_env(cli)
    listed = subprocess.run([str(cli), "plugin", "marketplace", "list"], env=env,
                            capture_output=True, text=True, timeout=60).stdout
    if MARKETPLACE not in listed:
        subprocess.run([str(cli), "plugin", "marketplace", "add", str(dest)], env=env,
                       capture_output=True, text=True, timeout=120)
    subprocess.run([str(cli), "plugin", "marketplace", "update", MARKETPLACE], env=env,
                   capture_output=True, text=True, timeout=120)


def seed_session(cfg, wt: Path, name: str = "", *, model: str = "") -> str:
    cli = _cli()
    argv = [str(cli), "-p", SEED_PROMPT, "--output-format", "json"]
    if name:
        # The session's title in the app's sidebar. `--name` writes a
        # `custom-title` record the app adopts on import; without it every session
        # was titled from the seed prompt alone, and all came out as "General
        # coding session". Same name emdash shows: c-/cx- + subject + disc.
        argv += ["--name", name]
    # The agent's model for this turn, else the box's own default for its sessions.
    model = model or getattr(cfg, "desktop_model", "")
    if model:
        argv += ["--model", model]
    out = subprocess.run(argv, cwd=wt, env=_cli_env(cli), capture_output=True, text=True, timeout=300)
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
    """Is the app up (starting it if not)? On Windows there is no `pgrep` or
    `open -a`, and no need for them: every path that needs the app's process
    (a new session, a wake) goes through `open_session`, and the claude://
    protocol handler starts the app when it is not running. A follow-up into a
    live session (`alive` fresh) already proves the app is up."""
    from .platform_jobs import is_windows

    if is_windows():
        return True
    if app_running():
        return True
    subprocess.run(["open", "-g", "-j", "-a", "Claude"], check=False)
    for _ in range(30):
        time.sleep(1)
        if app_running():
            return True
    return False


# ── the app's server-side feature gates (canopy-web#1238) ───────────────────
#
# Two of the app's GrowthBook features decide how this backend should work, and
# neither is a local setting:
#   - `start_session` (the in-app `mcp__ccd_session__start_session` tool) exists
#     only when gate 2371478310 ("sideSessions") is on. It is the real fix for the
#     deep-link focus steal: a live dispatcher session could start new sessions
#     with no claude:// URL at all. Off, there is no such tool.
#   - the unattended idle timeout (900s) arms only when 4041267332 is non-zero.
#     At 0 a hidden session is never paused, so wakes (deep links) are rare.
# The app caches the features it last fetched for this account in
# `<userData>/fcache`: an 8-byte magic header, then gzipped JSON
# `{timestamp, mode, orgUuid, features: {<id>: {value, on, source, ...}}}`.
# Reading it is what turns "recheck #1238" from unpacking the app bundle and
# probing from a mod into one `canopy-runner runtime`.

FCACHE_MAGIC = bytes([67, 76, 70, 2, 0, 154, 183, 226])
GATE_START_SESSION = "2371478310"
GATE_IDLE_TIMEOUT = "4041267332"


def fcache_path(home: Path | None = None) -> Path:
    return claude_app_dir(home) / "fcache"


def app_features(path: Path | None = None) -> dict | None:
    """What the Claude app last fetched for this account, or None if unreadable.

    {"fetched_at": epoch seconds, "start_session": bool | None,
     "idle_timeout": int | None}. None for a feature the cache does not name."""
    import gzip

    try:
        raw = (path or fcache_path()).read_bytes()
        if not raw.startswith(FCACHE_MAGIC):
            return None
        data = json.loads(gzip.decompress(raw[len(FCACHE_MAGIC):]))
        features = data.get("features") or {}
    except (OSError, ValueError, EOFError, AttributeError):
        return None

    def value(gate):
        f = features.get(gate)
        return f.get("value") if isinstance(f, dict) else None

    start = value(GATE_START_SESSION)
    idle = value(GATE_IDLE_TIMEOUT)
    return {
        "fetched_at": (data.get("timestamp") or 0) / 1000,
        "start_session": bool(start) if start is not None else None,
        "idle_timeout": idle if isinstance(idle, (int, float)) and not isinstance(idle, bool) else None,
    }


def describe_app_features(features: dict | None) -> str:
    """One line for `canopy-runner runtime`: the two gates #1238 watches."""
    if features is None:
        return "Claude desktop gates: unknown (no readable feature cache on this account)"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(features["fetched_at"]))
    start = {True: ("ON, used (CANOPY_DESKTOP_START_SESSION=1)" if start_session_enabled() else
                     "ON, unused: the app refuses automated starts (#1238); new sessions open by deep link"),
             False: "off (new sessions open by deep link)",
             None: "not in cache"}[features["start_session"]]
    idle = features["idle_timeout"]
    idle_txt = "not in cache" if idle is None else ("off" if idle == 0 else f"{idle}s")
    return f"Claude desktop gates (fetched {when}): start_session {start}; idle timeout {idle_txt}"


# ── starting a session with no deep link (canopy-web#1238) ──────────────────
#
# OFF unless CANOPY_DESKTOP_START_SESSION=1. The idea: with the app's
# `start_session` gate on, the runner asks the mod in a live session it already
# holds (the host) to call the app's start_session tool, which opens a sibling
# session in the folder the runner prepared, in the background, with no claude://
# link. The folder's `seeded` reads "start" until the new session's mod claims it.
#
# Tried live on jj (2026-10-21, Claude 2.42304): the app refuses it by design —
# "Start sessions only on the user's own words to you here; this turn runs an
# automated event, another session's message, or the task another session gave
# you." Only `initiation: "own_initiative"` is allowed, which shows a card for a
# person to click: not unattended. Each refused try cost a new session ~9 s before
# the deep link. The deep link took focus for ~0.25 s and the runner handed it
# back. Kept, off, for the day the app allows it; any step failing still falls
# back to the deep link.

def start_session_enabled() -> bool:
    return os.environ.get("CANOPY_DESKTOP_START_SESSION") == "1"


#: How long the host's mod may take to answer a start request.
START_REQUEST_SECONDS = 60
#: How long the started session may take to claim its folder.
START_CLAIM_SECONDS = 60
#: How long the started session's opening turn (SEED_PROMPT) may run.
START_SEED_SECONDS = 120
START_BACKGROUND = ("Prepared by the canopy runner, which delivers this session's real task "
                    "through its canopy-desktop mod as soon as this opening turn completes.")


def start_host(cfg) -> Path | None:
    """The channel of a live desktop session whose mod can start sessions, or None."""
    for entry in reversed(list(_index(cfg).values())):
        if entry.get("closed_at") or entry.get("runtime", CLAUDE_DESKTOP) != CLAUDE_DESKTOP:
            continue
        channel = Path(entry["worktree"]) / CHANNEL
        try:
            if channel_alive(channel) and "start" in (channel / "caps").read_text():
                return channel
        except OSError:
            continue
    return None


def _wait_for(check, seconds: float, step: float = 0.5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        got = check()
        if got:
            return got
        time.sleep(step)
    return None


def start_in_app(cfg, wt: Path, title: str, features: dict | None = None) -> str | None:
    """Open a new session in `wt` with start_session through a host's mod. The new
    session's id once it has claimed the folder and run its opening turn, or None
    to fall back to the deep link (the folder's `seeded` cleared)."""
    if not start_session_enabled():
        return None
    if not (features if features is not None else app_features() or {}).get("start_session"):
        return None
    host = start_host(cfg)
    if host is None:
        return None
    channel = wt / CHANNEL
    (channel / "seeded").write_text("start")
    n = next_followup_index(host, "start", ".json")
    (host / f"start-{n}.json").write_text(json.dumps({
        "cwd": str(wt), "title": title, "prompt": SEED_PROMPT, "background": START_BACKGROUND}))

    def answered():
        try:
            done = json.loads((host / f"start-{n}.done").read_text())
        except (OSError, ValueError):
            return None
        return None if done.get("pending") else done

    done = _wait_for(answered, START_REQUEST_SECONDS)
    if not done or done.get("isError"):
        logger.warning("desktop start in %s: start_session did not start it (%s)", wt,
                       (done or {}).get("text", "no answer from the host's mod")[:300])
        (channel / "seeded").write_text("")
        return None

    def claimed():
        sid = (channel / "seeded").read_text().strip()
        return sid if sid and sid != "start" else None

    sid = _wait_for(claimed, START_CLAIM_SECONDS)
    if not sid:
        logger.warning("desktop start in %s: started, but no session claimed the folder", wt)
        (channel / "seeded").write_text("")
        return None
    if not _wait_for(lambda: any(e.get("kind") == "turn.complete" for e in read_events(channel)),
                     START_SEED_SECONDS):
        logger.warning("desktop start %s: its opening turn did not complete in time", sid)
    logger.info("desktop start %s: opened with start_session, no deep link", sid)
    return sid


#: Opens a claude:// URL and hands focus straight back (see the file's header).
QUIET_OPEN = Path(__file__).resolve().parent / "desktop_quiet_open.js"
#: How long the helper watches for the app to come forward after the open.
QUIET_OPEN_WATCH_SECONDS = 3


def open_session(sid: str) -> None:
    """Hand a session to the app — and give focus back to whoever had it.

    The app raises itself for every claude:// link (its open-url handler calls
    show() + focus(); `open -g` and NSWorkspace `activates = false` are both
    overridden — canopy-web#1188, Focus round 2), so on an account a person is
    using this would steal their focus. The helper undoes it within ~60ms. Plain
    `open -g` stays as the fallback: a session that opens with a flicker beats one
    that never opens.

    Windows has neither `osascript` nor `open`: the URL goes to the shell's
    protocol handler (`os.startfile`), which is what the app registers claude://
    with there. No focus hand-back on Windows yet, so a new session raises the app."""
    url = f"claude://resume?session={sid}"
    from .platform_jobs import is_windows

    if is_windows():
        try:
            os.startfile(url)  # type: ignore[attr-defined]  # Windows-only
            logger.info("desktop open %s: handed to the claude:// handler", sid)
        except OSError as exc:
            logger.warning("desktop open %s: no claude:// handler (%s) — is the Claude app "
                           "installed on this account?", sid, exc)
        return
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", str(QUIET_OPEN), url,
                              str(QUIET_OPEN_WATCH_SECONDS)],
                             capture_output=True, text=True, timeout=QUIET_OPEN_WATCH_SECONDS + 15)
        if out.returncode == 0:
            logger.info("desktop open %s: %s", sid, out.stdout.strip())
            return
        logger.warning("desktop open %s: quiet open failed (%s); plain open instead",
                       sid, (out.stderr or out.stdout).strip()[-300:])
    except subprocess.TimeoutExpired:
        # The URL went out before the watch began; opening it again would only flicker.
        logger.warning("desktop open %s: quiet open timed out after opening", sid)
        return
    except OSError as exc:
        logger.warning("desktop open %s: quiet open failed (%s); plain open instead", sid, exc)
    subprocess.run(["open", "-g", url], check=False)


def channel_alive(channel: Path) -> bool:
    try:
        return time.time() - int((channel / "alive").read_text()) / 1000 < ALIVE_SECONDS
    except (OSError, ValueError):
        return False


def channel_held_for(channel: Path, sid: str) -> bool:
    """The app's process holds THIS session's channel. A worktree can hold more than
    one session (a fork shares its source's), and the channel is the one seeded
    for — so another session's live process is not this one's."""
    try:
        return channel_alive(channel) and (channel / "seeded").read_text().strip() == sid
    except OSError:
        return False


def read_events(channel: Path) -> list[dict]:
    evs = []
    for p in channel.glob("ev-*.json"):
        try:
            e = json.loads(p.read_text())
            # `ev-<t>-<seq>-<kind>`: the mod's own counter orders two events of one
            # millisecond (a card drawn and answered at once), which `t` cannot.
            seq = int(p.name.split("-")[2])
        except (OSError, ValueError, IndexError):
            continue
        evs.append((e.get("t", 0), seq, e))
    return [e for _, _, e in sorted(evs, key=lambda x: x[:2])]


def next_followup_index(channel: Path, prefix: str = "fu", suffix: str = ".txt") -> int:
    n = 1
    while (channel / f"{prefix}-{n}{suffix}").exists():
        n += 1
    return n


#: How long a stop waits for the mod's verdict before calling it unreadable.
STOP_WAIT_SECONDS = 12


def stop(cfg, session_key: str, wait: float = STOP_WAIT_SECONDS) -> dict:
    """Stop the running model turn in a desktop session. Same contract as
    `cdp_control.interrupt` so the callers need no second vocabulary:
    `{"action": "interrupted" | "idle" | "unreadable"}`.

    The mod does the stopping (`$.turn.abort` on the turn it saw start) and says
    what happened in a `stopped` event. A session whose process is down has
    nothing running, which is `idle` — the honest answer, not a failure.
    A Claude CLI session's turn running headless is cancelled over ACP instead."""
    from . import headless

    if headless.cancel(session_key):
        return {"action": "interrupted", "reason": "", "killed": 0}
    wt = worktree_for(cfg, session_key)
    if wt is None:
        return {"action": "unreadable", "reason": "no worktree for this desktop session"}
    channel = wt / CHANNEL
    if not channel_held_for(channel, session_key):
        return {"action": "idle", "reason": "the session's process is not running"}
    # Kill the running turn's shells FIRST, then abort. The other order leaves the
    # command backgrounded by the abort, and killing a BACKGROUND task makes Claude
    # Code notify the model, which starts a new turn and runs the work again (found
    # live, #1188). Killed while still a foreground tool call, the call just fails.
    killed = 0
    turn_started = _running_turn_started(channel)
    if turn_started is not None:
        killed = kill_turn_shells(session_key, turn_started / 1000)
    n = next_followup_index(channel, "stop")
    which = f"stop-{n}"
    (channel / f"{which}.txt").write_text(str(int(time.time() * 1000)))
    deadline = time.time() + wait
    while time.time() < deadline:
        hit = next((e for e in read_events(channel) if e.get("kind") == "stopped"
                    and (e.get("extra") or {}).get("which") == which), None)
        if hit:
            outcome = (hit.get("extra") or {}).get("outcome")
            return {"action": outcome if outcome in ("interrupted", "idle") else "unreadable",
                    "reason": (hit.get("extra") or {}).get("err", ""), "killed": killed}
        time.sleep(0.5)
    return {"action": "unreadable", "reason": "the session did not answer the stop", "killed": killed}


def _running_turn_started(channel: Path) -> int | None:
    """When the model turn running now began (ms), or None when none is running."""
    started = None
    for e in read_events(channel):
        if e.get("kind") == "turn.start":
            started = e.get("t", 0)
        elif e.get("kind") == "turn.complete":
            started = None
    return started


# ── the shells a stopped turn leaves behind ─────────────────────────────────
#
# Aborting a turn does NOT kill the Bash command it was running: Claude Code
# backgrounds it, and it runs on after the stop (a 120s sleep outlived a stop,
# found live in #1188). Its own TaskStop tool, called by the mod with the
# backgrounded task's id, reported success and left the process running — so the
# runner ends it directly. Claude Code runs every Bash command as
# `/bin/zsh -c source ~/.claude/shell-snapshots/…`, a descendant of the session's
# CLI process (`--resume=<id>`). Only those shells that started during the stopped
# turn are killed: a background task the session started earlier on purpose, and
# the session's MCP servers, are left alone.

_SHELL_MARK = "/.claude/shell-snapshots/"


def _etime_seconds(etime: str) -> int:
    """ps `etime` ([[dd-]hh:]mm:ss) in seconds."""
    days, _, rest = etime.strip().rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return (int(days) if days else 0) * 86400 + h * 3600 + m * 60 + s


def _processes() -> list[tuple[int, int, float, str]]:
    """(pid, ppid, started_at, command) for this user's processes. Empty on
    Windows: only a Claude desktop session's stop reads them, a runtime written
    for macOS (`claude-cli` stops over ACP instead)."""
    from .platform_jobs import is_windows

    if is_windows():
        return []
    out = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,etime=,command="],
                         capture_output=True, text=True).stdout
    now, rows = time.time(), []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            try:
                rows.append((int(parts[0]), int(parts[1]), now - _etime_seconds(parts[2]), parts[3]))
            except ValueError:
                continue
    return rows


def turn_shells(sid: str, since: float, procs=None) -> list[int]:
    """PIDs of the Bash shells (and everything under them) that the session `sid`
    started at or after `since` (epoch seconds)."""
    procs = _processes() if procs is None else procs
    children: dict[int, list[int]] = {}
    for pid, ppid, _, _ in procs:
        children.setdefault(ppid, []).append(pid)
    info = {pid: (start, cmd) for pid, _, start, cmd in procs}
    roots = [pid for pid, _, _, cmd in procs
             if f"--resume={sid}" in cmd or f"--resume {sid}" in cmd]

    def below(pid):
        for c in children.get(pid, []):
            yield c
            yield from below(c)

    victims: list[int] = []
    for root in roots:
        for pid in children.get(root, []):
            start, cmd = info[pid]
            if _SHELL_MARK in cmd and start >= since - 2:  # 2s: etime is whole seconds
                victims += [pid, *below(pid)]
    return victims


def kill_turn_shells(sid: str, since: float) -> int:
    import signal

    from .platform_jobs import is_windows

    if is_windows():
        # No process table is read on Windows (`_processes`), and there is no
        # SIGKILL: the mod's abort still stops the turn, its shells run on.
        return 0
    victims = turn_shells(sid, since)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in victims:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if sig == signal.SIGTERM and victims:
            time.sleep(1.5)
    if victims:
        logger.info("desktop session %s: killed %d process(es) left running by the stopped turn",
                    sid, len(victims))
    return len(victims)


# ── the session report ──────────────────────────────────────────────────────

def _agent_status(channel: Path, sid: str = "") -> str:
    """emdash's vocabulary, from the mod's events: `working` while a model turn
    runs, `awaiting-input` while a permission card it raised in that turn is
    unanswered, "" when idle or the session's process is down. A Claude CLI
    session with a turn running headless is `working`."""
    if not (channel_held_for(channel, sid) if sid else channel_alive(channel)):
        from . import headless

        return "working" if sid and headless.is_running(sid) else ""
    started = asked = 0
    for e in read_events(channel):
        kind, t = e.get("kind"), e.get("t", 0)
        if kind == "turn.start":
            started, asked = t, 0
        elif kind == "turn.complete":
            started = 0
        elif kind == "ask" and started:
            asked += 1
        elif kind == "answered" and asked:
            asked -= 1
    if not started:
        return ""
    return "awaiting-input" if asked else "working"


def open_sessions(cfg) -> list[dict]:
    """Desktop sessions for the runner's session report, in the same row shape as
    `emdash.list_open_sessions`. Without this the report only ever named emdash
    tasks, so canopy-web read every desktop session as stale and listed it as
    ARCHIVED within minutes of creating it (found live, #1188). A session whose
    worktree is gone is left out — the report's absence is how canopy-web learns
    a session closed."""
    import datetime as dt

    rows = []
    for sid, entry in _index(cfg).items():
        wt = Path(entry.get("worktree", ""))
        if entry.get("closed_at") or not wt.exists():
            continue
        path = transcript_path(sid)
        try:
            last = dt.datetime.fromtimestamp((path or wt).stat().st_mtime, tz=dt.timezone.utc)
        except OSError:
            continue
        rows.append({"emdash_task": sid, "project": entry.get("project", ""),
                     "status": "in_progress", "agent_status": _agent_status(wt / CHANNEL, sid),
                     "last_interacted_at": last.isoformat()})
    return sorted(rows, key=lambda r: r["last_interacted_at"], reverse=True)


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
    fork = plan.get("fork") if not key else None
    if fork:
        return _start_fork(cfg, client, runner_id, turn, thread_key, plan, fork)
    if key and (_index(cfg).get(key) or {}).get("closed_at"):
        # The thread's session was closed from the web: start a new DESKTOP
        # session, not hand an id emdash never had to the emdash backend.
        key = ""
    if key and is_desktop_session(cfg, key):
        run = TurnRun(cfg, client, runner_id, turn, thread_key, plan, reuse=key)
    elif key or _current not in (CLAUDE_DESKTOP, CLAUDE_CLI):
        return None  # an emdash thread continues in emdash
    elif _current == CLAUDE_CLI and _adapter_missing():
        logger.error("claude-cli runtime: the ACP adapter is not installed on this runner — turn %s "
                     "runs on emdash instead", turn.get("id"))
        return None
    elif claude_cli() is None:
        # A new desktop session needs the CLI (seed + mod install). Without it the
        # turn would only crash, so it runs on emdash instead — loudly: readiness
        # reports the same reason on every heartbeat (readiness.compute).
        logger.error("claude-desktop runtime: no Claude Code CLI on this runner — turn %s "
                     "runs on emdash instead", turn.get("id"))
        return None
    else:
        run = TurnRun(cfg, client, runner_id, turn, thread_key, plan, reuse="")
    th = threading.Thread(target=run.run, daemon=True, name=f"desktop-{str(turn['id'])[:8]}")
    with _lock:
        IN_FLIGHT[str(turn["id"])] = th
    th.start()
    return f"desktop:{'reuse' if key else 'create'}:{turn['id']}"


def _start_fork(cfg, client, runner_id: str, turn: dict, thread_key: str, plan: dict,
                fork: dict) -> str:
    """A forked chat's first turn: cut its transcript, point its thread at the new
    session, and run the turn there as a resume. A fork that cannot be cut fails the
    turn, saying why — never a fresh session that has lost the conversation."""
    target = turn.get("agent_slug") or turn.get("project") or ""
    try:
        sid = fork_session(cfg, fork["session_key"], int(fork["records"]), target)
    except ForkError as exc:
        client.fail_turn(str(turn["id"]), f"Could not fork: {exc}")
        return f"desktop:fork-failed:{turn['id']}"
    client.record_session(
        runner_id, turn.get("agent_slug") or "", thread_key,
        project=turn.get("project") or "", workspace=turn.get("workspace_slug") or "",
        emdash_task_id=sid, session_id=sid, summary=None)
    run = TurnRun(cfg, client, runner_id, turn, thread_key, plan, reuse=sid)
    th = threading.Thread(target=run.run, daemon=True, name=f"desktop-{str(turn['id'])[:8]}")
    with _lock:
        IN_FLIGHT[str(turn["id"])] = th
    th.start()
    return f"desktop:fork:{turn['id']}"


def _adapter_missing() -> bool:
    from . import headless

    return headless.adapter_argv() is None


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
        # Capped, and a cut note says so and links the whole reply (#1955).
        note = cap(note or "", turn_url(getattr(self.cfg, "base_url", ""), self.turn))
        if ok:
            self.client.finish(self.id, note, status="done", emdash_task_id=sid)
        elif sid:
            # Failed AFTER reaching a session: say which, so the server never
            # re-runs it blind (same rule as the emdash path).
            self.client.finish(self.id, note, status="failed", emdash_task_id=sid)
        else:
            self.client.fail_turn(self.id, note)
        logger.info("desktop turn=%s %s: %s", self.id, "done" if ok else "failed", note[:160])

    def _prompt(self) -> str:
        prompt = self.turn.get("prompt") or (f"/{self.target}:turn" if not self.chat else "")
        # The person's attachments, downloaded and named in the prompt — the same
        # step the emdash path takes (execute.execute_chat_turn). This runtime
        # skipped it, so a screenshot sent from the web never reached a desktop
        # session: the agent got the words and nothing else, and could not tell.
        # Imported here: execute imports this module lazily, and vice versa.
        from .execute import fetch_attachments, prompt_with_attachments
        prompt = prompt_with_attachments(prompt, *fetch_attachments(self.client, self.rid, self.turn))
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
                # Never silent: a crash whose fail did not land leaves the turn
                # CLAIMED until its lease runs out, and the log is the only trace.
                logger.exception("desktop turn=%s: could not report the failure", self.id)

    def _run(self) -> None:
        if (is_cli_session(self.cfg, self.reuse) if self.reuse else _current == CLAUDE_CLI):
            self._run_cli()
            return
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
            prepare_settings(self.cfg, wt, model=claude_model.for_turn(self.turn))
            transfer.restore_into(self.cfg, self.client, self.turn, wt)
            channel = wt / CHANNEL
            channel.mkdir(exist_ok=True)
            summary = self.plan.get("summary") or ""
            if summary:
                prompt = (f"[Continuing prior work on this thread — context from earlier sessions "
                          f"(a fresh session, possibly a different machine):]\n{summary}\n\n{prompt}")
            started = start_in_app(self.cfg, wt, name)
            sid = started or seed_session(self.cfg, wt, name,
                                          model=claude_model.for_turn(self.turn))
            # The turn's record starts AFTER the seed's "Reply with exactly: ready"
            # exchange — shipping it made a turn's transcript the seed and nothing
            # else (found live, #1188).
            seeded = transcript_path(sid)
            offset = seeded.stat().st_size if seeded else 0
            (channel / "task.txt").write_text(prompt)
            if not started:
                (channel / "seeded").write_text(sid)
            which = "task"
            _remember(self.cfg, sid, wt, self.target)
            self.client.start(self.id, sid)
            self.client.record_session(
                self.rid, self.turn.get("agent_slug") or "", self.thread_key,
                project=self.turn.get("project") or "",
                workspace=self.turn.get("workspace_slug") or "",
                emdash_task_id=sid, session_id=sid, summary=None,
                # The task name, not the session uuid canopy-web would show otherwise.
                title=name)
            if not started:
                open_session(sid)
            self.status("created_session", runtime=CLAUDE_DESKTOP, session=sid, worktree=str(wt),
                        opened="start_session" if started else "deep_link")
        self._follow(sid, channel, which, offset)

    def _run_cli(self) -> None:
        """A `claude-cli` turn: into the app's process through the mod when the app
        holds the session, else headless on this box. Never a deep link."""
        prompt = self._prompt()
        if self.reuse:
            sid = self.reuse
            wt = worktree_for(self.cfg, sid)
            if wt is None:
                self.client.start(self.id, sid)
                self.finish(False, f"session {sid} has no worktree on this box any more", sid)
                return
            channel = wt / CHANNEL
            path = transcript_path(sid)
            offset = path.stat().st_size if path else 0
            if channel_held_for(channel, sid):
                # The person opened it in the app: its process owns the session.
                n = next_followup_index(channel)
                (channel / f"fu-{n}.txt").write_text(prompt)
                self.client.start(self.id, sid)
                self.status("reused_session", runtime=CLAUDE_CLI, session=sid, held_by="claude-desktop")
                self._follow(sid, channel, f"fu-{n}", offset, wait_for_completion=True)
                return
            self.client.start(self.id, sid)
            self.status("reused_session", runtime=CLAUDE_CLI, session=sid, held_by="runner")

            def resumed(new: str) -> None:
                if new != sid:
                    # The session could not load; the turn runs in a fresh one,
                    # and the thread follows it there.
                    self._adopt(new, wt, self.target)

            self._headless(prompt, wt, resume=sid, offset=offset, on_session=resumed)
            return
        repo, base = project_repo(self.cfg, self.target)
        name = session_naming.build_task_name(self.target, self.turn)
        wt = make_worktree(self.cfg, repo, base, name)
        prepare_settings(self.cfg, wt, model=claude_model.for_turn(self.turn))
        # A transfer's work, restored before the agent sees its first prompt.
        transfer.restore_into(self.cfg, self.client, self.turn, wt)
        (wt / CHANNEL).mkdir(exist_ok=True)
        summary = self.plan.get("summary") or ""
        if summary:
            prompt = (f"[Continuing prior work on this thread — context from earlier sessions "
                      f"(a fresh session, possibly a different machine):]\n{summary}\n\n{prompt}")

        def created(sid: str) -> None:
            self.client.start(self.id, sid)
            self._adopt(sid, wt, name)
            self.status("created_session", runtime=CLAUDE_CLI, session=sid, worktree=str(wt))

        self._headless(prompt, wt, resume=None, offset=0, on_session=created)

    def _adopt(self, sid: str, wt: Path, title: str) -> None:
        """Make `sid` this thread's Claude CLI session, before its prompt goes in: the
        mod claims the channel by this id once the app holds the session, and
        canopy-web routes the thread's next turn here."""
        (wt / CHANNEL / "seeded").write_text(sid)
        _remember(self.cfg, sid, wt, self.target, runtime=CLAUDE_CLI)
        self.client.record_session(
            self.rid, self.turn.get("agent_slug") or "", self.thread_key,
            project=self.turn.get("project") or "",
            workspace=self.turn.get("workspace_slug") or "",
            emdash_task_id=sid, session_id=sid, summary=None, title=title)

    def _headless(self, prompt: str, wt: Path, *, resume: str | None, offset: int,
                  on_session=None) -> None:
        from . import headless

        def emit(events: list[dict]) -> None:
            self.client.post_events(self.id, events)

        cli = claude_cli()
        env = _cli_env(cli) if cli else None
        if env is not None and (model := claude_model.for_turn(self.turn)):
            # Read before any settings file, so a resumed session gets it too.
            env = {**env, "ANTHROPIC_MODEL": model}
        try:
            ok, text, sid = headless.run_turn(
                prompt, cwd=wt, emit=emit, resume=resume, on_session=on_session,
                env=env,
                cancelled=lambda: self.id in CANCELLED_TURNS, timeout=CHAT_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — reported as the turn's failure
            sid = getattr(exc, "session_id", "") or resume or ""
            ok, text = False, f"headless turn failed: {exc}"
        if sid:
            self._ship(sid, offset, events=False)
        if text == "cancelled":
            CANCELLED_TURNS.discard(self.id)
            self.client.finish(self.id, "cancelled by user", status="cancelled", emdash_task_id=sid)
            return
        self.finish(ok, text, sid)

    def _follow(self, sid: str, channel: Path, which: str, offset: int,
                wait_for_completion: bool = False) -> None:
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
                    if not self.chat and not wait_for_completion:
                        # Agent/project turn: delivered is done, as on emdash — the
                        # work continues in the visible session.
                        self.finish(True, f"delivered to Claude desktop session {sid}", sid)
                        return
                elif time.time() > submit_by:
                    self.finish(False, "the Claude desktop session never picked up the prompt "
                                "(is Claude.app signed in, and the canopy-desktop mod installed?)", sid)
                    return
            offset = self._ship(sid, offset)
            if self.id in CANCELLED_TURNS:
                # The person hit stop on this chat turn: stop the session's turn,
                # then close canopy's out as cancelled (the chat_pump contract).
                res = stop(self.cfg, sid)
                CANCELLED_TURNS.discard(self.id)
                self._ship(sid, offset)
                note = ("cancelled by user" if res["action"] == "interrupted" else
                        "cancelled by user (the agent had already stopped)" if res["action"] == "idle"
                        else f"cancelled by user; the stop was not confirmed ({res.get('reason')})")
                self.client.finish(self.id, note, status="cancelled", emdash_task_id=sid)
                return
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

    def _ship(self, sid: str, offset: int, events: bool = True) -> int:
        """Post the transcript from `offset` on; with `events`, also as turn events
        (a headless turn streamed its own already)."""
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
        if not events:
            return offset + len(complete)
        found: list[dict] = []
        for ln in lines:
            try:
                found += events_from_record(json.loads(ln))
            except ValueError:
                continue
        if found:
            self.client.post_events(self.id, found)
        return offset + len(complete)
