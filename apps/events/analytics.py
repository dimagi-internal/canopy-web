"""Usage analytics over a set of workspaces — a read over `Activity` and `TurnUsage` only.

Those two tables are the whole input on purpose: they are append-only, indexed by
(workspace, time), and untouched by retention, so a number shown here does not
change when a transcript is purged or an event is pruned. Anything worth counting
is emitted as a fact first (`activity.emit`), then shows up here for free.

Days are UTC calendar days.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from . import activity
from .models import Activity, TurnUsage

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _usage_sums(qs) -> dict:
    agg = qs.aggregate(**{f: Sum(f) for f in _TOKEN_FIELDS}, cost=Sum("cost_usd"), turns=Count("id"))
    out = {f: int(agg[f] or 0) for f in _TOKEN_FIELDS}
    out["cost_usd"] = float(agg["cost"] or Decimal(0))
    out["turns_with_usage"] = agg["turns"] or 0
    return out


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


def build(workspace_slugs: list[str], *, days: int = 30, now=None) -> dict:
    days, start_day, since = window(days, now)
    facts = Activity.objects.filter(workspace_id__in=workspace_slugs, at__gte=since).order_by()
    usage = TurnUsage.objects.filter(workspace_id__in=workspace_slugs, at__gte=since).order_by()
    people = facts.filter(actor_kind=Activity.USER, actor__isnull=False)

    # ---- the daily series -------------------------------------------------------
    series = {start_day + dt.timedelta(days=i): {
        "turns": 0, "sessions": 0, "messages": 0, "active_users": 0,
        "done": 0, "failed": 0, "lost": 0, "cancelled": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0,
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
    for row in (usage.annotate(day=TruncDate("at")).values("day")
                .annotate(i=Sum("input_tokens"), o=Sum("output_tokens"), c=Sum("cache_read_tokens"),
                          cost=Sum("cost_usd"))):
        if row["day"] in series:
            series[row["day"]].update(input_tokens=int(row["i"] or 0), output_tokens=int(row["o"] or 0),
                                      cache_read_tokens=int(row["c"] or 0),
                                      cost_usd=float(row["cost"] or 0))

    # ---- per agent --------------------------------------------------------------
    agents: dict[str, dict] = {}

    def _agent(slug: str) -> dict:
        return agents.setdefault(slug, {"agent": slug, "turns": 0, "messages": 0, "sessions": 0,
                                        "failed": 0, "people": 0, "input_tokens": 0,
                                        "output_tokens": 0, "cost_usd": 0.0, "last_at": None})

    for row in (facts.exclude(agent_slug="").values("agent_slug").annotate(
            turns=Count("id", filter=Q(kind=activity.TURN_ENQUEUED)),
            messages=Count("id", filter=Q(kind=activity.CHAT_MESSAGE)),
            sessions=Count("id", filter=Q(kind=activity.CHAT_SESSION_CREATED)),
            failed=Count("id", filter=Q(kind=activity.TURN_FINISHED, meta__status__in=["failed", "lost"])),
            people=Count("actor", filter=Q(actor_kind=Activity.USER), distinct=True),
            last_at=Max("at"))):
        a = _agent(row.pop("agent_slug"))
        a.update(row)
    for row in (usage.exclude(agent_slug="").values("agent_slug").annotate(
            i=Sum("input_tokens"), o=Sum("output_tokens"), cost=Sum("cost_usd"))):
        _agent(row["agent_slug"]).update(input_tokens=int(row["i"] or 0), output_tokens=int(row["o"] or 0),
                                         cost_usd=float(row["cost"] or 0))

    # ---- per person -------------------------------------------------------------
    users: dict[int, dict] = {}
    for row in (people.values("actor_id", "actor__email").annotate(
            messages=Count("id", filter=Q(kind=activity.CHAT_MESSAGE)),
            turns=Count("id", filter=Q(kind=activity.TURN_ENQUEUED)),
            sessions=Count("id", filter=Q(kind=activity.CHAT_SESSION_CREATED)),
            settings=Count("id", filter=Q(kind=activity.SETTING_CHANGED)),
            mcp_calls=Count("id", filter=Q(kind=activity.MCP_CALL)),
            agents=Count("agent_slug", filter=~Q(agent_slug=""), distinct=True),
            last_at=Max("at"))):
        uid = row.pop("actor_id")
        users[uid] = {"user_id": uid, "email": row.pop("actor__email") or "", **row,
                      "active_days": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                      "top_agents": []}
    for row in (people.annotate(day=TruncDate("at")).values("actor_id")
                .annotate(n=Count("day", distinct=True))):
        if row["actor_id"] in users:
            users[row["actor_id"]]["active_days"] = row["n"]
    for row in (people.exclude(agent_slug="").values("actor_id", "agent_slug")
                .annotate(n=Count("id")).order_by("actor_id", "-n")):
        u = users.get(row["actor_id"])
        if u is not None and len(u["top_agents"]) < 3:
            u["top_agents"].append(row["agent_slug"])
    for row in (usage.filter(user__isnull=False).values("user_id").annotate(
            i=Sum("input_tokens"), o=Sum("output_tokens"), cost=Sum("cost_usd"))):
        if row["user_id"] in users:
            users[row["user_id"]].update(input_tokens=int(row["i"] or 0), output_tokens=int(row["o"] or 0),
                                         cost_usd=float(row["cost"] or 0))

    surfaces = {row["surface"] or "other": row["n"] for row in (
        facts.filter(kind=activity.TURN_ENQUEUED).values("surface").annotate(n=Count("id")))}
    turn_count = facts.filter(kind=activity.TURN_ENQUEUED).count()
    return {
        "days": days,
        "since": since,
        "workspaces": sorted(workspace_slugs),
        "totals": {
            "turns": turn_count,
            "sessions": facts.filter(kind=activity.CHAT_SESSION_CREATED).count(),
            "messages": facts.filter(kind=activity.CHAT_MESSAGE).count(),
            "active_users": people.values("actor").distinct().count(),
            "active_agents": facts.exclude(agent_slug="").values("agent_slug").distinct().count(),
            **_usage_sums(usage),
        },
        "series": [{"day": d, **v} for d, v in series.items()],
        "surfaces": surfaces,
        "agents": sorted(agents.values(), key=lambda a: (-a["turns"], a["agent"])),
        "users": sorted(users.values(), key=lambda u: (-(u["messages"] + u["turns"]), u["email"])),
    }
