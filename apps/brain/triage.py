"""Triage: which of a run's due sessions are most worth its read budget. A cheap verdict before the expensive read.

A run may read only a few sessions (`budget`), and newest-first spends them on whatever moved last. Triage scores
each due session from counts the server already holds, in what the learner's own view of it would serve and past
where its read would start (`reading.resume_after`: only the rows that are new count for a session due again), and
serves and names the due sessions best first, each with a one-line reason. It orders, it never filters: every due
session stays due, is counted, and `brain_read` reads any of them by id. It runs only over `reading.candidates`, so
eligibility, consent, the window and the scope are exactly theirs; it never widens what a learner reads.

The signals (`SIGNALS`), counted in one query for every session that needs a verdict:
- `corrections`: rows a person typed that push back ("no, ...", "that's wrong", "still failing", "I said ...");
- `rejected`: tool calls a person stopped ("interrupted by user", "doesn't want to proceed");
- `denied`: tool calls a permission or rail refused ("permission ... denied", "blocked by");
- `tool_errors`: other failed tool calls;
- `failed_turns`: turns of its rows that ended failed, lost or cancelled;
- `rows`: how many rows are new; `typed`: how many a person typed (none: an agent-run session);
- `people`: the people in it (`SourceParticipant`).
Tool rows are counted only when the learner's view serves them (`full`); a narrowed view is scored on the rows it
serves. A judge (`opening`) is not triaged: it reads a few opening lines of each, newest first.

The score is a weighted sum with caps (`WEIGHTS`), so one runaway loop of errors cannot bury a session with a person
correcting the agent. Borrowed from gbrain's dream cycle (`dream_verdicts`): judge cheaply, cache the verdict keyed by
what it judged, and spend the expensive pass on the best. Here the judge is deterministic, not a model: no new call,
no new key, and the same session always gets the same verdict. A verdict holds counts, never words, and is kept per
(source, view, start, last row, `VERSION`) in `BrainTriage`: asking again costs one lookup, and a session that grew is
a new verdict."""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, IntegerField, Q, Value

from apps.canopy_sessions import transcript_read
from apps.canopy_sessions.models import Message
from apps.harness.models import Turn

from .models import BrainTriage, SourceParticipant

#: Bump when what a signal counts or how the score is made changes: older verdicts are then not used.
VERSION = 1
#: How many due sessions (newest active first) one ask triages; past them the rest follow newest first.
MAX = 200
#: Each signal's weight and the count past which it adds nothing more.
WEIGHTS = {"corrections": (3, 5), "rejected": (3, 3), "denied": (2, 5), "failed_turns": (2, 3),
           "tool_errors": (1, 10)}
#: A point per this many new rows (at most `GROWTH_CAP`), a point per person past the first (at most `PEOPLE_CAP`),
#: and a point for a session a person drove.
GROWTH_ROWS, GROWTH_CAP, PEOPLE_CAP = 50, 4, 3
SIGNALS = ("corrections", "rejected", "denied", "tool_errors", "failed_turns", "rows", "typed", "people")

#: How a person's pushback opens, and what it says anywhere in the row (case-insensitive, prefix or substring:
#: plain SQL, the same on Postgres and sqlite).
_PUSHBACK_OPENS = ("no,", "no.", "no!", "no ", "nope", "stop", "wait,", "wait ", "wrong", "that's not", "that is not",
                   "not what", "why did you", "why are you", "you didn't", "you did not", "undo", "revert", "ugh")
_PUSHBACK_SAYS = ("that's wrong", "is wrong", "still failing", "still broken", "still not", "doesn't work",
                  "didn't work", "does not work", "not working", "try again", "i said", "i told you", "wtf",
                  "frustrat")
_REJECTED_SAYS = ("interrupted by user", "doesn't want to proceed", "doesn't want to take this action")
_DENIED_SAYS = ("blocked by", "not permitted")
_FAILED_TURNS = (Turn.FAILED, Turn.LOST, Turn.CANCELLED)


@dataclass(frozen=True)
class Verdict:
    source: int
    view: str
    after: int
    upto: int
    score: int
    signals: dict
    reason: str


