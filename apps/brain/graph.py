"""How work, people, agents and projects relate, for one viewer (`GET /api/workspaces/{slug}/graph`).

Typed nodes (person, agent, session, project, task, workspace, output) and typed edges, each with its kind,
weight and when. Every node and edge is one the viewer could already see on another surface, by the rule that
surface asks; nothing here is a new way in:

* **Sessions** are the viewer's own legs 1 to 6 (`access.readable_sessions(brain=False)`): the graph is not
  one of the surfaces the brain's project leg opens (docs/architecture/brain.md), so a leg-7-only reader sees
  the project and not the session. A sealed session (sensitive, a contact's, words deleted:
  `participants.sealed_reason`) is drawn as the network page draws a thread: its node, who wrote in it and the
  agent that answered, and nothing the brain derives (no project link, no proposal, no lineage); a
  sensitive one never shows its title. A contact is never a node.
* **Projects and tasks** ask `project_read_q` / `task_read_q`: a personal project is its readers' alone.
* **Reporting lines** go only to a person as themselves (`acting_person.refusal`), never through MCP, as
  `GET /api/brain/workspaces/{slug}/reporting/` does: an owner sees every line of the workspaces they own, anyone
  else the lines they are in.
* **Coaching**: the program (who coaches a workspace) is any member's to see (`list_coaches`); who a coach
  reads is the coach's roster (`access.review_grantors`) and the viewer's own opt-in.
* **Outputs** are `outputs.visible_to`, their lineage drawn only to sessions already drawn.
* **Proposed** links (`include=proposed`) are `associate.propose_many`, the compute `associate` writes and the
  preview shows, over the drawn unsealed sessions, marked `proposed` and stored nowhere.

One query per kind of thing, whatever the number of sessions; capped (`MAX_*`), with `notes` saying what was
left out."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.agents.services.projects import project_read_q, task_read_q
from apps.canopy_sessions import access
from apps.canopy_sessions.models import CoachingOptIn, CoachingRole, SessionParticipant
from apps.harness.models import Turn
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import associate, outputs, participants
from .models import BRAIN_RUN_KEY, BrainSource, OutputLineage, ReportingLine, SourceLink, SourceParticipant

#: The most of each kind one graph draws; the most recent first.
MAX_SESSIONS, MAX_PROJECTS, MAX_OUTPUTS, MAX_DAYS = 400, 200, 100, 90

# Node types.
PERSON, AGENT, SESSION, PROJECT, TASK, WORKSPACE, OUTPUT = (
    "person", "agent", "session", "project", "task", "workspace", "output")
NODE_TYPES = (PERSON, AGENT, SESSION, PROJECT, TASK, WORKSPACE, OUTPUT)

# Edge kinds.
PARTICIPATES = "participates"   # person -> session: in it (a participant, wrote a line or started a turn there)
STARTED = "started"             # person -> session: created it
RAN = "ran"                     # agent -> session: answered turns in it
DISPATCHED = "dispatched_from"  # task -> session: a turn raised from the task ran in it
LINKED = "linked"               # session -> project: a `SourceLink` (tier, how), or a proposed one
REPORTS_TO = "reports_to"       # person -> manager
COACHES = "coaches"             # coach -> person they read, or coach -> workspace (the program)
DERIVED_FROM = "derived_from"   # output -> session it was learned from (`OutputLineage`)
ABOUT = "about"                 # output -> the person or project it is about
WITHIN = "within"               # agent / project / task -> workspace or project; workspace -> parent
EDGE_KINDS = (PARTICIPATES, STARTED, RAN, DISPATCHED, LINKED, REPORTS_TO, COACHES, DERIVED_FROM, ABOUT, WITHIN)

#: Link tiers a viewer is shown; `rejected` only when it is their own call.
SHOWN_TIERS = (SourceLink.CORE, SourceLink.ADJACENT, SourceLink.CANDIDATE)


def _name(u: dict) -> str:
    full = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
    return full or u.get("email") or u.get("username") or f"person {u['pk']}"


def tree(viewer, root: Workspace) -> list[str]:
    """`root` and every workspace below it the viewer is in (a member, or inherited from an owner/admin role)."""
    mine = wsvc.user_workspace_slugs(viewer)
    return [ws.slug for ws, _ in wsvc.subtree(root, lambda s: s in mine)]


#: Where the brain's own rows are read from. Only `live` (the tables) today; `brain_reads` is the one place that
#: reads them, so a snapshot source is one change there.
LIVE = "live"


@dataclass(frozen=True)
class BrainReads:
    """Every brain-table row one graph draws, read by `brain_reads`."""

    participants: list   # (session id, user id): `SourceParticipant`, people since taken out too
    links: list          # `SourceLink` rows on unsealed sessions, as dicts, tiers the viewer may see
    lines: list          # live `ReportingLine` rows the viewer is shown, as dicts; empty unless `as_person`
    outputs: list        # `BrainOutput` rows `outputs.visible_to` lets the viewer read, newest first, capped
    more_outputs: bool   # the cap left some out
    lineage: list        # (output id, session id): `OutputLineage` to unsealed sessions


def brain_reads(viewer, slugs, *, sessions, unsealed, as_person: bool, manager_leg: bool,
                source: str = LIVE) -> BrainReads:
    """The brain's rows for one graph, from `source`. Each read keeps the rule of the surface that shows it
    (`build`'s docstring); the proposed layer is not here, it is the pure compute (`associate.propose_many`)."""
    if source != LIVE:
        raise ValueError(f"unknown brain source {source!r}")
    participants = list(SourceParticipant.objects.filter(source__session_id__in=list(sessions), user__isnull=False)
                        .values_list("source__session_id", "user_id"))
    links = [x for x in SourceLink.objects.filter(source__session_id__in=unsealed,
                                                  subject_type=SourceLink.PROJECT_SUBJECT)
             .values("source__session_id", "subject", "tier", "how", "confidence", "evidence", "by_id", "updated_at")
             if x["tier"] in SHOWN_TIERS or (x["tier"] == SourceLink.REJECTED and x["by_id"] == viewer.pk)]
    lines = []
    if as_person:
        owned = perms.slugs_with(viewer, perms.OWN)
        lines = list(ReportingLine.objects.filter(workspace_id__in=slugs, ended_at__isnull=True)
                     .filter(Q(workspace_id__in=owned) | Q(person=viewer) | Q(manager=viewer))
                     .values("person_id", "manager_id", "workspace_id", "created_at"))
    outs = list(outputs.visible_to(viewer, as_person=manager_leg).filter(workspace_id__in=slugs)
                .order_by("-created_at", "-pk").values("pk", "title", "kind", "workspace_id", "subject_type",
                                                       "subject", "created_at")[:MAX_OUTPUTS + 1])
    more = len(outs) > MAX_OUTPUTS
    outs = outs[:MAX_OUTPUTS]
    lineage = list(OutputLineage.objects.filter(output_kind="brain.output",
                                                output_id__in=[str(o["pk"]) for o in outs],
                                                source__session_id__in=unsealed)
                   .values_list("output_id", "source__session_id"))
    return BrainReads(participants, links, lines, outs, more, lineage)


def build(viewer, slugs, *, days: int = 30, proposed: bool = False, as_person: bool = False,
          manager_leg: bool = False, now=None, source: str = LIVE) -> dict:
    """The graph over `slugs` (the caller resolved them to workspaces the viewer is in) for the last `days`.
    `as_person`: the request is the viewer themselves, off MCP, so reporting lines may be drawn.
    `manager_leg`: `api_admin.manager_leg_open`, passed to `outputs.visible_to`. `source`: where the brain's
    rows are read from (`brain_reads`)."""
    days = max(1, min(int(days), MAX_DAYS))
    now = now or timezone.now()
    since = now - dt.timedelta(days=days)
    slugs = sorted(set(slugs))
    mine = wsvc.user_workspace_slugs(viewer)
    notes: list[str] = []

    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def node(nid: str, typ: str, label: str, *, workspace: str = "", ref: str = "", at=None, **extra) -> dict:
        n = nodes.get(nid)
        if n is None:
            n = nodes[nid] = {"id": nid, "type": typ, "label": label, "workspace": workspace, "ref": ref,
                              "at": at, "weight": 0, "sensitive": False, **extra}
        elif at is not None and (n["at"] is None or at > n["at"]):
            n["at"] = at
        return n

    def edge(source: str, target: str, kind: str, *, weight: float = 1, at=None, **extra) -> None:
        key = (source, target, kind, extra.get("proposed", False))
        e = edges.get(key)
        if e is None:
            edges[key] = {"source": source, "target": target, "kind": kind, "weight": weight, "at": at,
                          "tier": "", "how": "", "proposed": False, "evidence": [], "confidence": None, **extra}
        else:
            e["weight"] += weight
            if at is not None and (e["at"] is None or at > e["at"]):
                e["at"] = at

    # ---- workspaces ------------------------------------------------------------------------------------
    for slug, name, parent in Workspace.objects.filter(slug__in=slugs).values_list("slug", "display_name", "parent_id"):
        node(f"workspace:{slug}", WORKSPACE, name or slug, workspace=slug, ref=slug)
        if parent in slugs:
            edge(f"workspace:{slug}", f"workspace:{parent}", WITHIN)

    # ---- sessions: legs 1 to 6 only -----------------------------------------------------------------------
    fields = ["id", "title", "workspace_id", "agent_id", "created_by_id", "contact_id", "metadata",
              "content_deleted_at", "created_at", "updated_at"] + (["activity"] if proposed else [])
    rows = list(access.readable_sessions(viewer, workspace_slugs=slugs, brain=False)
                .filter(updated_at__gte=since).exclude(metadata__has_key=BRAIN_RUN_KEY)
                .only(*fields).order_by("-updated_at", "pk")[:MAX_SESSIONS + 1])
    if len(rows) > MAX_SESSIONS:
        rows = rows[:MAX_SESSIONS]
        notes.append(f"Only the {MAX_SESSIONS} most recently active sessions are drawn.")
    sessions = {s.pk: s for s in rows}
    ids = list(sessions)
    why = {s.pk: participants.sealed_reason(s) for s in rows}
    sealed = {pk for pk, reason in why.items() if reason}
    unsealed = [pk for pk in ids if pk not in sealed]
    for s in rows:
        sensitive = why[s.pk] == BrainSource.SEALED_SENSITIVE
        label = "Sensitive conversation" if sensitive else (s.title or "Untitled session")
        n = node(f"session:{s.pk}", SESSION, label, workspace=s.workspace_id, ref=str(s.pk), at=s.updated_at)
        n["sensitive"] = sensitive

    # ---- who is in them (people; an agent's login is drawn as its agent) -------------------------------------
    people: dict[int, list] = {}       # user id -> [(session id, kind, weight, at)]
    for s in rows:
        if s.created_by_id is not None:
            people.setdefault(s.created_by_id, []).append((s.pk, STARTED, 1, s.created_at))
    for sid, uid, at in SessionParticipant.objects.filter(session_id__in=ids).values_list(
            "session_id", "user_id", "created_at"):
        people.setdefault(uid, []).append((sid, PARTICIPATES, 1, at))
    brain = brain_reads(viewer, slugs, sessions=ids, unsealed=unsealed, as_person=as_person,
                        manager_leg=manager_leg, source=source)
    for sid, uid in brain.participants:
        people.setdefault(uid, []).append((sid, PARTICIPATES, 0, None))   # since taken out, or a line they wrote

    turns = (Turn.objects.filter(Q(chat_session_id__in=ids) | Q(canopy_session_id__in=ids))
             .values("chat_session_id", "canopy_session_id", "agent_id", "initiator_user_id", "raised_from_task_id")
             .annotate(n=Count("pk"), last=Max("created_at")).order_by())
    ran: dict[tuple, list] = {}           # (agent id, session id) -> [turns, last]
    dispatched: dict[tuple, list] = {}    # (task id, session id) -> [turns, last]
    for t in turns:
        sid = t["canopy_session_id"] if t["canopy_session_id"] in sessions else t["chat_session_id"]
        if t["agent_id"]:
            r = ran.setdefault((t["agent_id"], sid), [0, t["last"]])
            r[0] += t["n"]
            r[1] = max(r[1], t["last"])
        if t["initiator_user_id"]:
            people.setdefault(t["initiator_user_id"], []).append((sid, PARTICIPATES, t["n"], t["last"]))
        if t["raised_from_task_id"] and sid not in sealed:
            d = dispatched.setdefault((t["raised_from_task_id"], sid), [0, t["last"]])
            d[0] += t["n"]
            d[1] = max(d[1], t["last"])
    for s in rows:
        if s.agent_id and (s.agent_id, s.pk) not in ran:
            ran[(s.agent_id, s.pk)] = [0, s.updated_at]

    # ---- agents: those of the viewer's workspaces ------------------------------------------------------------
    agent_ids = {a for a, _ in ran}
    agents = {a["pk"]: a for a in Agent.objects.filter(
        Q(pk__in=agent_ids) | Q(user_id__in=list(people)) | Q(workspace_id__in=slugs), workspace_id__in=mine)
        .values("pk", "slug", "name", "workspace_id", "user_id")}
    login_of = {a["user_id"]: a for a in agents.values() if a["user_id"]}
    for a in agents.values():
        if a["workspace_id"] in slugs or a["pk"] in agent_ids or a["user_id"] in people:
            node(f"agent:{a['slug']}", AGENT, a["name"] or a["slug"], workspace=a["workspace_id"], ref=a["slug"])
            if a["workspace_id"] in slugs:
                edge(f"agent:{a['slug']}", f"workspace:{a['workspace_id']}", WITHIN)
    for (aid, sid), (n, last) in ran.items():
        a = agents.get(aid)
        if a is not None:
            edge(f"agent:{a['slug']}", f"session:{sid}", RAN, weight=n, at=last)

    # ---- people ------------------------------------------------------------------------------------------------
    users = {u["pk"]: u for u in get_user_model().objects.filter(pk__in=[u for u in people if u not in login_of])
             .values("pk", "first_name", "last_name", "email", "username")}

    def person(uid: int) -> str | None:
        if uid in login_of:
            a = login_of[uid]
            return f"agent:{a['slug']}"
        u = users.get(uid)
        if u is None:
            return None
        return node(f"person:{uid}", PERSON, _name(u), ref=str(uid))["id"]

    for uid, seen in people.items():
        pid = person(uid)
        if pid is None:
            continue
        for sid, kind, weight, at in seen:
            kind = RAN if pid.startswith("agent:") else kind
            edge(pid, f"session:{sid}", kind, weight=weight, at=at)

    # ---- projects and tasks ------------------------------------------------------------------------------------
    links = brain.links
    would = associate.propose_many([s for s in rows if s.pk not in sealed]) if proposed else {}
    task_rows = {t["pk"]: t for t in AgentTask.objects.filter(
        task_read_q(viewer), pk__in={tid for tid, _ in dispatched}, workspace_id__in=mine)
        .values("pk", "key", "title", "status", "project_id", "workspace_id", "updated_at")}
    wanted = ({int(x["subject"]) for x in links if x["subject"].isdecimal()}
              | {t["project_id"] for t in task_rows.values() if t["project_id"]}
              | {p.project.pk for ps in would.values() for p in ps if p.tier})
    project_rows = list(AgentProject.objects.filter(project_read_q(viewer), workspace_id__in=slugs)
                        .filter(Q(pk__in=wanted) | Q(status=AgentProject.ACTIVE))
                        .values("pk", "handle", "name", "workspace_id", "visibility", "updated_at")
                        .order_by("-updated_at", "pk")[:MAX_PROJECTS + len(wanted)])
    if len([p for p in project_rows if p["pk"] not in wanted]) > MAX_PROJECTS:
        notes.append(f"Only the {MAX_PROJECTS} most recently changed active projects are drawn beside the linked ones.")
    projects = {p["pk"]: p for p in project_rows}
    for p in project_rows:
        node(f"project:{p['pk']}", PROJECT, p["name"] or p["handle"], workspace=p["workspace_id"], ref=p["handle"],
             at=p["updated_at"])
        edge(f"project:{p['pk']}", f"workspace:{p['workspace_id']}", WITHIN)
    for x in links:
        pk = int(x["subject"]) if x["subject"].isdecimal() else None
        if pk in projects:
            edge(f"session:{x['source__session_id']}", f"project:{pk}", LINKED, at=x["updated_at"], tier=x["tier"],
                 how=x["how"], confidence=x["confidence"], evidence=[str(e) for e in (x["evidence"] or [])][:6])
    stored = {(x["source__session_id"], x["subject"]): SourceLink(how=x["how"], tier=x["tier"], by_id=x["by_id"])
              for x in links}
    for sid, ps in would.items():
        for p in ps:
            row = stored.get((sid, str(p.project.pk)))
            unchanged = row is not None and (row.how, row.tier) == (p.how, p.tier)
            # Only what the brain would change: a link it would make where the stored one would not keep its place.
            if p.tier and p.project.pk in projects and not unchanged and not associate.outranks(
                    row, SourceLink(how=p.how, tier=p.tier)):
                edge(f"session:{sid}", f"project:{p.project.pk}", LINKED, tier=p.tier, how=p.how, proposed=True,
                     evidence=[str(e) for e in p.evidence][:6], at=sessions[sid].updated_at)
    for t in task_rows.values():
        node(f"task:{t['key']}", TASK, f"{t['key']} {t['title']}", workspace=t["workspace_id"], ref=t["key"],
             at=t["updated_at"], status=t["status"])
        if t["project_id"] in projects:
            edge(f"task:{t['key']}", f"project:{t['project_id']}", WITHIN)
    for (tid, sid), (n, last) in dispatched.items():
        if tid in task_rows:
            edge(f"task:{task_rows[tid]['key']}", f"session:{sid}", DISPATCHED, weight=n, at=last)

    # ---- reporting lines: to the person as themselves, as the reporting route shows them -------------------------
    if as_person:
        lines = brain.lines
        _people_nodes(node, users, {x["person_id"] for x in lines} | {x["manager_id"] for x in lines})
        for x in lines:
            edge(f"person:{x['person_id']}", f"person:{x['manager_id']}", REPORTS_TO, at=x["created_at"],
                 how=x["workspace_id"])

    # ---- coaching: the program, the coach's roster, the viewer's own opt-in -----------------------------------
    roles = list(CoachingRole.objects.filter(workspace_id__in=slugs, revoked_at__isnull=True)
                 .values("workspace_id", "user_id", "agent_id", "agent__slug", "agent__name",
                         "agent__workspace_id", "created_at"))
    roster = set(access.review_grantors(viewer).values_list("user_id", flat=True))
    opted_in = CoachingOptIn.objects.filter(user=viewer, revoked_at__isnull=True).exists()
    _people_nodes(node, users, {r["user_id"] for r in roles if r["user_id"]} | roster | ({viewer.pk} if opted_in else set()))
    viewer_agent = login_of.get(viewer.pk) or Agent.objects.filter(user_id=viewer.pk).values(
        "pk", "slug", "name", "workspace_id", "user_id").first()
    viewer_coach_id = (f"agent:{viewer_agent['slug']}" if viewer_agent else f"person:{viewer.pk}")
    for r in roles:
        if r["agent_id"]:
            if r["agent__workspace_id"] not in mine:
                continue
            coach = node(f"agent:{r['agent__slug']}", AGENT, r["agent__name"] or r["agent__slug"],
                         workspace=r["agent__workspace_id"], ref=r["agent__slug"])["id"]
        else:
            coach = f"person:{r['user_id']}"
        edge(coach, f"workspace:{r['workspace_id']}", COACHES, at=r["created_at"])
        if opted_in and coach != viewer_coach_id:
            edge(coach, f"person:{viewer.pk}", COACHES, at=r["created_at"])
    if roster and viewer_agent is not None:
        node(viewer_coach_id, AGENT, viewer_agent["name"] or viewer_agent["slug"],
             workspace=viewer_agent["workspace_id"], ref=viewer_agent["slug"])
    elif roster:
        _people_nodes(node, users, {viewer.pk})
    for uid in roster:
        if f"person:{uid}" in nodes and f"person:{uid}" != viewer_coach_id:
            edge(viewer_coach_id, f"person:{uid}", COACHES)

    # ---- learning outputs, as `outputs.visible_to` lets the viewer read them -----------------------------------
    outs, lineage = brain.outputs, brain.lineage
    if brain.more_outputs:
        notes.append(f"Only the {MAX_OUTPUTS} newest learning outputs are drawn.")
    for o in outs:
        node(f"output:{o['pk']}", OUTPUT, o["title"] or o["kind"], workspace=o["workspace_id"], ref=str(o["pk"]),
             at=o["created_at"], status=o["kind"])
        about = (f"person:{o['subject']}" if o["subject_type"] == "person"
                 else f"project:{o['subject']}" if o["subject_type"] == "project" else "")
        if about in nodes:
            edge(f"output:{o['pk']}", about, ABOUT, at=o["created_at"])
    for oid, sid in lineage:
        edge(f"output:{oid}", f"session:{sid}", DERIVED_FROM)

    # ---- weights and totals --------------------------------------------------------------------------------------
    out_edges = [e for e in edges.values() if e["source"] in nodes and e["target"] in nodes]
    for e in out_edges:
        nodes[e["source"]]["weight"] += 1
        nodes[e["target"]]["weight"] += 1
    totals = {t: sum(1 for n in nodes.values() if n["type"] == t) for t in NODE_TYPES}
    kinds = {k: sum(1 for e in out_edges if e["kind"] == k and not e["proposed"]) for k in EDGE_KINDS}
    kinds["proposed"] = sum(1 for e in out_edges if e["proposed"])
    return {"days": days, "since": since, "workspaces": slugs, "proposed": proposed,
            "reporting": as_person, "nodes": list(nodes.values()), "edges": out_edges,
            "totals": {"nodes": totals, "edges": kinds}, "notes": notes,
            "truncated": bool(notes)}


def _people_nodes(node, users: dict, ids) -> None:
    """Make a node for each person in `ids` not yet drawn (one query for those not yet read)."""
    missing = [i for i in ids if i and i not in users]
    if missing:
        users.update({u["pk"]: u for u in get_user_model().objects.filter(pk__in=missing)
                      .values("pk", "first_name", "last_name", "email", "username")})
    agent_logins = set(Agent.objects.filter(user_id__in=[i for i in ids if i]).values_list("user_id", flat=True)) \
        if ids else set()
    for i in ids:
        if i in users and i not in agent_logins:
            node(f"person:{i}", PERSON, _name(users[i]), ref=str(i))

