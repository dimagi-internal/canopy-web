"""Who and what a claimed turn came from, in one log-line shape.

A scratch script posted chat messages to an agent and the only trace on the
runner was `chat turn=<id> created emdash task=c-… (agent=hal)` — nothing about
who asked, with which credential, from which program, or inside which session.
canopy-web now sends all of it on the claimed turn (`initiator` carries the
credential, client and parent; `provenance`, `parent_*` beside it); these
helpers render it so every runner line about a turn can say it.
"""
from __future__ import annotations

import json

PROMPT_HEAD = 80


def who(turn: dict) -> str:
    ini = turn.get("initiator") or {}
    kind = ini.get("kind") or "unknown"
    person = ini.get("user") or ini.get("contact") or {}
    name = person.get("email") or ini.get("agent") or ""
    return f"{kind}:{name}" if name else kind


def credential(turn: dict) -> str:
    cred = (turn.get("initiator") or {}).get("credential") \
        or (turn.get("provenance") or {}).get("credential") or {}
    if not cred.get("type"):
        return "-"
    cid = cred.get("id")
    return f"{cred['type']}:{'' if cid is None else cid}:{cred.get('label') or ''}"


def client(turn: dict) -> str:
    return ((turn.get("initiator") or {}).get("client")
            or (turn.get("provenance") or {}).get("client") or "-")


def parent(turn: dict) -> str:
    """turn:<id>/session:<id>/task:<name> — whichever are known, else '-'."""
    p = (turn.get("initiator") or {}).get("parent") or {}
    parts = []
    tid = turn.get("parent_turn_id") or p.get("turn_id")
    sid = turn.get("parent_session_id") or p.get("session_id")
    task = turn.get("parent_task") or p.get("task")
    if tid:
        parts.append(f"turn:{tid}")
    if sid:
        parts.append(f"session:{sid}")
    if task:
        parts.append(f"task:{task}")
    return "/".join(parts) or "-"


def _q(text: str) -> str:
    return json.dumps(text or "", ensure_ascii=False)


def summary(turn: dict) -> str:
    """`origin=… who=… parent=…` — appended to execute's created/reused/done lines."""
    return f"origin={turn.get('origin') or '-'} who={who(turn)} parent={parent(turn)}"


def claim_line(turn: dict) -> str:
    """The CLAIM line: one per turn this runner takes."""
    target = turn.get("agent_slug") or turn.get("project") or turn.get("target") or "-"
    return (f"CLAIM turn={turn.get('id')} target={target} origin={turn.get('origin') or '-'} "
            f"who={who(turn)} credential={_q(credential(turn))} client={_q(client(turn))} "
            f"parent={parent(turn)} prompt_head={_q((turn.get('prompt') or '')[:PROMPT_HEAD])}")