def score(signals: dict) -> int:
    """The score of a verdict's signals (`WEIGHTS`, capped)."""
    total = sum(w * min(int(signals.get(k, 0)), cap) for k, (w, cap) in WEIGHTS.items())
    total += min(int(signals.get("rows", 0)) // GROWTH_ROWS, GROWTH_CAP)
    total += min(max(int(signals.get("people", 0)) - 1, 0), PEOPLE_CAP)
    return total + (1 if signals.get("typed") else 0)


def reason(signals: dict) -> str:
    """One line, counts only: what made the score."""
    def n(k, one, many=None):
        v = int(signals.get(k, 0))
        return f"{v} {one if v == 1 else (many or one + 's')}" if v else ""
    parts = [n("corrections", "correction"), n("rejected", "rejected tool call"), n("denied", "denied tool call"),
             n("tool_errors", "tool error"), n("failed_turns", "failed turn")]
    parts = [p for p in parts if p] or ["no friction"]
    parts.append(n("rows", "new row") or "nothing new")
    if int(signals.get("people", 0)) > 1:
        parts.append(f"{signals['people']} people")
    parts.append("person-driven" if signals.get("typed") else "agent-run")
    return ", ".join(parts)[:200]


def _any(field: str, words, how: str = "icontains") -> Q:
    q = Q()
    for word in words:
        q |= Q(**{f"{field}__{how}": word})
    return q


def _filters(view: str) -> dict:
    """Each signal's row filter in `view` (None: the view does not serve it, so it is 0)."""
    typed = Q(role=Message.USER, typed=True)
    pushback = typed & (_any("plaintext", _PUSHBACK_OPENS, "istartswith") | Q(plaintext__iexact="no")
                        | _any("plaintext", _PUSHBACK_SAYS))
    if view == transcript_read.FULL:
        # A stopped or refused call comes back as a failed result: only those are searched for words.
        failed = Q(role=Message.TOOL_RESULT, content__is_error=True)
        rejected = failed & _any("plaintext", _REJECTED_SAYS)
        denied = failed & ~rejected & ((Q(plaintext__icontains="permission") & Q(plaintext__icontains="denied"))
                                       | _any("plaintext", _DENIED_SAYS))
        errors = failed & ~rejected & ~denied
        return {"rows": Q(), "typed": typed, "corrections": pushback, "rejected": rejected, "denied": denied,
                "tool_errors": errors}
    if view == transcript_read.CONVERSATION:
        spoken = Q(role__in=(Message.USER, Message.ASSISTANT)) & ~Q(plaintext="")
        return {"rows": spoken, "typed": typed, "corrections": pushback, "rejected": None, "denied": None,
                "tool_errors": None}
    return {"rows": typed, "typed": typed, "corrections": pushback, "rejected": None, "denied": None,
            "tool_errors": None}


def judge(due, view: str) -> list[Verdict]:
    """The verdicts of the due sessions `due` (`reading.candidates`, annotated as it annotates; the `MAX` most
    recently active of them) in the learner's `view`, best first; ties newest first. A constant number of queries
    however many. `reading.verdicts` is what asks, and leaves out a judge."""
    qs = due
    rows = list(qs.values("pk", "session_id", "_read", "_redo", "_through", "_rows")[:MAX])
    if not rows:
        return []
    want = {}
    for i, r in enumerate(rows):
        start = r["_through"] if r["_read"] and not r["_redo"] and r["_through"] is not None else -1
        want[r["pk"]] = (i, r["session_id"], start, r["_rows"] if r["_rows"] is not None else -1)
    have = {(t.source_id, t.after, t.upto): t for t in BrainTriage.objects.filter(
        source_id__in=list(want), view=view, version=VERSION, upto__in={u for *_, u in want.values()})}
    out, missing = {}, {}
    for pk, (_i, session_id, after, upto) in want.items():
        t = have.get((pk, after, upto))
        if t is not None:
            out[pk] = Verdict(pk, view, after, upto, t.score, t.signals, t.reason)
        else:
            missing[pk] = (session_id, after, upto)
    if missing:
        made = _judge(view, missing)
        BrainTriage.objects.bulk_create([BrainTriage(source_id=v.source, view=view, after=v.after, upto=v.upto,
                                                     version=VERSION, score=v.score, signals=v.signals,
                                                     reason=v.reason) for v in made.values()],
                                        ignore_conflicts=True)
        out.update(made)
    return sorted(out.values(), key=lambda v: (-v.score, want[v.source][0]))


def _judge(view: str, missing: dict) -> dict[int, Verdict]:
    """Count the signals of every session in `missing` ({source: (session, after, upto)}) in one query, and the people
    in them in one more."""
    by_session = {session_id: (pk, after) for pk, (session_id, after, _u) in missing.items()}
    span = Q()
    for session_id, (_pk, after) in by_session.items():
        span |= Q(session_id=session_id, turn_index__gt=after)
    filters = _filters(view)
    # Named `n_<signal>`: a bare name like `typed` would be the column, and its filter would count itself.
    counts = {f"n_{k}": (Value(0, output_field=IntegerField()) if f is None else Count("pk", filter=f) if f
                         else Count("pk")) for k, f in filters.items()}
    counts["n_failed_turns"] = Count("turn", distinct=True,
                                     filter=filters["rows"] & Q(turn__status__in=_FAILED_TURNS))
    got = {r["session_id"]: r for r in Message.objects.filter(span).order_by().values("session_id").annotate(**counts)}
    people = dict(SourceParticipant.objects.filter(source_id__in=list(missing)).order_by().values("source_id")
                  .annotate(n=Count("pk")).values_list("source_id", "n"))
    out = {}
    for pk, (session_id, after, upto) in missing.items():
        r = got.get(session_id) or {}
        signals = {k: int(r.get(f"n_{k}") or 0) for k in SIGNALS if k != "people"}
        signals["people"] = int(people.get(pk, 0))
        out[pk] = Verdict(pk, view, after, upto, score(signals), signals, reason(signals))
    return out
