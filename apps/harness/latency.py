"""Reply latency: how long a person waits for an agent's answer, hop by hop.

One trace per turn, kept on the turn (`Turn.latency`, a flat ``{mark: iso time}``
plus two facts, ``warm`` and ``runner_kind``), so a single turn can be read back
(`trace`) and many can be summed up (`aggregate`). Each mark is written once, by
whoever sees that moment first-hand, and never moved afterwards (`mark`):

    site_received      the host site stored the person's message (the site says, on send)
    enqueued           canopy stored the turn                     (Turn.created_at)
    claimed            a runner took it                            (Turn.claimed_at)
    started            the runner had resolved its session         (Turn.started_at)
    session_ready      the runner found (warm) or made (cold) the session   (runner)
    prompt_sent        the words were typed into the session                (runner)
    prompt_delivered   the session's transcript shows them                  (runner)
    first_text         the agent's first words reached canopy  (first assistant event)
    reply_ready        the transcript shows the agent handed the floor back (runner)
    finished           canopy finished the turn                    (Turn.finished_at)
    reply_posted       canopy began POSTing the reply to the site's webhook
    site_acknowledged  the site answered that POST with a 2xx

The spans between them (`SPANS`) are what `aggregate` reports, p50/p95 per
runner kind and warm/cold. `BUDGET` is the promise for a warm contact turn;
`alerts/reply_latency.py` raises an alert when a workspace's recent turns break it.

Nothing here decides anything about a turn: a missing mark is simply a span not
measured (an older runner, a site that does not say when it received the
message), never an error.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Iterable

from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from .models import Turn, TurnEvent

MARKS = (
    "site_received", "enqueued", "claimed", "started", "session_ready", "prompt_sent",
    "prompt_delivered", "first_text", "reply_ready", "finished", "reply_posted",
    "site_acknowledged",
)
#: The marks a runner may report (on finish). The rest are canopy's or the site's.
RUNNER_MARKS = frozenset({"session_ready", "prompt_sent", "prompt_delivered", "first_text",
                          "reply_ready"})

#: (name, from, to): what each hop is, in the order a message travels.
SPANS = (
    ("site_to_canopy", "site_received", "enqueued"),     # the site's own handling, then the POST
    ("claim_wait", "enqueued", "claimed"),               # queued until a runner took it
    ("prepare", "claimed", "prompt_delivered"),          # session found/made, words typed and seen
    ("prepare_sent", "claimed", "prompt_sent"),          # the same, for a runner that sees no delivery
    ("model_answer", "prompt_sent", "reply_ready"),      # prompt in to answer out, the same way
    ("model_first_text", "prompt_delivered", "first_text"),
    ("answer", "prompt_delivered", "reply_ready"),       # the model's whole answer
    ("reply_to_finish", "reply_ready", "finished"),      # runner noticed, finished the turn
    ("finish_to_site", "finished", "site_acknowledged"),  # canopy pushed it, the site took it
    ("total", "site_received", "site_acknowledged"),
    ("canopy_total", "enqueued", "finished"),            # for turns with no site marks
)

#: The promise for a contact turn on a warm session: p95 of `total` (or, when the
#: site does not report, `canopy_total`) under this many seconds.
BUDGET_P95_SECONDS = 15.0
#: Too few turns say nothing about a p95.
BUDGET_MIN_SAMPLE = 5

#: A site's own clock is trusted this far, and no further: a `site_received` in
#: the future, or older than this, is not a receipt time but a wrong clock.
SITE_CLOCK_WINDOW = dt.timedelta(hours=6)


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, dt.datetime) else str(value)


def _parse(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def site_received_at(value, now: dt.datetime | None = None) -> dt.datetime | None:
    """A site's claim of when it received the message, or None when it is not
    believable (unparseable, in the future, or older than `SITE_CLOCK_WINDOW`)."""
    when = _parse(value)
    now = now or timezone.now()
    if when is None or when > now + dt.timedelta(seconds=5) or now - when > SITE_CLOCK_WINDOW:
        return None
    return when


def mark(turn_or_id, facts: dict | None = None, **marks) -> None:
    """Record marks (and facts) on a turn. A mark already there wins: each moment
    is written by whoever saw it first, and a retried report cannot move it."""
    clean = {k: _iso(v) for k, v in marks.items() if k in MARKS and v}
    extra = {k: v for k, v in (facts or {}).items() if k in ("warm", "runner_kind") and v is not None}
    if not clean and not extra:
        return
    pk = getattr(turn_or_id, "pk", turn_or_id)
    with transaction.atomic():
        row = Turn.objects.select_for_update().filter(pk=pk).only("pk", "latency").first()
        if row is None:
            return
        current = dict(row.latency or {})
        merged = {**clean, **extra, **current}
        if merged != current:
            Turn.objects.filter(pk=pk).update(latency=merged)
    if hasattr(turn_or_id, "latency"):
        turn_or_id.latency = {**clean, **extra, **(turn_or_id.latency or {})}


def runner_marks(raw) -> tuple[dict, dict]:
    """What a runner reported on finish, split into (marks, facts) and cleaned:
    only the marks a runner may report, only times that parse."""
    if not isinstance(raw, dict):
        return {}, {}
    marks = {k: _parse(v) for k, v in raw.items() if k in RUNNER_MARKS}
    facts = {"warm": raw["warm"]} if isinstance(raw.get("warm"), bool) else {}
    return {k: v for k, v in marks.items() if v is not None}, facts


def _first_assistant_at(turn) -> dt.datetime | None:
    return (TurnEvent.objects.filter(turn_id=turn.pk, kind="assistant")
            .order_by("seq").values_list("ts", flat=True).first())


def _times(turn, *, first_text: dt.datetime | None = None) -> dict[str, dt.datetime]:
    """Every mark this turn has, as datetimes: the stored ones, then canopy's own
    columns where nothing was stored."""
    out = {k: v for k, v in ((k, _parse(v)) for k, v in (turn.latency or {}).items() if k in MARKS) if v}
    for name, value in (("enqueued", turn.created_at), ("claimed", turn.claimed_at),
                        ("started", turn.started_at), ("finished", turn.finished_at),
                        ("first_text", first_text)):
        if value and name not in out:
            out[name] = value
    return out


def _runner_kind(turn) -> str:
    runner = getattr(turn, "claimed_by", None)
    return (turn.latency or {}).get("runner_kind") or getattr(runner, "kind", "") or "unknown"


def _spans(times: dict[str, dt.datetime]) -> dict[str, float]:
    return {name: round((times[b] - times[a]).total_seconds(), 3)
            for name, a, b in SPANS if a in times and b in times}


def trace(turn, *, first_text: dt.datetime | None = None) -> dict:
    """One turn's trace: its marks in travel order, the spans between them, and
    whether its session was warm. Reads the first assistant event when the
    caller did not pass it."""
    if first_text is None and "first_text" not in (turn.latency or {}):
        first_text = _first_assistant_at(turn)
    times = _times(turn, first_text=first_text)
    warm = (turn.latency or {}).get("warm")
    return {
        "turn_id": str(turn.pk),
        "origin": turn.origin,
        "runner_kind": _runner_kind(turn),
        "warm": warm if isinstance(warm, bool) else None,
        "marks": [{"mark": m, "at": times[m].isoformat()} for m in MARKS if m in times],
        "spans": _spans(times),
    }


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (no interpolation: a p95 is a turn that happened)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _group_key(t: dict) -> str:
    warmth = {True: "warm", False: "cold"}.get(t["warm"], "unknown")
    return f"{t['runner_kind']}/{warmth}"


def aggregate(traces: Iterable[dict]) -> dict:
    """p50/p95/max of every span, overall and per `<runner kind>/<warm|cold>`."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in traces:
        groups["all"].append(t)
        groups[_group_key(t)].append(t)
    out = {}
    for key, rows in sorted(groups.items()):
        spans = {}
        for name, _, _ in SPANS:
            values = [r["spans"][name] for r in rows if name in r["spans"]]
            if values:
                spans[name] = {"n": len(values), "p50": percentile(values, 50),
                               "p95": percentile(values, 95), "max": max(values)}
        out[key] = {"turns": len(rows), "spans": spans}
    return out


