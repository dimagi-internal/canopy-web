"""What the Claude Code sessions on this box spent — per session, per UTC day.

Both runners run this over `~/.claude/projects/*/*.jsonl` and post the result to
canopy-web (`POST /runners/{id}/usage`). It reads only usage and a little identity
— the session's working directory, its repo, when it ran — never shipping content.
canopy-web keeps the usage of the sessions it already has and drops the rest. Scoped to the repos canopy-web names for the box
(`GET /runners/{id}/usage-scope`: its declared projects and the agents it serves),
so nothing else on the machine is read past its first records.

Incremental by file: `scan` takes and returns `seen` ({path: [mtime, size]}), and
a file that did not change is not read again. A report for a session carries its
WHOLE history (every day), so the server simply replaces what it had.
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import parse_emdash_worktree
from .rows import record_usage

#: How far into a transcript to look for its working directory.
_CWD_PROBE_LINES = 50


def repo_for_cwd(cwd: str, *, home: Path, repos: set[str]) -> str:
    """The canopy-known repo a session's working directory belongs to, or "".

    An emdash worktree names its repo (`parse_emdash_worktree`); anything else
    (an agent's checkout `~/emdash-projects/<slug>`, a cloud box's agent root
    `<root>/<slug>`, a plain clone) is matched on its path segments, innermost
    first — so `…/canopy-web/packages/x` is `canopy-web`."""
    if not cwd:
        return ""
    parsed = parse_emdash_worktree(cwd, home=home)
    if parsed and parsed[0] in repos:
        return parsed[0]
    for part in reversed(Path(cwd).parts):
        if part in repos:
            return part
    return ""


def _cwd(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _CWD_PROBE_LINES:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
                    return rec["cwd"]
    except OSError:
        pass
    return ""


def session_usage(path: Path, repo: str, cwd: str) -> dict | None:
    """One transcript's report, or None when it spent nothing."""
    by_message: dict[str, dict] = {}
    first_at, last_at = "", ""
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            at = str(rec.get("timestamp") or "")
            if at:
                first_at = min(first_at, at) if first_at else at
                last_at = max(last_at, at)
            u = record_usage(rec)
            if u:
                by_message[u["message_id"] or f"#{len(by_message)}"] = u
    if not by_message:
        return None
    days: dict[str, dict] = {}
    for u in by_message.values():
        day = (u.get("at") or last_at or "")[:10]
        if not day:
            continue
        d = days.setdefault(day, {"day": day, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                                  "cache_creation_tokens": 0, "messages": 0, "_models": {}})
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            d[k] += int(u.get(k) or 0)
        d["messages"] += 1
        if u.get("model"):
            d["_models"][u["model"]] = d["_models"].get(u["model"], 0) + int(u.get("output_tokens") or 0)
    for d in days.values():
        models = d.pop("_models")
        d["model"] = max(models, key=models.get) if models else ""
    return {
        "claude_session": path.stem, "repo": repo, "cwd": cwd,
        "first_at": first_at, "last_at": last_at, "days": sorted(days.values(), key=lambda d: d["day"]),
    }


def scan(claude_home: Path, *, home: Path, repos: set[str],
         seen: dict | None = None) -> tuple[list[dict], dict]:
    """Reports for every changed transcript in a known repo, and the new `seen`."""
    seen = dict(seen or {})
    out: list[dict] = []
    if not claude_home.is_dir() or not repos:
        return out, seen
    for path in sorted(claude_home.glob("*/*.jsonl")):
        try:
            st = path.stat()
        except OSError:
            continue
        stamp = [st.st_mtime, st.st_size]
        key = str(path)
        if seen.get(key) == stamp:
            continue
        seen[key] = stamp
        cwd = _cwd(path)
        repo = repo_for_cwd(cwd, home=home, repos=repos)
        if not repo:
            continue
        report = session_usage(path, repo, cwd)
        if report:
            out.append(report)
    return out, seen


#: Sessions per POST; a report body stays far below the server's limits.
REPORT_CHUNK = 100


def send_report(post, *, home: Path, repos: list[str]) -> int:
    """Scan and send: THE report both runners make. `post(sessions)` sends one
    chunk. What was sent is remembered in
    `~/.canopy/usage-seen.json`, written only after every chunk was taken, so a
    failed post is simply sent again. Returns how many sessions were reported."""
    seen_path = home / ".canopy" / "usage-seen.json"
    try:
        seen = json.loads(seen_path.read_text())
    except (OSError, ValueError):
        seen = {}
    sessions, seen = scan(home / ".claude" / "projects", home=home, repos=set(repos), seen=seen)
    for i in range(0, len(sessions), REPORT_CHUNK):
        post(sessions[i:i + REPORT_CHUNK])
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(seen))
    return len(sessions)
