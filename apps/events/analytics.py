"""Usage analytics over a set of workspaces — a read over `Activity` and `SessionUsage` only.

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
from .models import Activity, SessionUsage

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
    ever, "done"/"failed": turns that finished so (failed counts lost), "people":
    distinct people who did anything with it, "runners": [{name, kind, turns}]
    its finished turns ran on, most first}} from the fact log — the per-agent
    figures Health shows beside Analytics', counted the way `_breakdown` counts."""
    _, _, since = window(days, now)
    facts = Activity.objects.filter(agent_slug__in=agent_slugs).order_by()
    started = facts.filter(kind=activity.TURN_ENQUEUED)
    blank = {"n": 0, "last_at": None, "done": 0, "failed": 0, "people": 0, "runners": []}
    out: dict[str, dict] = {}
    for row in started.values("agent_slug").annotate(last_at=Max("at")):
        out[row["agent_slug"]] = {**blank, "runners": [], "last_at": row["last_at"]}
    recent = facts.filter(at__gte=since)
    for row in recent.values("agent_slug").annotate(
            n=Count("id", filter=Q(kind=activity.TURN_ENQUEUED)),
            done=Count("id", filter=Q(kind=activity.TURN_FINISHED, meta__status="done")),
            failed=Count("id", filter=Q(kind=activity.TURN_FINISHED, meta__status__in=["failed", "lost"])),
            people=Count("actor", filter=Q(actor_kind=Activity.USER), distinct=True)):
        slug = row.pop("agent_slug")
        out.setdefault(slug, {**blank, "runners": []}).update(row)
    for row in (recent.filter(kind=activity.TURN_FINISHED, meta__has_key="runner")
                .values("agent_slug", "meta__runner", "meta__runner_kind").annotate(n=Count("id"))
                .order_by("agent_slug", "-n", "meta__runner")):
        out[row["agent_slug"]]["runners"].append(
            {"name": row["meta__runner"], "kind": row["meta__runner_kind"] or "", "turns": row["n"]})
    return out


def _tokens(qs, group: str | None = None) -> dict:
    """{group value: token sums}, or the one total when `group` is None."""
    sums = {f: Sum(f) for f in _TOKENS}
    if group is None:
        agg = qs.aggregate(**sums, sessions=Count("claude_session", distinct=True))
        return {**{f: int(agg[f] or 0) for f in _TOKENS}, "sessions_with_usage": agg["sessions"] or 0}
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
    usage = SessionUsage.objects.filter(workspace_id__in=workspace_slugs, day__gte=start_day).order_by()
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
    for day, sums in _tokens(usage, "day").items():
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


# ---- the network: who talks to whom, through which threads ------------------------

#: The most threads one network draws. Busiest first (most people, then most turns),
#: so a cap drops one-person threads before shared ones.
MAX_THREADS = 3000


def _contacts_that_are_agents(contact_ids: set[str]) -> dict[str, str]:
    """contact id -> agent slug, for contacts whose address is an agent's: its own
    `email`, its login's, or one of its inbound mailboxes."""
    from apps.agents.models import Agent
    from apps.contacts.models import Contact
    from apps.inbound.models import InboundMailbox

    emails = {str(pk): (email or "").lower() for pk, email in Contact.objects.filter(
        pk__in=[int(i) for i in contact_ids if i.isdigit()]).exclude(email="").values_list("pk", "email")}
    if not emails:
        return {}
    wanted = set(emails.values())
    slug_of: dict[str, str] = {}
    for slug, own, login in Agent.objects.values_list("slug", "email", "user__email"):
        for address in (own, login):
            if address and address.lower() in wanted:
                slug_of.setdefault(address.lower(), slug)
    for address, slug in (InboundMailbox.objects.filter(agent__isnull=False)
                          .values_list("address", "agent__slug")):
        if address and address.lower() in wanted:
            slug_of.setdefault(address.lower(), slug)
    return {pk: slug_of[e] for pk, e in emails.items() if e in slug_of}


