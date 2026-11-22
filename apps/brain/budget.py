"""A target's budget: how much one run may read and write, and how much a target may spend in a UTC day.

Set on the target (`BrainTarget.max_*`, by its workspace's owners and admins on the brain page) and shown there with
today's use. A run stops cleanly at a cap, never in the middle of a source or an output:

* **sessions per run**: `brain_read` serves no source past it and answers `done` with `budget` saying why
  (`reading.read_next`);
* **outputs per run** and **per day**: `brain_emit` refuses the next one with the reason (`emit.emit`); a repeat that
  folds into an existing finding (`findings`) writes no new output and costs nothing;
* **runs per day**, **outputs per day** and **tokens per day**: a fire or a Run now starts nothing once one is spent,
  and leaves a `brain.target` event saying which (`targets.why_not`).

A target also says how far back its runs read and how long a finding stays fresh (`lookback_days`, `fade_days`, set by
the same people on the same form): a run reads only sessions active in the last `lookback_days` (`reading.candidates`),
newest first, and an open finding nobody re-saw for `fade_days` is faded (`findings.faded`). Null takes the default.

Tokens are what the boxes reported for the runs' conversations (`events.SessionUsage`), so they lag the run and are
checked before a run starts, never during one: canopy-web cannot stop a model mid-turn. A run is one turn by
construction (its first turn's end finishes it, `signals._run_ends_with_its_turn`), so "turns per run" is one.
A run with no target (started by hand) has the defaults."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from django.db.models import F, Sum
from django.utils import timezone

from .models import BrainOutput, BrainRun, BrainRunRead, BrainTarget

#: A target's budget unless its managers set another; what `BrainTarget`'s columns default to.
DEFAULTS = {"max_sources_per_run": 50, "max_outputs_per_run": 20, "max_runs_per_day": 4, "max_outputs_per_day": 50,
            "max_tokens_per_day": None}
FIELDS = tuple(DEFAULTS)
#: How far back a target's runs read, in days of a session's last activity, by kind of target. A workspace moves fast
#: and its learner reads every day, so two weeks is the current state of things; a project target reads only the
#: sessions tied to one piece of work, far fewer a day, over that work's longer arc, so a month.
LOOKBACK_DAYS = {"workspace": 14, "project": 30}
#: How long an open finding stays fresh without a run seeing it again: half again the workspace window, so a finding
#: none of three weeks of daily runs re-saw is one reality has probably moved past.
FADE_DAYS = 21
#: The target's settings beside its budget, on the same form and route; null takes the default.
WINDOW_FIELDS = ("lookback_days", "fade_days")


@dataclass(frozen=True)
class Limits:
    max_sources_per_run: int
    max_outputs_per_run: int
    max_runs_per_day: int
    max_outputs_per_day: int
    max_tokens_per_day: int | None


def limits(target: BrainTarget | None) -> Limits:
    if target is None:
        return Limits(**DEFAULTS)
    return Limits(**{f: getattr(target, f) for f in FIELDS})


def lookback_days(target: BrainTarget | None) -> int | None:
    """How many days back a run of `target` reads (None: a run with no target reads without a window)."""
    if target is None:
        return None
    return target.lookback_days or LOOKBACK_DAYS["project" if target.project_id else "workspace"]


def fade_days(target: BrainTarget | None) -> int:
    """How many days an open finding of `target` stays fresh without a run seeing it again."""
    return (target.fade_days if target is not None else None) or FADE_DAYS


def _today_start(now=None) -> dt.datetime:
    now = now or timezone.now()
    return now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def today(target: BrainTarget, now=None) -> dict:
    """What `target` spent since midnight UTC: runs started, outputs written, tokens its runs' conversations used."""
    from apps.events.models import SessionUsage  # noqa: PLC0415 — events reads this app's neighbours

    since = _today_start(now)
    runs = BrainRun.objects.filter(target=target, created_at__gte=since)
    outputs = BrainOutput.objects.filter(run__target=target, created_at__gte=since).count()
    sessions = [s for s in runs.values_list("session_id", flat=True) if s]
    tokens = (SessionUsage.objects.filter(session_id__in=sessions, day=since.date())
              .aggregate(n=Sum(F("input_tokens") + F("output_tokens")))["n"] or 0) if sessions else 0
    return {"runs": runs.count(), "outputs": outputs, "tokens": int(tokens)}


def start_refusal(target: BrainTarget, now=None) -> str:
    """Why `target` may start no run today for its budget, or ""."""
    lim, used = limits(target), today(target, now)
    if used["runs"] >= lim.max_runs_per_day:
        return f"its budget of {lim.max_runs_per_day} run(s) a day is spent"
    if used["outputs"] >= lim.max_outputs_per_day:
        return f"its budget of {lim.max_outputs_per_day} output(s) a day is spent"
    if lim.max_tokens_per_day is not None and used["tokens"] >= lim.max_tokens_per_day:
        return f"its budget of {lim.max_tokens_per_day} tokens a day is spent"
    return ""


def run_left(run: BrainRun) -> dict:
    """What `run` may still read and write: `{"sources": n, "outputs": n}` (outputs also within its target's day)."""
    lim = limits(run.target)
    read = BrainRunRead.objects.filter(run=run).count()
    wrote = BrainOutput.objects.filter(run=run).count()
    outputs = lim.max_outputs_per_run - wrote
    if run.target_id is not None:
        outputs = min(outputs, lim.max_outputs_per_day - today(run.target)["outputs"])
    return {"sources": max(0, lim.max_sources_per_run - read), "outputs": max(0, outputs)}


def out(target: BrainTarget) -> dict:
    """The budget and today's use, as the brain page shows them."""
    return {**asdict(limits(target)), "lookback_days": lookback_days(target), "fade_days": fade_days(target),
            "today": today(target)}
