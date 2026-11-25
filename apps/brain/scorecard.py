"""How well each learner target's findings land (`GET /api/brain/scorecard`, the brain page's Setup tab).

Per target at the pinned workspace and below (`api_manage.targets_out`), over the findings its runs made that the viewer
may read (`outputs.visible_to`, the same legs as the Findings tab, never wider; a merged duplicate is not counted):

* **weeks**: the last `WEEKS` weeks (UTC, Monday first), the findings made each week by where each stands now;
* **precision**: of those, the share the people took up, `(accepted + done) / (accepted + done + dismissed)`; null
  while nothing has been decided;
* **open** and **faded**: the open or accepted findings now, split by whether a run re-saw them within the window of
  their type (`findings.faded`);
* **came back**: findings made in the weeks that repeat one already done (`recurs_of`): a fix that did not hold.

Counts only, never a finding's words."""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from . import findings, outputs
from .api_manage import targets_out
from .models import BrainOutput

#: How many weeks the scorecard covers.
WEEKS = 8
STATUSES = (findings.OPEN, findings.ACCEPTED, findings.DONE, findings.DISMISSED)


def week_starts(now=None, weeks: int = WEEKS) -> list[dt.date]:
    """The Mondays (UTC) of the last `weeks` weeks, oldest first, this week's last."""
    today = (now or timezone.now()).astimezone(dt.UTC).date()
    monday = today - dt.timedelta(days=today.weekday())
    return [monday - dt.timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]


def precision(accepted: int, done: int, dismissed: int) -> float | None:
    decided = accepted + done + dismissed
    return round((accepted + done) / decided, 3) if decided else None


def of_target(rows: list[BrainOutput], starts: list[dt.date], now=None) -> dict:
    """One target's figures from its findings (`rows`, already the viewer's)."""
    now = now or timezone.now()
    first = dt.datetime.combine(starts[0], dt.time.min, tzinfo=dt.UTC)
    weeks = {d: dict.fromkeys(STATUSES, 0) for d in starts}
    totals = dict.fromkeys(STATUSES, 0)
    came_back = open_now = faded_now = 0
    memo: dict = {}
    for o in rows:
        if o.status in findings.FOLDS_INTO and o.state == BrainOutput.ACTIVE:
            if findings.faded(o, findings.fade_days_of(o, memo), now):
                faded_now += 1
            else:
                open_now += 1
        if o.created_at < first or o.status not in totals:
            continue
        day = o.created_at.astimezone(dt.UTC).date()
        week = day - dt.timedelta(days=day.weekday())
        if week in weeks:
            weeks[week][o.status] += 1
        totals[o.status] += 1
        came_back += o.recurs_of_id is not None
    return {"weeks": [{"week": d, **weeks[d]} for d in starts], **totals,
            "precision": precision(totals[findings.ACCEPTED], totals[findings.DONE], totals[findings.DISMISSED]),
            "open_now": open_now, "faded_now": faded_now, "came_back": came_back}


def scorecard(user, pin: str, slugs, *, as_person: bool, acting=None, now=None) -> dict:
    """Every target at `pin` and below in `slugs`, with its figures over the findings `user` may read."""
    slugs = set(slugs)
    aimed = [t for t in targets_out(acting if acting is not None else user, pin) if t["workspace"] in slugs]
    starts = week_starts(now)
    visible = (outputs.visible_to(user, as_person=as_person, acting=acting)
               .filter(workspace_id__in=slugs, run__target_id__in=[t["id"] for t in aimed])
               .exclude(status=findings.DUPLICATE)
               .select_related("capability", "run"))
    by_target: dict[int, list] = {}
    for o in visible:
        by_target.setdefault(o.run.target_id, []).append(o)
    return {"weeks": starts, "targets": [
        {"target": t["id"], "learner": t["learner"], "label": t["label"], "workspace": t["workspace"],
         "project": t["project"], **of_target(by_target.get(t["id"], []), starts, now)} for t in aimed]}
