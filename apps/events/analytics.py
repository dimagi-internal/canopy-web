"""Usage analytics over a set of workspaces — a read over `Activity` and `TurnUsage` only.

Those two tables are the whole input on purpose: they are append-only, indexed by
(workspace, time), and untouched by retention, so a number shown here does not
change when a transcript is purged or an event is pruned. Anything worth counting
is emitted as a fact first (`activity.emit`), then shows up here for free.

Three breakdowns share one aggregation (`_breakdown`): by agent, by repository
(work no agent runs — a repo turn, a person's own session in a repo), and by
person. Days are UTC calendar days.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Case, Count, F, Max, Q, Sum, When
from django.db.models.functions import TruncDate
from django.utils import timezone

from . import activity
from .models import Activity, TurnUsage

_TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
_EMPTY = {"turns": 0, "failed": 0, "sessions": 0, "messages": 0, "settings": 0, "mcp_calls": 0,
          "people": 0, "last_at": None, "active_days": 0}


def window(days: int, now=None) -> tuple[int, dt.date, dt.datetime]:
    """THE "last N days": N UTC calendar days ending today, as (days, first day,
    start instant). Every "N days" figure on any screen uses this, so Health's
    per-agent turn count and Analytics' cannot disagree under the same label."""
    now = now or timezone.now()
    days = max(1, min(days, 365))
    start_day = (now - dt.timedelta(days=days - 1)).date()
    return days, start_day, dt.datetime.combine(start_day, dt.time.min, tzinfo=dt.UTC)


def agent_turns(agent_slugs: list[str], *, days: int = 30, now=None) -> dict[str, dict]:
    """{agent slug: {"n": turns started in `window(days)`, "last_at": latest turn
    ever}} from the fact log — the per-agent count Health shows beside Analytics'."""
    _, _, since = window(days, now)
    started = Activity.objects.filter(kind=activity.TURN_ENQUEUED, agent_slug__in=agent_slugs).order_by()
    out = {row["agent_slug"]: {"n": 0, "last_at": row["last_at"]}
           for row in started.values("agent_slug").annotate(last_at=Max("at"))}
    for row in started.filter(at__gte=since).values("agent_slug").annotate(n=Count("id")):
        out[row["agent_slug"]]["n"] = row["n"]
    return out


def _tokens(qs, group: str | None = None) -> dict:
    """{group value: token sums}, or the one total when `group` is None."""
    sums = {f: Sum(f) for f in _TOKENS}
    if group is None:
        agg = qs.aggregate(**sums, turns=Count("id"))
        return {**{f: int(agg[f] or 0) for f in _TOKENS}, "turns_with_usage": agg["turns"] or 0}
    return {row[group]: {f: int(row[f] or 0) for f in _TOKENS} for row in qs.values(group).annotate(**sums)}


def _breakdown(facts, usage, field: str, usage_field: str | None = None) -> dict:
    """{value of `field` (agent_slug, project, actor_id): turns, failed or lost,
    chats, messages, settings changed, MCP calls, people, active days, last
    activity, and its tokens}. The ONE aggregation behind every table."""
    rows: dict = {}
    for row in facts.values(field).annotate(
            turns=Count("id", filter=Q(kind=activity.TURN_ENQUEUED)),
            failed=Count("id", filter=Q(kind=activity.TURN_FINISHED, meta__status__in=["failed", "lost"])),
            sessions=Count("id", filter=Q(kind=activity.CHAT_SESSION_CREATED)),
            messages=Count("id", filter=Q(kind=activity.CHAT_MESSAGE)),
            settings=Count("id", filter=Q(kind=activity.SETTING_CHANGED)),
            mcp_calls=Count("id", filter=Q(kind=activity.MCP_CALL)),
            people=Count("actor", filter=Q(actor_kind=Activity.USER), distinct=True),
            last_at=Max("at")):
        key = row.pop(field)
        rows[key] = {**_EMPTY, **row, **{f: 0 for f in _TOKENS}}
    for key, n in (facts.annotate(day=TruncDate("at")).values(field)
                   .annotate(n=Count("day", distinct=True)).values_list(field, "n")):
        if key in rows:
            rows[key]["active_days"] = n
    for key, sums in _tokens(usage, usage_field or field).items():
        rows.setdefault(key, {**_EMPTY, **{f: 0 for f in _TOKENS}}).update(sums)
    return rows