def network(workspace_slugs: list[str], *, viewer, days: int = 30, now=None) -> dict:
    """People, contacts and agents, linked through the threads they wrote in — a chat
    (where a bound Slack thread lives too) or an email thread — over `window(days)`.

    Read from `turn.enqueued` facts alone: each names its thread, who asked (a person,
    a contact, or an agent) and the agent that answered (`activity.who_and_where`).
    A thread's title is shown only when `viewer` may open that chat; everything else
    is ids, names and counts, never content."""
    from django.contrib.auth import get_user_model

    from apps.agents.models import Agent
    from apps.canopy_sessions.access import visible_session_q
    from apps.canopy_sessions.models import Session
    from apps.contacts.models import Contact

    days, _, since = window(days, now)
    facts = (Activity.objects.filter(workspace_id__in=workspace_slugs, at__gte=since,
                                     kind=activity.TURN_ENQUEUED, meta__has_key="thread")
             .order_by().values_list("workspace_id", "actor_kind", "actor_id", "agent_slug",
                                     "surface", "meta", "at"))
    threads: dict[str, dict] = {}
    for ws, actor_kind, actor_id, agent_slug, surface, meta, at in facts.iterator():
        key = meta["thread"]
        t = threads.setdefault(key, {"workspace": ws, "agent": agent_slug, "turns": 0,
                                     "surfaces": {}, "writers": {}, "last_at": at})
        t["turns"] += 1
        t["last_at"] = max(t["last_at"], at)
        t["agent"] = t["agent"] or agent_slug
        t["surfaces"][surface] = t["surfaces"].get(surface, 0) + 1
        # A member who wrote by email is a person (their contact rides along);
        # an agent's own login is never a person (`activity._actor_kind`).
        writer = (f"person:{actor_id}" if actor_kind == Activity.USER and actor_id
                  else f"contact:{meta['contact']}" if meta.get("contact")
                  else f"agent:{meta['by_agent']}" if meta.get("by_agent") else None)
        if writer:
            t["writers"][writer] = t["writers"].get(writer, 0) + 1

    # An agent mailing another agent outside its workspace is recorded as a contact
    # (only a member of the receiving workspace is linked as a person), which is right
    # for access but wrong here: draw that sender as the agent it is.
    as_agent = _contacts_that_are_agents({w.partition(":")[2] for t in threads.values()
                                          for w in t["writers"] if w.startswith("contact:")})
    for t in threads.values():
        merged: dict[str, int] = {}
        for w, n in t["writers"].items():
            w = f"agent:{as_agent[w[8:]]}" if w.startswith("contact:") and w[8:] in as_agent else w
            merged[w] = merged.get(w, 0) + n
        t["writers"] = merged

    def humans(t):
        return [w for w in t["writers"] if not w.startswith("agent:")]

    ranked = sorted(threads.items(), key=lambda kv: (-len(humans(kv[1])), -kv[1]["turns"], kv[0]))
    kept = dict(ranked[:MAX_THREADS])

    # Pairs of people (or contacts) who wrote in the same thread — the figure the
    # network is for: is an agent a room people meet in, or a private line?
    pairs = set()
    for t in kept.values():
        h = sorted(humans(t))
        pairs.update((a, b) for i, a in enumerate(h) for b in h[i + 1:])

    # ---- labels -------------------------------------------------------------------
    ids = {"person": set(), "contact": set(), "agent": set()}
    for t in kept.values():
        for w in t["writers"]:
            kind, _, pk = w.partition(":")
            ids[kind].add(pk)
        if t["agent"]:
            ids["agent"].add(t["agent"])
    users = {str(u.pk): (u.get_full_name() or u.email or u.get_username())
             for u in get_user_model().objects.filter(pk__in=[int(i) for i in ids["person"] if i.isdigit()])}
    contacts = {str(pk): (name or email or f"{source} contact") for pk, name, email, source in (
        Contact.objects.filter(pk__in=[int(i) for i in ids["contact"] if str(i).isdigit()])
        .values_list("pk", "display_name", "email", "source"))}
    agents = {slug: (name, ws) for slug, name, ws in Agent.objects.filter(slug__in=ids["agent"])
              .values_list("slug", "name", "workspace_id")}
    session_ids = [k.partition(":")[2] for k in kept if k.startswith("session:")]
    titles = dict(Session.objects.filter(visible_session_q(viewer), pk__in=session_ids)
                  .values_list("pk", "title"))
    titles = {str(pk): title for pk, title in titles.items()}

    # ---- nodes and links ------------------------------------------------------------
    nodes: dict[str, dict] = {}
    links: list[dict] = []

    def actor(node_id: str, kind: str, label: str, ws: str, turns: int) -> None:
        n = nodes.setdefault(node_id, {"id": node_id, "kind": kind, "label": label, "workspace": ws,
                                       "workspaces": [], "turns": 0, "threads": 0, "channel": "",
                                       "ref": "", "openable": False})
        n["turns"] += turns
        n["threads"] += 1
        if ws not in n["workspaces"]:
            n["workspaces"].append(ws)

    for key, t in kept.items():
        kind, _, ref = key.partition(":")
        channel = max(t["surfaces"], key=t["surfaces"].get) if t["surfaces"] else ""
        agent_name = agents.get(t["agent"], (t["agent"], ""))[0] or t["agent"]
        title = titles.get(ref, "") if kind == "session" else ""
        fallback = f"{'Email' if kind == 'mail' else channel.capitalize() or 'Chat'}"
        nodes[key] = {"id": key, "kind": "thread", "label": title or (
            f"{fallback} with {agent_name}" if agent_name else fallback),
            "workspace": t["workspace"], "workspaces": [t["workspace"]], "turns": t["turns"],
            "threads": 1, "channel": "email" if kind == "mail" else channel, "ref": ref,
            "agent": t["agent"], "openable": kind == "mail" or ref in titles, "last_at": t["last_at"]}
        for writer, turns in t["writers"].items():
            wkind, _, pk = writer.partition(":")
            label = (users.get(pk) if wkind == "person" else contacts.get(pk) if wkind == "contact"
                     else agents.get(pk, (pk, ""))[0]) or writer
            actor(writer, wkind, label, t["workspace"], turns)
            links.append({"source": writer, "target": key, "turns": turns, "kind": "wrote"})
        if t["agent"]:
            agent_id = f"agent:{t['agent']}"
            actor(agent_id, "agent", agent_name, t["workspace"], t["turns"])
            if t["agent"] not in (w.partition(":")[2] for w in t["writers"] if w.startswith("agent:")):
                links.append({"source": key, "target": agent_id, "turns": t["turns"], "kind": "answered"})
    for slug, (_, home) in agents.items():
        if f"agent:{slug}" in nodes and home:
            nodes[f"agent:{slug}"]["workspace"] = home
            nodes[f"agent:{slug}"]["ref"] = slug

    count = {k: sum(1 for n in nodes.values() if n["kind"] == k)
             for k in ("person", "contact", "agent", "thread")}
    return {
        "days": days,
        "since": since,
        "workspaces": sorted(workspace_slugs),
        "nodes": list(nodes.values()),
        "links": links,
        "totals": {"people": count["person"], "contacts": count["contact"], "agents": count["agent"],
                   "threads": count["thread"], "shared_pairs": len(pairs),
                   "shared_threads": sum(1 for t in kept.values() if len(humans(t)) > 1),
                   "threads_seen": len(threads)},
        "truncated": len(threads) > len(kept),
    }
