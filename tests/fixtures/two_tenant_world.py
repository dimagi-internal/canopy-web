"""Two tenants, mirrored, for the cross-tenant sweep (`tests/test_two_tenant_isolation.py`).

Workspaces A and B sit in different organisations (two tenant trees). Each holds the
same kinds of object, built by one function, so a route that works on A's object has
the twin it would wrongly reach in B. Every B object carries `CANARY` in its words; A's
carry nothing. Alice owns A and is in nothing else; Bob owns B.

Ids are uniform per side so one value names "B's one of these" for any kind: every
integer pk is `INT[side]`, every UUID pk `UUID[side]`, every slug, handle, run id,
narrative slug and thread key `SLUG[side]`. A request built from the A values is the
control; swapping one parameter to the B value is a cross-tenant reference.
"""
from __future__ import annotations

import functools
import json
import uuid

from django.apps import apps
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from apps.agents.models import (
    Agent,
    AgentProject,
    AgentTask,
    Label,
    ProjectMilestone,
    SavedView,
    Vault,
)
from apps.api.api import api
from apps.canopy_sessions.models import Message, RunnerBinding
from apps.canopy_sessions.models import Session as ChatSession
from apps.dynamic_views.models import DynamicView
from apps.harness.models import Runner, Turn
from apps.harness.testing import enrolled
from apps.meetings.models import Meeting
from apps.reviews.models import ReviewRequest
from apps.session_sharing.models import ArcShareToken, SessionArc, ShareToken
from apps.session_sharing.models import Session as SharedSession
from apps.storyboards.models import Storyboard
from apps.walkthroughs.models import NarrativeSource, Walkthrough
from apps.workspaces.models import Workspace, WorkspaceMembership

