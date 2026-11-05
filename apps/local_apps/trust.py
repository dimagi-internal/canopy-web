"""The trust surface: the parts of a manifest a person agreed to by installing.

A new version that changes none of it reaches them at their next Start, since
Canopy reviewed the code and they couldn't judge it anyway. One that changes any
of it waits until they've read the differences and accepted. Same idea as
`workspace_apps.template_diff`: review is a list of plain findings, or a statement
that there are none.
"""

from __future__ import annotations

import hashlib
import json

_FIELDS = ("notice", "disclosure", "canopy", "source", "entry", "platforms")
_OPS = {
    "sessions.create": "open a new Canopy session with your agent",
    "sessions.send": "send text to that session",
    "turns.status": "check whether your agent has finished",
    "meetings.save": "save a meeting's transcript to your private Meetings page",
}


def surface(manifest: dict) -> dict:
    return {name: manifest.get(name) for name in _FIELDS}


def digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(surface(manifest), sort_keys=True).encode()).hexdigest()


def _list_changes(old: list, new: list, added: str, removed: str) -> list[str]:
    return [f"{added}: {item}" for item in new if item not in old] + [
        f"{removed}: {item}" for item in old if item not in new
    ]


def changes(old: dict | None, new: dict) -> list[str]:
    """What changed in the trust surface, in words. Empty means nothing did."""
    if old is None:
        return []
    out: list[str] = []
    if old.get("notice") != new.get("notice"):
        out.append(f"The notice now says: {new.get('notice') or '(nothing)'}")
    before, after = old.get("disclosure") or {}, new.get("disclosure") or {}
    out += _list_changes(before.get("reads") or [], after.get("reads") or [], "Now reads", "No longer reads")
    out += _list_changes(before.get("sends") or [], after.get("sends") or [], "Now sends", "No longer sends")
    out += _list_changes(
        [_OPS.get(o, o) for o in old.get("canopy") or []],
        [_OPS.get(o, o) for o in new.get("canopy") or []],
        "Canopy will now",
        "Canopy will no longer",
    )
    if old.get("entry") != new.get("entry"):
        out.append(f"Runs a different program: {' '.join(new.get('entry') or [])}")
    if old.get("source") != new.get("source"):
        out.append(f"Listens through a different program: {' '.join(new.get('source') or []) or '(none)'}")
    out += _list_changes(old.get("platforms") or [], new.get("platforms") or [], "Now runs on", "No longer runs on")
    return out
