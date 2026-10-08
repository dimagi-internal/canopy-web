"""Huddles are DERIVED, never stored (canopy docs/superpowers/specs/2026-10-06-huddle-design.md).

A huddle is a team of agents syncing, led by one of them, in rounds. Everything it
consists of already lives here:

* one ANCHOR turn — a report-only row the leader files (`origin_ref.kind ==
  "huddle"`, carrying type/team/leader/members, and `finished_at` once filed);
* the ROUND turns the leader dispatches, tagged `origin_ref.kind == "huddle_round"`
  with the huddle id, round, member and attempt, parented on the anchor;
* each member's REPLY — a fenced ```huddle JSON block it files as its close-out
  (on the round turn itself, or a report-only row), else found in the round turn's
  retained transcript;
* the board TASKS the huddle produced, whose `source_url` is the huddle page.

Every read goes through the caller's visible-turn queryset (`harness.api.
visible_turns_qs`), so a huddle can never list a turn `/api/harness/turns/` would
not; and a cell's prompt and reply are read only when `turn_access.
can_read_turn_content` allows it for that round turn — the same line `/api/harness/
turns/{id}/messages` draws. A caller who may only see THAT a round ran gets its
status and nothing else.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from django.db.models import Exists, OuterRef, Q

from apps.agents.models import AgentTask
from apps.harness import services as hsvc
from apps.harness import turn_access
from apps.harness.models import Turn, TurnTranscript

_FENCE = re.compile(r"```huddle[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
#: A ```json or unlabelled fence — accepted when its object carries a "huddle" key.
_LOOSE_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
#: A round's members must reply within this of the earliest round-1 dispatch.
DEADLINE = dt.timedelta(minutes=90)
#: A member may close out a moment before the anchor's own row lands.
_CLOSEOUT_SLACK = dt.timedelta(minutes=5)
_HUDDLE_TAIL = re.compile(r"/huddles/([^/?#]+)/?$")


def _parse_obj(raw: str) -> tuple[dict | None, str]:
    try:
        b = json.loads(raw)
    except ValueError as e:
        return None, f"reply block is not valid JSON: {e}"
    if not isinstance(b, dict):
        return None, "reply block is not a JSON object"
    return b, ""


def _names(b: dict, huddle: str, round_no: int) -> bool:
    try:
        rnd = int(b.get("round") or 0)
    except (TypeError, ValueError):
        rnd = 0
    return str(b.get("huddle")) == huddle and rnd == round_no


def _loose_candidates(text: str) -> list[str]:
    """Where a member's reply lands when it skips the ```huddle label — newest
    (last) first: a ```json / unlabelled fence, then the whole text as one object."""
    out = list(reversed(_LOOSE_FENCE.findall(text)))
    whole = text.strip()
    if whole.startswith("{") and whole.endswith("}"):
        out.append(whole)
    return out


def extract_block(text: str, huddle: str, round_no: int) -> tuple[dict | None, str]:
    """The LAST ```huddle block in `text` that names this huddle and round.

    Members sometimes file the reply without the label — a ```json or bare
    ``` fence, or the whole close-out as a bare JSON object. Those are accepted
    too (an object with a "huddle" key, same huddle/round rule), but a labelled
    ```huddle block always wins.

    Returns (block, "") on a match, else (None, why) — `why` is "" when there was
    no block at all, so "not replied yet" and "replied wrongly" stay distinct."""
    text = text or ""
    err = ""
    for raw in reversed(_FENCE.findall(text)):
        b, perr = _parse_obj(raw)
        if b is None:
            err = err or perr
            continue
        if _names(b, huddle, round_no):
            return b, ""
        err = err or f"block names huddle {b.get('huddle')!r} round {b.get('round')!r}"
    for raw in _loose_candidates(text):
        b, _ = _parse_obj(raw)
        # Unlabelled JSON is only a reply if it says so; anything else is prose.
        if b is None or "huddle" not in b:
            continue
        if _names(b, huddle, round_no):
            return b, ""
        err = err or f"block names huddle {b.get('huddle')!r} round {b.get('round')!r}"
    return None, err


def anchors(visible_qs):
    return visible_qs.filter(origin_ref__kind="huddle")


def _ref(turn) -> dict:
    return turn.origin_ref if isinstance(turn.origin_ref, dict) else {}


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rounds_qs(visible_qs, huddle_ids):
    return visible_qs.filter(origin_ref__kind="huddle_round", origin_ref__huddle__in=list(huddle_ids))


def _outputs_qs(workspaces):
    """Board tasks pointing at a huddle page, filed by an agent in any of
    `workspaces` (the caller's own) — a huddle's members span workspaces."""
    return AgentTask.objects.select_related("agent", "project").filter(
        agent__workspace_id__in=list(workspaces), source_url__contains="/huddles/")


def _huddle_of_url(url: str) -> str:
    # The page URL ends with /huddles/<id>; match the exact tail so h1 never
    # claims h10's tasks.
    m = _HUDDLE_TAIL.search(url or "")
    return m.group(1) if m else ""


def _base(anchor, *, rounds_dispatched: int, outcome_count: int) -> dict:
    ref = _ref(anchor)
    return {
        "id": str(ref.get("huddle") or ""),
        "type": str(ref.get("type") or ""),
        "team": str(ref.get("team") or ""),
        "leader": str(ref.get("leader") or (anchor.agent.slug if anchor.agent_id else "")),
        "members": [str(m) for m in (ref.get("members") or [])],
        "anchor_turn_id": str(anchor.id),
        "created_at": anchor.created_at,
        "finished": bool(ref.get("finished_at")),
        "outcome_count": outcome_count,
        "rounds_dispatched": rounds_dispatched,
    }


def summaries(anchor_rows, visible_qs, workspaces) -> list[dict]:
    """One summary per anchor, in the order given — a fixed handful of queries
    for the whole list, not several per huddle. `workspaces` are the caller's own:
    a huddle's outcomes count tasks from any of them, not just the anchor's."""
    ids = {str(_ref(a).get("huddle") or "") for a in anchor_rows}
    top: dict[str, int] = {}
    for ref in _rounds_qs(visible_qs, ids).values_list("origin_ref", flat=True):
        ref = ref if isinstance(ref, dict) else {}
        h = str(ref.get("huddle") or "")
        top[h] = max(top.get(h, 0), _int(ref.get("round")))
    counts: dict[str, int] = {}
    if ids:
        for url in _outputs_qs(workspaces).values_list("source_url", flat=True):
            h = _huddle_of_url(url)
            if h in ids:
                counts[h] = counts.get(h, 0) + 1
    out = []
    for a in anchor_rows:
        h = str(_ref(a).get("huddle") or "")
        out.append(_base(a, rounds_dispatched=top.get(h, 0), outcome_count=counts.get(h, 0)))
    return out


def _transcript_text(turn) -> str:
    try:
        msgs, _ = hsvc.transcript_messages(turn)
    except Exception:  # a broken or aged-out blob must not 500 the page
        return ""
    return "\n".join(m.get("plaintext") or "" for m in msgs if m.get("role") == "assistant")


def _cell(turn, *, huddle, user, closeouts, memo) -> dict:
    ref = _ref(turn)
    member, rnd = str(ref.get("member") or ""), _int(ref.get("round"))
    readable = turn_access.can_read_turn_content(user, turn, memo)
    out = {"member": member, "round": rnd, "attempt": _int(ref.get("attempt"), 1),
           "turn_id": str(turn.id), "status": turn.status, "created_at": turn.created_at,
           "finished_at": turn.finished_at, "content_hidden": not readable,
           "prompt": turn.prompt if readable else "", "block": None,
           "reply_source": "none", "reply_error": "",
           "has_transcript": bool(getattr(turn, "has_transcript", False))}
    if not readable:
        return out
    # 1. The round turn's own close-out (a cloud runner's report joins its row).
    b, err = extract_block(turn.report_summary, huddle, rnd)
    if b:
        return {**out, "block": b, "reply_source": "closeout"}
    # 2. A report-only close-out the member filed (a laptop session), newest first.
    # A close-out summary is board-tier (the agents' turn list shows it to every
    # member), so the gate that matters is the round turn's, above.
    for text in closeouts.get(member, ()):
        cb, cerr = extract_block(text, huddle, rnd)
        if cb and str(cb.get("member") or member) == member:
            return {**out, "block": cb, "reply_source": "closeout"}
        err = err or cerr
    # 3. The round turn's retained transcript.
    if out["has_transcript"]:
        tb, terr = extract_block(_transcript_text(turn), huddle, rnd)
        if tb:
            return {**out, "block": tb, "reply_source": "transcript"}
        err = err or terr
    return {**out, "reply_error": err}


def detail(anchor, *, user, visible_qs, workspaces) -> dict:
    hid = str(_ref(anchor).get("huddle") or "")
    rounds = list(
        _rounds_qs(visible_qs, [hid])
        .annotate(has_transcript=Exists(TurnTranscript.objects.filter(turn=OuterRef("pk"))))
        .order_by("created_at")
    )
    latest: dict[tuple[str, int], Turn] = {}
    for t in rounds:
        ref = _ref(t)
        k = (str(ref.get("member") or ""), _int(ref.get("round")))
        if k not in latest or _int(ref.get("attempt"), 1) >= _int(_ref(latest[k]).get("attempt"), 1):
            latest[k] = t
    members = {m for m, _ in latest}
    closeouts: dict[str, list[str]] = {}
    if members:
        rows = (visible_qs.filter(agent__slug__in=members, report_summary__contains=hid,
                                  created_at__gte=anchor.created_at - _CLOSEOUT_SLACK)
                .order_by("-created_at").values_list("agent__slug", "report_summary"))
        for slug, text in rows:
            closeouts.setdefault(slug, []).append(text)
    memo: dict = {}
    cells = [_cell(t, huddle=hid, user=user, closeouts=closeouts, memo=memo)
             for t in sorted(latest.values(), key=lambda t: (_int(_ref(t).get("round")),
                                                             str(_ref(t).get("member") or "")))]
    r1 = [t.created_at for (_, r), t in latest.items() if r == 1]
    outputs = [
        {"agent": a.agent.slug, "task_id": a.id, "ext_id": a.ext_id, "title": a.title,
         "status": a.status, "assigned": a.assigned,
         "project": a.project.ext_id if a.project_id else "",
         "url": f"/w/{a.agent.workspace_id}/agents/{a.agent.slug}/tasks",
         "next_action": a.next_action, "updated_at": a.updated_at}
        for a in _outputs_qs(workspaces).filter(
            Q(source_url__endswith=f"/huddles/{hid}") | Q(source_url__endswith=f"/huddles/{hid}/"))
        .order_by("agent__slug", "id")
    ]
    base = _base(anchor, rounds_dispatched=max([r for _, r in latest] or [0]),
                 outcome_count=len(outputs))
    # The leader's digest is the anchor's content; same gate as a round's.
    readable = turn_access.can_read_turn_content(user, anchor, memo)
    # The principal's priorities brief the huddle ran on (canopy `huddle plan
    # --priorities-file` tags the anchor with it) — what "priority 2" in a
    # proposal refers to. Content, so the same gate as the digest.
    brief = str(_ref(anchor).get("priorities_brief") or "")
    return {**base, "summary": anchor.report_summary if readable else "",
            "priorities_brief": brief if readable else "",
            "deadline_at": (min(r1) + DEADLINE) if r1 else None,
            "cells": cells, "outputs": outputs}
