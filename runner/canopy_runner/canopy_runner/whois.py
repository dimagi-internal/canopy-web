"""Who and what a claimed turn came from, as one log fragment.

The server records it (`TurnOut.initiator`, `.provenance`, `.parent_*` —
canopy-web apps/harness/provenance.py); this renders it so the runner's own log
says it too. Before this, the runner's only line was
`chat turn=<id> created emdash task=c-… (agent=hal)` — a scratch script on a
person's PAT and that person typing in the web UI produced identical lines.

Pure and defensive: a turn from an older server carries none of these keys, and
the fragment then says `-` rather than raising inside the claim path.
"""
from __future__ import annotations

import json

PROMPT_HEAD = 80


def _v(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return text if all(c not in text for c in ' "=\n\t') else json.dumps(text)


def who(turn: dict) -> str:
    """`<kind>:<email|contact|agent>` — e.g. `user:jj@dimagi.com`."""
    ini = turn.get("initiator") or {}
    kind = ini.get("kind") or "unknown"
    person = ini.get("user") or ini.get("contact") or {}
    ident = person.get("email") or ini.get("agent") or ""
    return f"{kind}:{ident}" if ident else kind


def credential(turn: dict) -> str:
    """`type:id:label` — e.g. `pat:42:scratch-script`."""
    cred = (turn.get("initiator") or {}).get("credential") or \
        (turn.get("provenance") or {}).get("credential") or {}
    if not cred:
        return ""
    parts = [str(cred.get("type") or "?")]
    if cred.get("id") is not None or cred.get("label"):
        parts += ["" if cred.get("id") is None else str(cred["id"]), str(cred.get("label") or "")]
    return ":".join(parts)


def parent(turn: dict) -> str:
    """The first parent the turn names: turn, else session, else task."""
    for key in ("parent_turn_id", "parent_session_id", "parent_task", "parent_claude_session"):
        if turn.get(key):
            return f"{key.removeprefix('parent_').removesuffix('_id')}:{turn[key]}"
    return ""


def client(turn: dict) -> str:
    return (turn.get("initiator") or {}).get("client") or \
        (turn.get("provenance") or {}).get("client") or ""


def brief(turn: dict) -> str:
    """`origin=… who=… credential=… client=… parent=…` for an existing log line."""
    return (f"origin={_v(turn.get('origin'))} who={_v(who(turn))} "
            f"credential={_v(credential(turn))} client={_v(client(turn))} "
            f"parent={_v(parent(turn))}")


def claim_line(turn: dict) -> str:
    """The one line logged when a turn is claimed."""
    head = json.dumps(str(turn.get("prompt") or "")[:PROMPT_HEAD])
    return f"CLAIM turn={turn.get('id')} {brief(turn)} prompt_head={head}"