def end_to_end(t: dict) -> float | None:
    """The wait the budget is about: site to site when the site reports, else
    canopy's own enqueue to finish."""
    return t["spans"].get("total", t["spans"].get("canopy_total"))


def contact_turns(*, since: dt.datetime, base=None, limit: int = 2000) -> list:
    """Finished site-message turns (a person writing in over a host site) since
    `since`, newest first, from `base` (a caller's visible turns) or all of them."""
    qs = Turn.objects.all() if base is None else base
    qs = (qs.filter(origin=Turn.ORIGIN_SITE_MESSAGE, finished_at__gte=since, status=Turn.DONE)
          .select_related("claimed_by", "chat_session__agent").order_by("-finished_at"))
    return list(qs[:limit])


def traces_for(turns) -> list[dict]:
    """Traces for many turns, with their first assistant events read in one query."""
    firsts = dict(TurnEvent.objects.filter(turn_id__in=[t.pk for t in turns], kind="assistant")
                  .values("turn_id").annotate(at=Min("ts")).values_list("turn_id", "at"))
    return [trace(t, first_text=firsts.get(t.pk)) for t in turns]


def budget_breaches(now: dt.datetime | None = None, window: dt.timedelta = dt.timedelta(hours=24)) -> list[dict]:
    """Per workspace: warm contact turns in `window` whose p95 wait is over budget."""
    now = now or timezone.now()
    by_ws: dict[str, list[dict]] = defaultdict(list)
    turns = contact_turns(since=now - window)
    for t, tr in zip(turns, traces_for(turns), strict=True):
        if tr["warm"] is False:
            continue  # a cold start is measured, and budgeted separately once we know it
        wait = end_to_end(tr)
        if wait is not None and t.tenant_slug:
            by_ws[t.tenant_slug].append({"wait": wait, "turn_id": tr["turn_id"]})
    out = []
    for ws, rows in sorted(by_ws.items()):
        waits = [r["wait"] for r in rows]
        p95 = percentile(waits, 95)
        if len(waits) >= BUDGET_MIN_SAMPLE and p95 is not None and p95 > BUDGET_P95_SECONDS:
            slowest = max(rows, key=lambda r: r["wait"])
            out.append({"workspace": ws, "n": len(waits), "p95": p95, "p50": percentile(waits, 50),
                        "slowest_turn": slowest["turn_id"], "slowest": slowest["wait"]})
    return out