def _heaviest_first(row: dict):
    return (-(row["input_tokens"] + row["output_tokens"] + row["cache_read_tokens"]), -row["turns"])


def build(workspace_slugs: list[str], *, days: int = 30, now=None) -> dict:
    days, start_day, since = window(days, now)
    facts = Activity.objects.filter(workspace_id__in=workspace_slugs, at__gte=since).order_by()
    usage = TurnUsage.objects.filter(workspace_id__in=workspace_slugs, at__gte=since).order_by()
    people = facts.filter(actor_kind=Activity.USER, actor__isnull=False)

    # ---- the daily series -------------------------------------------------------
    series = {start_day + dt.timedelta(days=i): {
        "turns": 0, "sessions": 0, "messages": 0, "active_users": 0,
        "done": 0, "failed": 0, "lost": 0, "cancelled": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
    } for i in range(days)}
    count_kinds = {activity.TURN_ENQUEUED: "turns", activity.CHAT_SESSION_CREATED: "sessions",
                   activity.CHAT_MESSAGE: "messages"}
    for row in (facts.filter(kind__in=count_kinds).annotate(day=TruncDate("at"))
                .values("day", "kind").annotate(n=Count("id"))):
        if row["day"] in series:
            series[row["day"]][count_kinds[row["kind"]]] = row["n"]
    for row in (facts.filter(kind=activity.TURN_FINISHED).annotate(day=TruncDate("at"))
                .values("day", "meta__status").annotate(n=Count("id"))):
        status = row["meta__status"]
        if row["day"] in series and status in ("done", "failed", "lost", "cancelled"):
            series[row["day"]][status] = row["n"]
    for row in (people.annotate(day=TruncDate("at")).values("day")
                .annotate(n=Count("actor", distinct=True))):
        if row["day"] in series:
            series[row["day"]]["active_users"] = row["n"]
    for day, sums in _tokens(usage.annotate(day=TruncDate("at")), "day").items():
        if day in series:
            series[day].update(input_tokens=sums["input_tokens"], output_tokens=sums["output_tokens"],
                               cache_read_tokens=sums["cache_read_tokens"])

    # ---- breakdowns ---------------------------------------------------------------
    agents = sorted(({"agent": k, **v} for k, v in _breakdown(
        facts.exclude(agent_slug=""), usage.exclude(agent_slug=""), "agent_slug").items()),
        key=_heaviest_first)
    repos = sorted(({"repository": k, **v} for k, v in _breakdown(
        facts.exclude(project=""), usage.exclude(project=""), "project").items()),
        key=_heaviest_first)
    emails = dict(people.values_list("actor_id", "actor__email").distinct())
    # What each person worked WITH most: an agent, else a repository.
    worked_with = Case(When(~Q(agent_slug=""), then=F("agent_slug")), default=F("project"))
    top: dict[int, list] = {}
    for uid, where in (people.annotate(where=worked_with).exclude(where="").values("actor_id", "where")
                       .annotate(n=Count("id")).order_by("actor_id", "-n").values_list("actor_id", "where")):
        if len(top.setdefault(uid, [])) < 3:
            top[uid].append(where)
    users = sorted(({"user_id": uid, "email": emails[uid], "top": top.get(uid, []), **v}
                    for uid, v in _breakdown(people, usage.filter(user__isnull=False), "actor_id",
                                             usage_field="user_id").items() if uid in emails),
                   key=_heaviest_first)

    surfaces = {row["surface"] or "other": row["n"] for row in (
        facts.filter(kind=activity.TURN_ENQUEUED).values("surface").annotate(n=Count("id")))}
    return {
        "days": days,
        "since": since,
        "workspaces": sorted(workspace_slugs),
        "totals": {
            "turns": facts.filter(kind=activity.TURN_ENQUEUED).count(),
            "sessions": facts.filter(kind=activity.CHAT_SESSION_CREATED).count(),
            "messages": facts.filter(kind=activity.CHAT_MESSAGE).count(),
            "active_users": people.values("actor").distinct().count(),
            "active_agents": facts.exclude(agent_slug="").values("agent_slug").distinct().count(),
            "active_repositories": facts.exclude(project="").values("project").distinct().count(),
            **_tokens(usage),
        },
        "series": [{"day": d, **v} for d, v in series.items()],
        "surfaces": surfaces,
        "agents": agents,
        "repositories": repos,
        "users": users,
    }
