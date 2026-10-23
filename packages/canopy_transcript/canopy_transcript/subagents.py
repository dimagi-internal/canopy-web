"""What a session's subagents are doing, from the files Claude Code keeps beside it.

A subagent (the `Agent` tool) never writes to the session's own transcript. Each
one gets `<transcript-stem>/subagents/agent-<id>.jsonl` for its turns and an
`agent-<id>.meta.json` naming the parent's tool call that started it
(`toolUseId`), its `description`, `agentType`, `requestShape` ("background" for
one that runs on after its tool call returns) and, for one spawned by another
subagent, `parentAgentId`.

So the parent transcript shows one `Agent` row and, for a background agent, a
result that comes back at once — a green tick on work that runs on for another
twenty minutes. `summarize_subagents` turns the side files into one small dict
per subagent, which the chat hangs on that row.

Both runners call it; stdlib only, like the rest of the package.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .records import read_records
from .rows import hands_back_to_human

SUBAGENT_DIRNAME = "subagents"

#: The most recently started subagents kept per session. A long session can
#: spawn hundreds; the chat only needs the ones near what is on screen, and the
#: report is re-sent whenever one of them changes.
MAX_SUBAGENTS = 50

#: Longest `description` and `last` (the latest tool call, summarised) shipped.
TEXT_MAX = 120


def subagent_dir(transcript: Path) -> Path:
    return Path(transcript).with_suffix("") / SUBAGENT_DIRNAME


def summarize_subagents(transcript: Path | None, cache: dict | None = None) -> list[dict]:
    """One summary per subagent of this session, oldest first. Never raises.

    `cache` (any dict the caller keeps between calls) maps a subagent file to
    its size and summary, so only a file that grew is read again — a transcript
    only ever grows by appending, so an unchanged size is an unchanged file.
    [] when the session has no subagents, which is the common case.
    """
    if transcript is None:
        return []
    try:
        entries = list(os.scandir(subagent_dir(transcript)))
    except OSError:
        return []
    out: list[dict] = []
    for entry in entries:
        if not (entry.name.startswith("agent-") and entry.name.endswith(".jsonl")):
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        held = cache.get(entry.path) if cache is not None else None
        if held is not None and held[0] == size:
            summary = held[1]
        else:
            summary = summarize_one(Path(entry.path))
            if cache is not None:
                cache[entry.path] = (size, summary)
        if summary is not None:
            out.append(summary)
    out.sort(key=lambda s: (s.get("started_at") or "", s["agent_id"]))
    return out[-MAX_SUBAGENTS:]


def summarize_one(path: Path) -> dict | None:
    """The summary of one subagent file, or None when it is not one."""
    agent_id = path.stem.removeprefix("agent-")
    meta = _meta(path.with_suffix(".meta.json"))
    steps = 0
    last = ""
    ended = False
    started_at = updated_at = None
    for rec in read_records(path):
        if not isinstance(rec, dict):
            continue
        stamp = rec.get("timestamp")
        if isinstance(stamp, str):
            started_at = started_at or stamp
            updated_at = stamp
        kind = rec.get("type")
        content = (rec.get("message") or {}).get("content") if isinstance(
            rec.get("message"), dict) else None
        if kind == "assistant":
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    steps += 1
                    last = tool_summary(block)
            ended = hands_back_to_human(rec)
        elif kind == "user":
            # A tool result or a new prompt (SendMessage resumes a finished
            # agent) means the agent has the floor again.
            ended = False
    if started_at is None and not meta:
        return None
    return {
        "agent_id": agent_id,
        "tool_use_id": str(meta.get("toolUseId") or ""),
        "parent_agent_id": str(meta.get("parentAgentId") or ""),
        "description": _cap(meta.get("description")),
        "agent_type": str(meta.get("agentType") or ""),
        "background": meta.get("requestShape") == "background",
        "state": "done" if ended else "running",
        "steps": steps,
        "last": last,
        "started_at": started_at,
        "updated_at": updated_at,
    }


def tool_summary(block: dict) -> str:
    """`Name · what it was pointed at`, for the one line under a subagent's row.

    The same picks as the chat's own tool rows (canopy-ui `toolPreview`), so a
    subagent's latest step reads the way the step would in the main chat.
    """
    name = str(block.get("name") or "tool")
    raw = block.get("input")
    args = raw if isinstance(raw, dict) else {}
    detail = ""
    if name == "Bash":
        detail = str(args.get("command") or "").strip().split("\n")[0]
    elif name in ("Read", "Write", "Edit"):
        detail = str(args.get("file_path") or "")
    elif name in ("Grep", "Glob"):
        detail = str(args.get("pattern") or "")
    elif name == "Skill":
        detail = str(args.get("skill") or "")
    elif name == "Agent":
        detail = str(args.get("description") or args.get("subagent_type") or "")
    if name.startswith("mcp__"):
        name = name.split("__")[-1] or name
    return _cap(f"{name} · {detail}" if detail else name)


def _meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _cap(value) -> str:
    text = str(value or "").strip()
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"