CANARY = "zzbcanary"
SIDES = ("a", "b")
INT = {"a": 880001, "b": 990001}
UUID = {"a": uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a"),
        "b": uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")}
SLUG = {"a": "zza-shared", "b": "zzb-shared"}
WS = {"a": "zza-ws", "b": "zzb-ws"}


def _words(side: str, what: str) -> str:
    """The words a row carries: B's carry the canary, A's do not."""
    return f"{CANARY}-{what}" if side == "b" else f"plain-{what}"


def _build_side(side: str, person: User, sharer: User) -> dict:
    n, u, s, w = INT[side], UUID[side], SLUG[side], lambda what: _words(side, what)
    ws = Workspace.objects.create(slug=WS[side], display_name=f"Tenant {side.upper()}", created_by=person)
    WorkspaceMembership.objects.create(user=person, workspace=ws, role=WorkspaceMembership.OWNER)
    login = User.objects.create_user(f"{side}-agent-login", f"{side}-agent@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(id=n, slug=s, name=w("agent.name"), description=w("agent.description"),
                                 workspace=ws, owner=person, user=login,
                                 repo_url=f"https://github.com/dimagi/{s}", repo_ref="main")
    project = AgentProject.objects.create(id=n, workspace=ws, handle=s, name=w("project.name"),
                                          outcome=w("project.outcome"), notes=w("project.notes"),
                                          owner_user=person, agent=agent, ext_id=s, created_by_agent=agent)
    parent = AgentTask.objects.create(id=n, workspace=ws, project=project, agent=agent, created_by_agent=agent,
                                      title=w("task.title"), notes=w("task.notes"), ext_id=s,
                                      status=AgentTask.TODO)
    child = AgentTask.objects.create(workspace=ws, project=project, agent=agent, created_by_agent=agent,
                                     parent=parent, title=w("subtask.title"), status=AgentTask.TODO)
    other = AgentTask.objects.create(workspace=ws, project=project, agent=agent, created_by_agent=agent,
                                     title=w("othertask.title"), status=AgentTask.TODO)
    Label.objects.create(id=n, workspace=ws, name=w("label"))
    ProjectMilestone.objects.create(id=n, project=project, name=w("milestone"), description=w("milestone.d"))
    SavedView.objects.create(id=n, workspace=ws, owner=person, name=w("savedview"), shared=True)
    Vault.objects.create(id=u, workspace=ws, kind=Vault.WORKSPACE, name=w("vault"), created_by=person)
    DynamicView.objects.create(id=n, workspace=ws, project=project, slug=s, title=w("view.title"),
                               description=w("view.description"), code=f"// {w('view.code')}")

    runner = Runner.objects.create(id=u, name=f"{side}-box", kind="laptop", owner=person, workspace=ws)
    enrolled(runner, ws)
    chat = ChatSession.objects.create(id=u, workspace=ws, agent=agent, created_by=person, title=w("chat.title"))
    Message.objects.create(session=chat, turn_index=0, role="user", content=w("chat.message"))
    RunnerBinding.objects.create(session=chat, runner=runner, thread_key=s, session_key=s,
                                 summary=w("binding.summary"))
    Turn.objects.create(id=u, agent=agent, origin=Turn.ORIGIN_API, idempotency_key=f"{side}-turn",
                        prompt=w("turn.prompt"), result_note=w("turn.result"), workspace=ws,
                        initiator_user=person, enqueued_by=person)

    # B's link shares are made by Carol, a teammate who is in BOTH tenants: a list that
    # scopes by "made by a teammate" rather than by the row's workspace shows them to A.
    shared = SharedSession.objects.create(id=n, workspace=ws, owner=sharer, slug=s, title=w("shared.title"),
                                          visibility=SharedSession.VISIBILITY_LINK)
    ShareToken.objects.create(session=shared, created_by=sharer, token=f"{side}-share-token")
    arc = SessionArc.objects.create(id=n, workspace=ws, owner=sharer, slug=s, title=w("arc.title"),
                                    visibility="link")
    ArcShareToken.objects.create(arc=arc, created_by=sharer, token=f"{side}-arc-token")

    review = ReviewRequest.objects.create(id=u, run_id=s, narrative_slug=s, gate="concept_change", version=1,
                                          workspace=ws, owner=person, request_json={"narrative": w("narrative.story"),
                                                                      "title": w("narrative.title")})
    Walkthrough.objects.create(id=u, title=w("walkthrough.title"), description=w("walkthrough.description"),
                               kind="video", role=Walkthrough.ROLE_HERO_VIDEO, owner=person, drive_file_id=s,
                               drive_folder_id=s, content_type="video/mp4", size_bytes=1, workspace=ws,
                               run_id=s, narrative_slug=s, visibility="link")
    NarrativeSource.objects.create(id=u, workspace=ws, narrative_slug=s, narrative_review_id=review.id,
                                   role="recipe", content_type="text/plain", size_bytes=1, sha256="0" * 64,
                                   owner=person)
    Storyboard.objects.create(id=n, slug=s, title=w("storyboard.title"), workspace=ws)
    Meeting.objects.create(id=u, workspace=ws, owner=person, project=project, agent=agent, title=w("meeting.title"))
    return dict(ws=ws, person=person, login=login, agent=agent, project=project, task=parent, subtask=child, other=other,
                runner=runner, chat=chat, review=review)


def _stamp(world: dict) -> None:
    """What an editor of A can do with no access to B: upload a link-shared artifact in A
    carrying B's run id and narrative slug. Both are free text the uploader picks, so a
    read that groups by them across workspaces hands B's narrative to A through it."""
    a, b = world["a"], SLUG["b"]
    Walkthrough.objects.create(title="plain-stamped", kind="video", role=Walkthrough.ROLE_HERO_VIDEO,
                               owner=a["person"], drive_file_id="stamped", drive_folder_id="stamped",
                               content_type="video/mp4", size_bytes=1, workspace=a["ws"], run_id=b,
                               narrative_slug=b, visibility="link")


def make_world() -> dict:
    """Alice owns A and is in nothing else; Bob owns B; Carol is an editor in both."""
    alice = User.objects.create_user("alice", "alice@dimagi.com", "pw")
    bob = User.objects.create_user("bob", "bob@dimagi.com", "pw")
    carol = User.objects.create_user("carol", "carol@dimagi.com", "pw")
    world = {"a": _build_side("a", alice, alice), "b": _build_side("b", bob, carol),
             "alice": alice, "bob": bob, "carol": carol}
    for side in SIDES:
        WorkspaceMembership.objects.create(user=carol, workspace=world[side]["ws"], role=WorkspaceMembership.EDITOR)
    _stamp(world)
    return world


# ---- what to send -------------------------------------------------------------------

@functools.cache
def schema() -> dict:
    return json.loads(json.dumps(api.get_openapi_schema(), default=str))


def operations() -> list[tuple[str, str, dict]]:
    """(method, path, operation) for every operation in the real schema."""
    return [(m, p, op) for p, item in schema()["paths"].items() for m, op in item.items()]


#: Names that never reference an object: values the caller chooses, not lookups.
NOT_A_REFERENCE = {
    "limit", "offset", "since", "until", "before", "after", "q", "query", "status", "state", "kind", "category",
    "draft", "template", "source", "actor", "days", "scope", "mine", "subsystem", "recurring", "dry_run",
    "cursor", "page", "order", "sort", "format", "platform", "start", "end", "provider", "action", "title",
    "name", "description", "notes", "text", "body", "summary", "reason", "note", "label", "wait_s", "folder",
    "visibility", "role", "priority", "health", "cron", "timezone", "method", "op", "outcome", "error",
    "idempotency_key", "batch_key", "ext_id", "slug_hint", "cut_id", "cut_scene_ids",
}

#: The object a path segment names, so `/projects/{ref}` is a project and `/tasks/{ref}` a task.
SEGMENT_KIND = {
    "projects": "project_ref", "workspace-projects": "project_ref", "tasks": "task_key", "agents": "agent",
    "workspaces": "workspace", "storyboards": "slug", "narratives": "slug", "runs": "slug", "release": "slug",
    "arcs": "slug", "sessions": "slug", "canopy-sessions": "uuid", "runners": "uuid", "turns": "uuid",
    "walkthroughs": "uuid", "reviews": "uuid", "meetings": "uuid", "vaults": "uuid", "labels": "int",
    "milestones": "int", "dynamic-views": "int", "views": "int", "people": "person",
}


def reference_values(world: dict, side: str) -> dict[str, object]:
    """What each reference name takes on `side`. A name not listed falls back on its JSON-schema
    type (integer → INT, uuid → UUID, string → SLUG), so a new route is swept by default."""
    t = world[side]
    uid = str(UUID[side])
    return {
        "workspace": WS[side], "ws": WS[side], "to_workspace": WS[side],
        "key": t["task"].key, "task_key": t["task"].key, "task": t["task"].key, "parent": t["other"].key,
        "keys": t["task"].key, "project": t["project"].address, "project_ref": t["project"].handle,
        "project_slug": SLUG[side], "agent_project": INT[side], "agent": SLUG[side], "agent_slug": SLUG[side],
        "slug": SLUG[side], "handle": SLUG[side], "ref": t["task"].key,
        "user_id": t["person"].id, "person_id": t["person"].pk, "person": t["person"].pk,
        "email": t["person"].email, "owner_email": t["person"].email,
        "runner_id": uid, "runner": uid, "session_id": uid, "session": uid, "turn_id": uid, "turn": uid,
        "vault_id": uid, "meeting_id": uid, "wid": uid, "rid": uid, "review_id": uid, "uuid": uid,
        "narrative_review_id": uid, "walkthrough_id": uid, "lease_id": uid,
        "thread_key": SLUG[side], "run_id": SLUG[side], "narrative_slug": SLUG[side],
        "token": f"{side}-share-token", "number": 1, "int": INT[side],
    }


def _resolve(spec: dict, comps: dict) -> dict:
    while "$ref" in spec:
        spec = comps[spec["$ref"].split("/")[-1]]
    for key in ("anyOf", "oneOf"):
        if key in spec:
            options = [o for o in spec[key] if o.get("type") != "null"]
            if options:
                return _resolve(options[0], comps)
    if "allOf" in spec and len(spec["allOf"]) == 1:
        return _resolve(spec["allOf"][0], comps)
    return spec


def _is_reference(name: str, spec: dict, values: dict) -> bool:
    """Does a field name an object? Its name says so (a known reference, or `*_id`/`*_slug`/`*_key`),
    and its type can hold one; an integer only when it is an id."""
    if name in NOT_A_REFERENCE or spec.get("enum") or spec.get("format") in ("date", "date-time", "email"):
        return False
    typ = spec.get("type")
    if typ not in (None, "string", "integer"):
        return False
    known = name in values or name.endswith(("_id", "_slug", "_key", "_ref"))
    if typ == "integer" and "maximum" in spec and spec["maximum"] < INT["a"]:
        return False
    return known


def _reference(name: str, spec: dict, side: str, values: dict, kind: str = ""):
    v = values.get(kind) if kind else None
    if v is None:
        v = values.get(name)
    typ = spec.get("type")
    if v is None:
        if typ == "integer":
            return INT[side]
        if spec.get("format") == "uuid" or name.endswith("_id") or name == "id":
            return str(UUID[side])
        return SLUG[side]
    if typ == "integer" and not isinstance(v, int):
        return INT[side]
    if typ == "string" and not isinstance(v, str):
        return str(v)
    return v


def _filler(name: str, spec: dict, comps: dict, values: dict):
    """A value for a field that names nothing — the same on both sides."""
    if "default" in spec and spec["default"] is not None:
        return spec["default"]
    if spec.get("enum"):
        return spec["enum"][0]
    typ, fmt = spec.get("type"), spec.get("format")
    if typ == "integer":
        lo, hi = spec.get("minimum", 1), spec.get("maximum", 10**6)
        return max(lo, min(hi, 1))
    if typ == "number":
        return 1
    if typ == "boolean":
        return False
    if typ == "object":
        return {}
    if typ == "array":
        return []
    if fmt == "date-time":
        return timezone.now().isoformat()
    if fmt == "date":
        return timezone.localdate().isoformat()
    if fmt == "email" or name in ("email", "address", "to"):
        return values["email"]
    if fmt in ("uri", "url") or name.endswith("_url") or name == "url":
        return "https://example.test/x"
    return "x"


#: `sides[OTHERS] = OMIT` leaves out every optional body reference but the one under test.
OTHERS, OMIT = "_others", "omit"


class _Builder:
    """Builds one request's values. Each reference takes the side `sides` names for its
    ident (default A); every ident seen is collected in `refs`."""

    def __init__(self, comps: dict, world: dict, sides: dict[str, str]):
        self.comps, self.world, self.sides = comps, world, sides
        self.refs: list[str] = []
        self.a = reference_values(world, "a")

    def ref(self, ident: str, name: str, spec: dict, kind: str = ""):
        self.refs.append(ident)
        side = self.sides.get(ident, "a")
        return _reference(name, spec, side, reference_values(self.world, side), kind)

    def value(self, ident: str, name: str, spec: dict, required: bool, depth: int = 0):
        spec = _resolve(spec, self.comps)
        required = required and "default" not in spec  # a field with a default may be left out
        if _is_reference(name, spec, self.a):
            if not required and ident not in self.sides and self.sides.get(OTHERS) == OMIT:
                # The minimal variant: only the references the route requires, plus the one
                # under test. An optional A reference beside it (an A parent next to a B
                # project) can refuse the request for the wrong reason and hide a leak.
                self.refs.append(ident)
                return None
            return self.ref(ident, name, spec)
        typ = spec.get("type")
        if typ == "object" and spec.get("properties") and depth < 3:
            return self.obj(ident, spec, depth + 1)
        if typ == "array" and depth < 3:
            items = _resolve(spec.get("items", {}), self.comps)
            singular = name[:-1] if name.endswith("s") else name
            if items.get("type") in ("string", "integer") and _is_reference(singular, items, self.a):
                return [self.ref(f"{ident}[]", singular, items)]
            if spec.get("minItems") or items.get("properties") and required:
                return [self.value(f"{ident}[0]", singular, items, True, depth + 1)]
        if not required:
            return None
        return _filler(name, spec, self.comps, self.a)

    def obj(self, prefix: str, spec: dict, depth: int = 0) -> dict:
        out = {}
        required = set(spec.get("required", []))
        for name, fspec in (spec.get("properties") or {}).items():
            ident = f"{prefix}.{name}" if prefix else f"body:{name}"
            v = self.value(ident, name, fspec, name in required, depth)
            if v is not None:
                out[name] = v
        return out


def request_parts(method: str, path: str, op: dict, comps: dict, world: dict, sides: dict[str, str]):
    """(url, query, body, files, refs) for one request: each reference parameter takes the
    value of the side `sides` names for it (default A). `refs` is every reference parameter
    by ident (`path:slug`, `query:project`, `body:parent`, `body:tasks[0].parent` …)."""
    b = _Builder(comps, world, sides)
    url, query = path, {}
    segments = path.split("/")
    for p in op.get("parameters", []):
        name, loc, spec = p["name"], p["in"], _resolve(p.get("schema", {}), comps)
        ident = f"{loc}:{name}"
        if loc == "path":
            i = next(n for n, seg in enumerate(segments) if "{%s}" % name in seg)
            kind = SEGMENT_KIND.get(segments[i - 1], "") if i else ""
            if spec.get("enum") or name in NOT_A_REFERENCE and kind == "":
                value = _filler(name, spec, comps, b.a)
            else:
                value = b.ref(ident, name, spec, kind)
            url = url.replace("{%s}" % name, str(value))
        elif loc == "query":
            if _is_reference(name, spec, b.a):
                # An optional filter is sent only when it is the one under test: on the
                # control it would narrow a list to A's one object and hide B's rows.
                if p.get("required") or ident in sides:
                    query[name] = b.ref(ident, name, spec)
            elif p.get("required"):
                query[name] = _filler(name, spec, comps, b.a)
    body, files = None, None
    content = (op.get("requestBody") or {}).get("content", {})
    if "application/json" in content:
        bspec = _resolve(content["application/json"].get("schema", {}), comps)
        if bspec.get("type") == "array":
            body = [b.value("body[0]", "item", bspec.get("items", {}), True, 1)]
        else:
            body = b.obj("", bspec)
    elif "multipart/form-data" in content:
        fspec = _resolve(content["multipart/form-data"].get("schema", {}), comps)
        files = {}
        for name, pspec in (fspec.get("properties") or {}).items():
            pspec = _resolve(pspec, comps)
            if pspec.get("format") == "binary":
                files[name] = ("x.mp4", b"\x00" * 16, "video/mp4")
            else:
                v = b.value(f"form:{name}", name, pspec, name in set(fspec.get("required", [])))
                if v is not None:
                    files[name] = v if isinstance(v, str) else json.dumps(v)
    # Pinned: the canonical tenant URL a real client sends (`/api/w/<ws>/…`).
    if url.startswith("/api/") and not url.startswith("/api/w/"):
        url = f"/api/w/{WS['a']}/" + url[len("/api/"):]
    return url, query, body, files, b.refs


# ---- what must not change -----------------------------------------------------------

@functools.cache
def _b_scoped_models():
    """Models with a concrete FK to Workspace, and models with a concrete FK to one of those."""
    direct = [m for m in apps.get_models()
              if any(f.many_to_one and f.concrete and f.related_model is Workspace for f in m._meta.fields)]
    direct_set = set(direct)
    children = [m for m in apps.get_models() if m not in direct_set and m is not Workspace
                and any(f.many_to_one and f.concrete and f.related_model in direct_set for f in m._meta.fields)]
    return direct, children


@functools.cache
def b_scoped_tables() -> frozenset[str]:
    """Every table a B row (or a row pointing at one) can live in."""
    direct, children = _b_scoped_models()
    return frozenset(m._meta.db_table for m in [Workspace, *direct, *children])


def b_snapshot(world: dict, skip: frozenset = frozenset()) -> dict[str, int]:
    """A fingerprint of everything in tenant B, and of every row anywhere that points at it.

    Direct rows: those whose workspace FK names B (or Workspace B itself). Child rows: those
    pointing at a direct B row — wherever they live, so a row CREATED in A that references
    B's task, project or session changes the fingerprint too."""
    b = WS["b"]
    direct, children = _b_scoped_models()
    out: dict[str, int] = {"workspace": hash(repr(list(Workspace.objects.filter(slug=b).values_list())))}
    b_pks: dict[type, set] = {}
    for m in direct:
        if m._meta.label in skip:
            continue
        q = models.Q()
        for f in m._meta.fields:
            if f.many_to_one and f.concrete and f.related_model is Workspace:
                q |= models.Q(**{f.attname: b})
        rows = list(m.objects.filter(q).values_list())
        b_pks[m] = {r[0] for r in rows}
        out[m._meta.label] = hash(tuple(sorted(map(repr, rows))))
    for m in children:
        if m._meta.label in skip:
            continue
        q = models.Q()
        for f in m._meta.fields:
            if f.many_to_one and f.concrete and f.related_model in b_pks and b_pks[f.related_model]:
                q |= models.Q(**{f"{f.attname}__in": b_pks[f.related_model]})
        if not q:
            continue
        out[m._meta.label] = hash(tuple(sorted(map(repr, m.objects.filter(q).values_list()))))
    return out


def changed(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
