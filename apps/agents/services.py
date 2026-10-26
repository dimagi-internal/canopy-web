"""Business logic for the agent workspace — kept out of the Ninja router so it's
unit-testable without HTTP."""
from __future__ import annotations

import datetime as dt
import re

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from django.utils.text import slugify

from apps.agents.credentials import vault_import
from apps.agents.schemas import AgentVaultOut
from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.contacts import people
from apps.contacts.models import Person
from apps.events import services as events
from apps.harness.dispatch import dispatch, enqueue_task_turn
from apps.harness.models import Runner, RunnerAssignment, Turn, agent_turns_q
from apps.harness.routing import inherited_orders
from apps.harness.runner_requirements import is_sensitive_data_box
from apps.tokens.models import PersonalToken
from apps.workspaces import permissions as perms
from apps.workspaces import role_cap
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import participants, sensitive_data, work
from .definition import definition_key, siblings
from .models import (
    MAX_TASK_DEPTH,
    Agent,
    AgentBootstrapReport,
    AgentCredential,
    AgentCredentialRead,
    AgentProject,
    AgentSkill,
    AgentSync,
    AgentTask,
    AgentTaskAction,
    AgentTaskHumanWhy,
    ZeroKnowledgeValue,
)

_VALID_TASK_STATUS = set(AgentTask.CATEGORY)


def _aware(value):
    if isinstance(value, dt.datetime) and timezone.is_naive(value):
        return value.replace(tzinfo=dt.UTC)
    return value


# ---- agents ----
#: What an agent IS on its boxes: the code they clone and what they install with
#: it. Changing one on an existing agent is its admins' (agents/api.py upsert).
DEFINITION_FIELDS = ("repo_url", "repo_ref", "runtime_engine", "runtime_secrets",
                     "runtime_sources", "clis", "cli_models")


def upsert_agent(data, *, workspace, owner=None) -> Agent:
    """Create or update an agent by slug.

    `workspace` is REQUIRED and keyword-only: `Agent.workspace` is NOT NULL
    (agents/0013), so there is no such thing as creating an agent and homing it
    afterwards. Making the caller supply the tenant up front is the point — the
    old shape created the row unhomed and left the view to home it a few lines
    later, which is how a workspace-less agent was ever representable.

    It is applied on CREATE only (`create_defaults`), never on update: an
    upsert must not silently move an existing agent between tenants. Moving one
    is an explicit, membership-gated action in the view. `owner` likewise: it
    homes a new agent with an owner, and never transfers an existing one.
    """
    defaults = {
        "name": data.name,
        "description": data.description,
        "persona": data.persona,
        "email": data.email,
        "avatar_url": data.avatar_url,
    }
    # Runtime-registry fields (repo pointer / engine / secret refs) are written
    # ONLY when explicitly provided. The plugin re-upserts agents on every sync
    # with these fields absent (None) — a plain default would clobber runtime
    # config back to empty on each heartbeat. Configure once, keep it.
    for field in ("repo_url", "repo_ref", "runtime_engine", "runtime_secrets",
                  "runtime_sources", "runner_preference", "clis", "cli_models"):
        value = getattr(data, field, None)
        if value is not None:
            defaults[field] = value
    # Same rule for the Google account: the plugin's re-upserts omit it.
    google_account = getattr(data, "google_account", None)
    if google_account is not None:
        defaults["google_account"] = google_account.strip().lower()
    agent, _ = Agent.objects.update_or_create(
        slug=data.slug, defaults=defaults, create_defaults={**defaults, "workspace": workspace, "owner": owner,
                                                            **born_zk_fields(owner)}
    )
    return agent


def born_zk_fields(owner) -> dict:
    """Every new agent starts zero-knowledge (canopy-web#1550; the ONLY mode of
    the canopy-web vault, Jonathan 2026-10-17): `zk_enabled` at kv 0 — no vault
    key yet, so every value that arrives is sealed (`zks1`) to whoever may read
    the agent (its owner's vault key, its approved machines) until a holder
    creates the vault key.

    And on the canopy-web vault itself: a new agent's secrets live here unless
    its owner explicitly chooses 1Password (PATCH /credential-source). A person
    with no 1Password — a family workspace — must never be routed to `op`."""
    return {"zk_enabled": True, "zk_kv": 0, "credential_source": Agent.CANOPY_WEB}


def make_zero_knowledge(agent) -> int:
    """Move a custodial agent to zero-knowledge, server side: each stored value
    is sealed to the agent's readers (`zks1`, kv 0) and its custodial copy
    wiped, atomically. canopy-web held these values readable until now; after
    this it cannot. Raises ZkError `zk_no_recipients` when nobody could read
    them (no vault key, no approved machine) — nothing changes then. Returns
    the number of values sealed; a no-op on an agent already zero-knowledge."""
    from django.db import transaction

    from .credentials import zk, zk_seal  # cycle: agents.credentials.zk -> agents.services  # noqa: PLC0415

    with transaction.atomic():
        agent = zk._locked(agent)
        if agent.zk_enabled:
            return 0
        agent.zk_enabled, agent.zk_kv = True, 0
        rows = list(AgentCredential.objects.filter(agent=agent))
        plain = {r.name: r.plaintext() for r in rows if not r.is_zk}
        sealed = {name: zk_seal.seal_for_agent(agent, name, value) for name, value in plain.items()}
        for r in rows:
            if r.name in sealed:
                r.value_zk, r.kv, r.value_enc = sealed[r.name], 0, ""
                r.save(update_fields=["value_zk", "kv", "value_enc", "updated_at"])
        agent.save(update_fields=["zk_enabled", "zk_kv", "updated_at"])
    return len(sealed)


def list_agents() -> list[Agent]:
    return list(Agent.objects.all())


def get_agent(slug: str) -> Agent | None:
    return Agent.objects.filter(slug=slug).first()


def _definition_summary(agent: Agent) -> dict:
    """The repo this instance runs, and which other tenants run it.

    Cheap for the fleet's size (a handful of agents, compared in Python because
    the key needs normalising before it can be matched). If the fleet ever grows
    enough for that to matter, the fix is a stored normalised column — not a
    raw-URL match, which would silently under-report.
    """

    return {
        "key": definition_key(agent.repo_url, agent.repo_ref),
        "repo_url": agent.repo_url,
        "repo_ref": agent.repo_ref,
        "shared_with": sorted({a.workspace_id for a in siblings(agent)}),
    }


def _owner_summary(agent: Agent) -> dict | None:
    owner = agent.owner
    if owner is None:
        return None
    return {"user_id": owner.pk, "name": owner.get_full_name() or owner.email, "email": owner.email}


def _canopy_user_summary(agent: Agent) -> dict | None:
    linked = agent.user
    if linked is None:
        return None
    return {"user_id": linked.pk, "name": linked.get_full_name() or linked.email, "email": linked.email}


class AgentUserLinkError(Exception):
    """The canopy user cannot be linked to this agent; the message says why, as shown."""


def link_canopy_user(agent: Agent, user_id: int | None) -> Agent:
    """Link this agent to the canopy user it IS (`Agent.user`), or unlink with None.

    An agent calls canopy with its own token, and that token signs in as a
    canopy user — `ace@dimagi-ai.com` for ACE. This link is what says that user
    is the agent: an agent acting as itself is never confined against itself
    (`agents/access.py`). It is an identity, not a permission — it grants the
    user nothing it did not already have.

    It was only ever set by migration `agents/0022`, matching on `Agent.email`,
    so agents created with a blank email (echo, ace, eva) had no link and no way
    to get one.

    One user is ONE instance: the column is one-to-one, so of several ACE
    instances exactly one can be `ace@dimagi-ai.com`, and a second attempt says
    which instance already is. The user must be a member of the agent's
    workspace. The agent's `email` is filled from the user when it was blank,
    since for these agents it is the same mailbox.
    """
    from django.contrib.auth import get_user_model

    if user_id is None:
        agent.user = None
        agent.save(update_fields=["user", "updated_at"])
        return agent
    linked = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if linked is None:
        raise AgentUserLinkError("no such canopy user")
    other = Agent.objects.filter(user=linked).exclude(pk=agent.pk).first()
    if other is not None:
        raise AgentUserLinkError(f"{linked.email} is already the canopy user of '{other.slug}' — "
                                 "one user is one agent instance; unlink it there first")
    if not wsvc.is_member(linked, agent.workspace_id):
        raise AgentUserLinkError(f"{linked.email} is not a member of this agent's workspace")
    agent.user = linked
    fields = ["user", "updated_at"]
    if not agent.email:
        agent.email = linked.email
        fields.append("email")
    agent.save(update_fields=fields)
    return agent


class AgentTokenRefused(Exception):
    """The agent's canopy token cannot be minted by this caller; the message says why.
    `status` is the HTTP answer."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def mint_agent_canopy_token(agent: Agent, actor, *, email: str = "", label: str = ""):
    """Mint a PAT for the agent's OWN canopy login (`Agent.user`), creating that login
    first when the agent has none (`agent_login.create_agent_login`). Returns
    `(raw, token, user, created_user)`; the raw value exists only in this return — the
    caller (`canopy agent vault`) writes it straight into the agent's vault.

    The guard is impersonation: a token for `agent.user` signs in AS that user, so this
    must never become "mint a token for a person". With no login it only ever CREATES one
    (see agent_login). With one linked, it mints for it only while that login has never
    signed in interactively (`last_login` is None — an account only tokens use); one that
    has might be a person's, so only a workspace OWNER may, and a workspace owner can
    already make themselves the agent's owner.

    Non-expiring, like the agent's vault key: nothing watches either for expiry, and a
    lapsed one fails every turn silently."""

    from .agent_login import (  # cycle: agents.agent_login -> agents.services  # noqa: PLC0415
        AgentLoginRefused,
        create_agent_login,
    )

    created = False
    user = agent.user
    if user is None:
        address = (agent.email or email or "").strip().lower()
        if "@" not in address:
            raise AgentTokenRefused(
                f"{agent.slug} has no mailbox address to name its canopy login — set the "
                "agent's email first, or pass the address", 422)
        try:
            user = create_agent_login(agent, actor, address)
        except AgentLoginRefused as exc:
            raise AgentTokenRefused(str(exc), exc.status) from exc
        created = True
    elif user.last_login is not None and not perms.can(actor, agent.workspace, perms.OWN):
        raise AgentTokenRefused(
            f"{user.email} has signed in to canopy, so it may be a person's account; only a "
            "workspace owner may mint a token for it", 403)
    raw, token = PersonalToken.create_for_user(
        user=user, label=(label or f"{agent.slug} agent").strip()[:100], ttl_days=0)
    return raw, token, user, created


def give_agent_canopy_login(agent: Agent, actor, *, email: str = "") -> dict:
    """The agent's canopy login in one step, for an agent on the canopy-web vault: mint
    its token (`mint_agent_canopy_token`), store it as its login (`pat_rotation.CANOPY_PAT_REF`) sealed to whoever
    may read the agent (`minted=True` → zks1), and turn on automatic rotation. The raw
    token never leaves this function — the browser button that calls it has no use for
    it, and a response that carried it would put a live login in a page's memory.

    Refuses BEFORE minting when nobody could read the stored token yet (no vault key,
    no approved machine): a token minted and then not storable would be a live login
    with nowhere to go."""
    from django.db import transaction

    from .credentials import (  # cycle: agents.credentials.pat_rotation -> agents.services  # noqa: PLC0415
        pat_rotation,
        zk_seal,
    )

    if agent.credential_source != Agent.CANOPY_WEB:
        raise AgentTokenRefused(
            f"{agent.slug} reads its secrets from 1Password; switch it to the Canopy vault "
            "first, or put its token in 1Password with `canopy agent vault`", 409)
    if agent.zk_enabled and not zk_seal.recipients_for(agent):
        raise AgentTokenRefused(
            "nobody can open this agent's secrets yet — set up your key under Settings → "
            "Security → You (one passkey touch), then try again", 409)
    with transaction.atomic():
        raw, token, user, created = mint_agent_canopy_token(
            agent, actor, email=email, label=f"{agent.slug} agent")
        set_agent_credentials(agent, {pat_rotation.CANOPY_PAT_REF: raw}, user=actor, minted=True)
        del raw
        agent.refresh_from_db()
        rotation = pat_rotation.enable_refusal(agent, actor)
        if rotation is None and not agent.pat_rotation_enabled:
            agent.pat_rotation_enabled = True
            agent.pat_rotation_enabled_by = actor
            agent.save(update_fields=["pat_rotation_enabled", "pat_rotation_enabled_by", "updated_at"])
    return {"user_email": user.email, "created_user": created, "token_id": token.pk,
            "rotation_enabled": agent.pat_rotation_enabled,
            "rotation_note": "" if rotation is None else str(rotation)}


def agent_detail(agent: Agent) -> dict:
    latest = agent.syncs.order_by("-period_end").first()
    return {
        "id": agent.id,
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description,
        "persona": agent.persona,
        "email": agent.email,
        "google_account": agent.google_account,
        "avatar_url": agent.avatar_url,
        "workspace_id": agent.workspace_id,
        "definition": _definition_summary(agent),
        "owner": _owner_summary(agent),
        "canopy_user": _canopy_user_summary(agent),
        "runner_preference": list(agent.runner_preference or []),
        "clis": list(agent.clis or []),
        "cli_models": dict(agent.cli_models or {}),
        "turn_mode": agent.turn_mode,
        "slack_enabled": agent.slack_enabled,
        "credential_source": agent.credential_source,
        "require_device_binding": agent.require_device_binding,
        "op_vault": agent.op_vault or "",
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
        "sync_count": agent.syncs.count(),
        "skill_count": agent.skills.count(),
        "task_count": agent.tasks.count(),
        # Same set as `GET /api/harness/turns/?agent=` (agent_turns_q): counting
        # only `agent.turns` left out every email/Slack turn, which targets the
        # agent's session, so the card and the turn list disagreed (#359).
        "turn_count": Turn.objects.filter(agent_turns_q(agent)).count(),
        "latest_sync_at": latest.period_end if latest else None,
        # "When did this agent last RUN" — read off the dispatch queue, which has a
        # row for every turn the harness sent. It used to be read off the close-out
        # report, which only exists when an agent survived to package itself: ada
        # rendered as never-run (0 / null) against 34 dispatched turns, and every
        # other agent was weeks stale against its own queue.
        #
        # `started_at` (not created_at) is the honest answer — a queued turn nobody
        # picked up is not a run — with created_at as the fallback for rows that
        # predate the runner reporting a start.
        "latest_turn_at": _latest_turn_at(agent),
    }


def _latest_turn_at(agent: Agent):
    """Newest start among turns that actually started, else newest enqueue.

    Deliberately NOT `order_by("-started_at", "-created_at")`: Postgres sorts DESC
    NULLS FIRST, so a single queued turn (started_at IS NULL) would win and the
    fallback would become the answer for every agent with anything in the queue.
    """
    turns = Turn.objects.filter(agent_turns_q(agent))
    started = turns.filter(started_at__isnull=False).order_by("-started_at").first()
    if started is not None:
        return started.started_at
    newest = turns.order_by("-created_at").first()
    return newest.created_at if newest else None


# ---- syncs ----
def upsert_sync(agent: Agent, data) -> AgentSync:
    """Idempotent per (agent, period_start, period_end, source)."""
    period_start = _aware(data.period_start)
    period_end = _aware(data.period_end)
    AgentSync.objects.filter(
        agent=agent,
        period_start=period_start,
        period_end=period_end,
        source=data.source,
    ).delete()
    return AgentSync.objects.create(
        agent=agent,
        period_start=period_start,
        period_end=period_end,
        title=data.title,
        summary=data.summary,
        doc_url=data.doc_url,
        self_grades=data.self_grades,
        source=data.source,
    )


def list_syncs(agent: Agent, limit: int = 100) -> list[AgentSync]:
    return list(agent.syncs.select_related("agent")[:limit])


def delete_sync(agent: Agent, sync_id: int) -> bool:
    """Delete ONE sync by id, scoped to the agent. True if a row was removed.

    upsert_sync is idempotent per (period_start, period_end, source), so re-posting
    only ever corrects a sync for the SAME window. A sync posted with the wrong
    period (or a stray test row) is otherwise unreachable — this is the escape hatch.
    """
    deleted, _ = AgentSync.objects.filter(agent=agent, pk=sync_id).delete()
    return bool(deleted)


# ---- skills ----
@transaction.atomic
def replace_skills(agent: Agent, items: list) -> int:
    """Replace the agent's whole skill catalog so it mirrors the repo."""
    agent.skills.all().delete()
    AgentSkill.objects.bulk_create(
        [
            AgentSkill(
                agent=agent,
                name=s.name,
                description=s.description,
                url=s.url,
                improvement_note=s.improvement_note,
                launchable=s.launchable,
                args_hint=s.args_hint,
            )
            for s in items
        ]
    )
    return agent.skills.count()


def list_skills(agent: Agent) -> list[AgentSkill]:
    return list(agent.skills.select_related("agent"))


# ---- projects ----
#
# A project is the workspace's (`AgentProject`): addressed `<ws>/<handle>`, led
# by a person and/or an agent, worked on by its member agents. The lead agent's
# `Projects/<name>` Drive folder holds its files; canopy keeps the state.


def agent_projects_q(agent: Agent):
    """The projects an agent's board shows: the ones it leads or works on."""
    return Q(agent=agent) | Q(member_agents=agent)


def list_projects(agent: Agent, status: str = "") -> list:
    qs = (AgentProject.objects.filter(agent_projects_q(agent)).distinct()
          .select_related("agent", "owner_user", "workspace"))
    if status:
        qs = qs.filter(status__in=[s.strip() for s in status.split(",") if s.strip()])
    return list(qs)


def get_project(agent: Agent, ref: str):
    """A project an agent names: by its alias ("P3", its own first, then one a
    project it leads or works on carries), by numeric id among those, or by
    handle ("givewell-kb") or address ("connect/givewell-kb"). A bare handle
    is looked up in the agent's workspace, then up its tree (a division's
    agents use the org's projects); an address may also name a workspace below
    the agent's, where an agent of the parent can lead a project."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    base = AgentProject.objects.select_related("agent", "owner_user", "workspace")
    project = base.filter(created_by_agent=agent, ext_id__iexact=ref).first()
    if project is None:
        project = base.filter(agent_projects_q(agent), ext_id__iexact=ref).distinct().first()
    if project is None and ref.isdigit():
        project = base.filter(agent_projects_q(agent), pk=int(ref)).distinct().first()
    if project is None:
        ws, _, handle = ref.rpartition("/")
        lineage = [agent.workspace_id, *agent.workspace.ancestor_slugs()]
        if ws:
            if ws in lineage or may_hold_workspace_project(agent, ws):
                project = get_workspace_project(ws, handle)
        else:
            for slug in lineage:
                project = get_workspace_project(slug, handle)
                if project is not None:
                    break
    return project


def may_hold_workspace_project(steward: Agent, workspace_slug: str) -> bool:
    """May `steward` lead (or be assigned work in) `workspace_slug`? Its own
    workspace, or one below it: an org's agent can curate a division's
    resource (Eva, of dimagi, leads `connect/givewell-kb`), but not the other
    way round, nor across organisations."""

    if workspace_slug == steward.workspace_id:
        return True
    ws = Workspace.objects.filter(slug=workspace_slug).first()
    return ws is not None and steward.workspace_id in ws.ancestor_slugs()


class ProjectScopeError(ValueError):
    """A project write that cannot be done, with its HTTP status."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def get_workspace_project(workspace_slug: str, handle: str):
    return (AgentProject.objects.select_related("agent", "owner_user", "workspace")
            .filter(workspace_id=workspace_slug, handle__iexact=(handle or "").strip()).first())


def workspace_projects(workspace_slugs, status: str = "", steward_workspaces=()) -> list:
    """Owned by any of `workspace_slugs` — or led from any of
    `steward_workspaces` (a parent's agent leading a division's project)."""
    qs = (AgentProject.objects.select_related("agent", "owner_user", "workspace")
          .filter(Q(workspace_id__in=list(workspace_slugs))
                  | Q(agent__workspace_id__in=list(steward_workspaces))))
    if status:
        qs = qs.filter(status__in=[s.strip() for s in status.split(",") if s.strip()])
    return list(qs.order_by("workspace_id", "handle"))


def _clean_handle(handle: str) -> str:
    handle = (handle or "").strip().lower()
    if not re.match(AgentProject.HANDLE_RE, handle):
        raise ProjectScopeError("handle must be 3-60 characters of a-z, 0-9 and '-', "
                                "starting and ending with a letter or digit")
    return handle


def free_handle(workspace_slug: str, name: str, fallback: str = "project") -> str:
    """A handle for a new project from its name, unique in the workspace."""
    base = slugify(name or "")[:56].strip("-")
    if not re.match(AgentProject.HANDLE_RE, base):
        base = slugify(fallback)[:56].strip("-")
    if not re.match(AgentProject.HANDLE_RE, base):
        base = "project"
    taken = set(AgentProject.objects.filter(workspace_id=workspace_slug, handle__startswith=base[:52])
                .values_list("handle", flat=True))
    handle, i = base, 2
    while handle in taken:
        handle = f"{base[:52]}-{i}"
        i += 1
    return handle


def promote_project(project: AgentProject, handle: str, workspace: str = "") -> AgentProject:
    """Give a project the address `<workspace>/<handle>` — a new handle, and/or
    a workspace below its lead agent's. The same address is a no-op; an address
    already taken is refused (409). Its alias, tasks, folder, history and its
    knowledge base's stored objects (`kb_store.root`, not keyed by the address)
    are untouched."""
    handle = _clean_handle(handle)
    workspace = (workspace or "").strip() or project.workspace_id
    if project.handle == handle and project.workspace_id == workspace:
        return project
    if project.agent_id is not None and not may_hold_workspace_project(project.agent, workspace):
        raise ProjectScopeError(f"'{project.agent.slug}' can only lead a project of "
                                f"'{project.agent.workspace_id}' or a workspace below it")
    project.workspace_id = workspace
    project.handle = handle
    try:
        with transaction.atomic():
            project.save(update_fields=["workspace", "handle", "updated_at"])
    except IntegrityError:
        raise ProjectScopeError(f"'{workspace}/{handle}' is already taken", 409) from None
    # A task lives where its project does.
    for task in project.tasks.exclude(workspace_id=workspace):
        work.rehome(task, workspace)
    return project


def set_steward(project: AgentProject, steward: Agent | None) -> AgentProject:
    """Make `steward` the project's lead agent (None: no agent leads it). It
    must belong to the project's workspace or one above it. The address is the
    identity, so callers notice nothing; the alias stays its maker's."""
    if steward is None:
        project.agent = None
        project.save(update_fields=["agent", "updated_at"])
        return project
    if not may_hold_workspace_project(steward, project.workspace_id):
        raise ProjectScopeError(f"the lead agent must be an agent of '{project.workspace_id}' "
                                "or of a workspace above it")
    if steward.pk != project.agent_id:
        project.agent = steward
        project.save(update_fields=["agent", "updated_at"])
    project.member_agents.add(steward)
    return project


def set_member_agents(project: AgentProject, agents: list[Agent]) -> AgentProject:
    """Replace the agents working on it. Its lead agent stays one."""
    for a in agents:
        if not may_hold_workspace_project(a, project.workspace_id):
            raise ProjectScopeError(f"'{a.slug}' is not an agent of '{project.workspace_id}' "
                                    "or of a workspace above it")
    keep = {a.pk for a in agents}
    if project.agent_id:
        keep.add(project.agent_id)
    project.member_agents.set(list(keep))
    return project


def stewarded_workspace_projects(agent: Agent) -> list[str]:
    """Addresses of the projects `agent` leads OUTSIDE its own workspace (a
    division's, led from the org) — what blocks moving it: the move would take
    it out of the tree those projects live in."""
    return [p.address for p in agent.projects.exclude(workspace_id=agent.workspace_id)]


def next_project_ext_id(agent: Agent) -> str:
    """P1, P2, … — from a counter that only ever goes up.

    Not from the highest existing project: deleting P2 would hand "P2" out
    again, and an old link — a task's Links, a Drive folder, an email — would
    then point at different work. The increment is a single UPDATE so two turns
    numbering at once cannot collide.
    """
    Agent.objects.filter(pk=agent.pk).update(project_seq=F("project_seq") + 1)
    agent.refresh_from_db(fields=["project_seq"])
    return f"P{agent.project_seq}"


_PROJECT_FIELDS = ("name", "outcome", "status", "owner_note", "drive_folder_id",
                   "drive_folder_url", "repo_slug", "notes", "priority", "start_date", "target_date")
_PROJECT_STATUS = {s for s, _ in AgentProject.STATUS_CHOICES}


def _norm_project_status(value: str) -> str:
    return value if value in _PROJECT_STATUS else AgentProject.ACTIVE


def new_project(*, workspace: str, name: str, agent: Agent | None = None, by_agent: Agent | None = None,
                by_user=None, handle: str = "", ext_id: str = "", **fields) -> AgentProject:
    """The one constructor of a project row: its workspace, a handle (given, or
    free from its name), its lead agent as a member, and — made through an
    agent's route — that agent's alias."""
    if agent is not None and not may_hold_workspace_project(agent, workspace):
        raise ProjectScopeError(f"'{agent.slug}' can only lead a project of "
                                f"'{agent.workspace_id}' or a workspace below it")
    handle = _clean_handle(handle) if handle else free_handle(workspace, name, fallback=ext_id or "project")
    try:
        with transaction.atomic():
            project = AgentProject.objects.create(
                workspace_id=workspace, handle=handle, agent=agent, name=name,
                created_by_agent=by_agent, ext_id=ext_id,
                created_by_user=by_user if getattr(by_user, "is_authenticated", False) else None,
                **fields)
    except IntegrityError:
        raise ProjectScopeError(f"'{workspace}/{handle}' is already taken", 409) from None
    if agent is not None:
        project.member_agents.add(agent)
    return project


def create_project(agent: Agent | None, data, *, workspace: str = "", by_user=None) -> AgentProject:
    """A project from an API body. Through an agent's route (`agent` given and
    no `workspace`), the agent leads it, it lives in the agent's workspace and
    carries the agent's next `P<n>` alias. Otherwise it is made in `workspace`,
    led by `agent` if one is named."""
    payload = {f: getattr(data, f) for f in _PROJECT_FIELDS if getattr(data, f, None) is not None}
    payload["status"] = _norm_project_status(payload.get("status", AgentProject.ACTIVE))
    if getattr(data, "links", None):
        payload["links"] = [link.model_dump() for link in data.links]
    via_agent = agent is not None and not workspace
    ws = workspace or agent.workspace_id
    owner_email = getattr(data, "owner_email", "") or getattr(data, "lead_email", "")
    payload["owner_user"] = resolve_project_owner_in(ws, owner_email)
    name = payload.pop("name")
    ext_id = ""
    if via_agent:
        ext_id = (getattr(data, "ext_id", "") or "").strip() or next_project_ext_id(agent)
    return new_project(workspace=ws, name=name, agent=agent, by_agent=agent if via_agent else None,
                       by_user=by_user, handle=(getattr(data, "handle", "") or "").strip(),
                       ext_id=ext_id, **payload)


class ProposalStartError(ValueError):
    """Starting a proposed project by PATCH, past its decision card."""


def patch_project(project: AgentProject, data: dict) -> AgentProject:
    if "status" in data and data["status"] is not None:
        new = _norm_project_status(data["status"])
        if (project.status == AgentProject.PROPOSED and new not in
                (AgentProject.PROPOSED, AgentProject.CANCELED, AgentProject.ARCHIVED)):
            raise ProjectScopeError("a proposed project is started through its decision card "
                                    "(approve its decision task), not by changing its status", 409)
    for f in _PROJECT_FIELDS:
        if f in data:
            value = data[f]
            if f == "status":
                value = _norm_project_status(value)
            elif f == "priority":
                value = int(value or 0)
                if not 0 <= value <= 4:
                    raise ValueError("priority is 0 (none) to 4 (low)")
            setattr(project, f, value)
    if "links" in data:
        project.links = data["links"]
    for key in ("owner_email", "lead_email"):
        if key in data:
            project.owner_user = resolve_project_owner_in(project.workspace_id, data[key])
    project.save()
    return project


def project_task_counts(agent: Agent) -> dict:
    """`{project_id: (tasks, still open, waiting on a person)}` in ONE query.

    The board shows a count per project, and resolving it per row would be a
    query per project on a page that lists them all.
    """
    # Every task IN the projects the agent leads or works on, whoever does it.
    project_ids = AgentProject.objects.filter(agent_projects_q(agent)).values("pk")
    rows = (
        AgentTask.objects.filter(project__in=project_ids)
        .values("project_id")
        .annotate(
            total=Count("id"),
            open=Count("id", filter=Q(status__in=LIVE_STATUSES)),
            waiting=Count("id", filter=waiting_q()),
        )
    )
    return {r["project_id"]: (r["total"], r["open"], r["waiting"]) for r in rows}


def set_task_project(task, project) -> None:
    """Put a task in a project (or take it out with None). It moves to the
    project's workspace."""
    task.project = project
    task.save(update_fields=["project", "updated_at"])
    if project is not None and project.workspace_id != task.workspace_id:
        work.rehome(task, project.workspace_id)
    _link_participants(task)


def _link_participants(task) -> None:
    """A task in a project raised by a human's turn makes them a participant
    (fleet brain v1.1). Best-effort: never fails the task write."""

    participants.on_task(task)


# ---- tasks ----
def _norm_status(s: str) -> str:
    return s if s in _VALID_TASK_STATUS else AgentTask.SUGGESTED


_TASK_FIELDS = ("title", "next_action", "status", "owner", "assigned", "confidence",
                "score", "review", "rationale", "source_url", "plan", "due", "notes",
                "drive_folder_id", "drive_folder_url", "priority")


def _norm_confidence(value) -> str:
    value = (value or "").strip().lower()
    return value if value in AgentTask.CONFIDENCE_VALUES else ""


def _norm_priority(value) -> int:
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"priority is 0 (none) to 4 (low), got {value!r}") from None
    if not 0 <= value <= 4:
        raise ValueError(f"priority is 0 (none) to 4 (low), got {value!r}")
    return value


def _norm_field(f: str, value):
    if f == "status":
        return _norm_status(value)
    if f == "confidence":
        return _norm_confidence(value)
    if f == "priority":
        return _norm_priority(value)
    return value


def agent_tasks_q(agent: Agent, prefix: str = ""):
    """The tasks an agent's board shows: the ones it is assigned, and the ones
    it filed that no agent is assigned (a suggestion for a person, say)."""
    p = prefix
    return Q(**{f"{p}agent": agent}) | Q(**{f"{p}created_by_agent": agent, f"{p}agent__isnull": True})


class TaskTreeError(ValueError):
    """A parent that would break the task tree: another agent's task, another
    project's, the task itself or one of its own subtasks (a cycle), or a
    chain deeper than `MAX_TASK_DEPTH`. A ValueError, so every route that
    already maps those to 422 refuses it the same way."""


def _depth_above(task: AgentTask) -> int:
    """How many levels sit above `task` (0 for a top-level task)."""
    depth, node, seen = 0, task, set()
    while node.parent_id is not None and node.pk not in seen:
        seen.add(node.pk)
        node = node.parent
        depth += 1
    return depth


def _height_below(task: AgentTask) -> int:
    """How many levels of subtasks hang under `task` (0 for a leaf). One
    query per level, and the tree is at most `MAX_TASK_DEPTH` deep."""
    if task.pk is None:
        return 0
    height, level = 0, [task.pk]
    while level:
        level = list(AgentTask.objects.filter(parent_id__in=level).values_list("pk", flat=True))
        if level:
            height += 1
        if height > MAX_TASK_DEPTH:  # a corrupt tree; refuse rather than spin
            break
    return height


def resolve_parent(task: AgentTask, ref: str, agent: Agent | None = None) -> AgentTask | None:
    """The task `ref` names as `task`'s parent, checked — or None to clear.

    Refused (TaskTreeError) when the parent is unknown, is the task itself or
    one of its descendants, sits in a different project, or would make the
    chain deeper than MAX_TASK_DEPTH. `task` is the CHILD as it will be saved:
    its `project` must already be the one it will have.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    namer = agent or task.created_by_agent or task.agent
    parent = get_task(namer, ref) if namer is not None else work.task_by_key(ref)
    if parent is None or parent.workspace_id != task.workspace_id:
        raise TaskTreeError(f"parent {ref} is not a task of {namer.slug if namer else task.workspace_id}")
    if task.pk is not None:
        node, seen = parent, set()
        while node is not None and node.pk not in seen:
            if node.pk == task.pk:
                raise TaskTreeError(f"{parent.ref} is {task.ref} or one of its subtasks "
                                    f"— a task cannot be its own ancestor")
            seen.add(node.pk)
            node = node.parent
    if parent.project_id != task.project_id:
        where = parent.project.address if parent.project_id else "no project"
        raise TaskTreeError(f"parent {parent.ref} is in {where}; a subtask lives in its "
                            f"parent's project")
    levels = _depth_above(parent) + 1 + 1 + _height_below(task)
    if levels > MAX_TASK_DEPTH:
        raise TaskTreeError(f"making {parent.ref} the parent would nest tasks {levels} deep "
                            f"(at most {MAX_TASK_DEPTH}) — file the deeper work as its own task")
    return parent


def _kind(value) -> str:
    kind = (value or AgentTask.WORK).strip().lower()
    if kind not in (AgentTask.WORK, AgentTask.IDEA):
        raise ValueError(f"kind must be work|idea, got {value!r}")
    return kind


def _require_words(human_why) -> None:
    """An idea is a PERSON's want: it carries their own words, or it is not one."""
    if not any((w or {}).get("quote", "").strip() for w in (human_why or [])):
        raise ValueError("an idea (kind=idea) carries a person's own words — give at least one "
                         "human_why quote; the agent's own work is kind=work")


def _resolve_who(workspace_slug: str, who: dict):
    """(person, email, name) for one quote's speaker.

    A Person is linked only when canopy knows them in the task's workspace
    (`people.known_in`) — the same gate as every per-person read, so a quote
    cannot be used to learn that canopy knows someone another tenant deals
    with. An email canopy does not know is kept as text: the quote still says
    who. A `person_id` that is not known here is refused — it was explicit."""

    pid = who.get("person_id")
    email = (who.get("email") or "").strip().lower()
    name = (who.get("name") or "").strip()
    person = None
    if pid is not None:
        person = Person.objects.filter(pk=pid).first()
        if person is None or not people.known_in(person, workspace_slug):
            raise ValueError(f"person {pid} is not someone {workspace_slug} knows")
    elif email:
        found = people.by_email(email)
        if found is not None and people.known_in(found, workspace_slug):
            person = found
    if person is not None:
        email = email or people.email_of(person)
        name = name or people.display_name(person)
    return person, email, name


def set_human_why(task: AgentTask, items: list[dict]) -> None:
    """Replace the task's human-why quotes with `items` (T68), in order."""
    rows = []
    for i, item in enumerate(items or []):
        quote = (item.get("quote") or "").strip()
        if not quote:
            raise ValueError("a human_why entry needs the person's quote")
        person, email, name = _resolve_who(task.workspace_id, item.get("who") or {})
        rows.append(AgentTaskHumanWhy(
            task=task, position=i, quote=quote, person=person, who_email=email,
            who_name=name, source_url=(item.get("source_url") or "").strip(),
            said_at=_aware(item.get("said_at")),
        ))
    task.human_whys.all().delete()
    AgentTaskHumanWhy.objects.bulk_create(rows)
    if hasattr(task, "_prefetched_objects_cache"):
        task._prefetched_objects_cache.pop("human_whys", None)


def _descendants(task: AgentTask) -> list[AgentTask]:
    out, level = [], [task.pk]
    for _ in range(MAX_TASK_DEPTH):
        rows = list(AgentTask.objects.filter(parent_id__in=level))
        if not rows:
            break
        out.extend(rows)
        level = [r.pk for r in rows]
    return out

#: What a create may carry beyond `_TASK_FIELDS`, copied straight onto the row.
_TASK_CREATE_EXTRAS = ("ask_body", "on_approve", "batch_key", "origin", "origin_ref", "source")

_ASK_KINDS = {AgentTask.ASK_NONE, AgentTask.ASK_REVIEW, AgentTask.ASK_QUESTION}


def _plain_links(links) -> list:
    return [link.model_dump() if hasattr(link, "model_dump") else dict(link) for link in links or []]


class AssigneeError(ValueError):
    """An assignee canopy cannot give the task to: an unknown person or agent,
    or one outside the task's workspace."""


def resolve_assignee(workspace_slug: str, value: str) -> tuple[Agent | None, object | None]:
    """`(agent, user)` from an assignee string: `""` (nobody), a member's email,
    or an agent's slug (`agent:<slug>` or bare). An agent must belong to the
    workspace or one above it; a person must be a member."""
    value = (value or "").strip()
    if not value:
        return None, None
    if "@" in value:
        return None, _workspace_member(workspace_slug, value, instead="assigned")
    slug = value.removeprefix("agent:").strip()
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None or not may_hold_workspace_project(agent, workspace_slug):
        raise AssigneeError(f"no agent '{slug}' can be assigned work in {workspace_slug}")
    return agent, None


def _task_workspace(agent: Agent | None, project, workspace: str) -> str:
    if project is not None:
        return project.workspace_id
    if workspace:
        return workspace
    if agent is not None:
        return agent.workspace_id
    raise ValueError("a task needs a workspace")


@transaction.atomic
def create_tasks(agent: Agent | None, payloads: list[dict], *, workspace: str = "",
                 by_user=None) -> list[AgentTask]:
    """Create a batch of tasks, idempotent per `idempotency_key`.

    Through an agent's route (`agent` given, `workspace` not): the agent filed
    them, is their assignee unless the payload names one (`assignee`), and each
    carries its `T<n>` alias. Otherwise they are made in `workspace` by
    `by_user`, assigned as the payload says.

    ONE outer transaction, so a batch that cannot route somebody's wait
    (`UnknownPersonError`) leaves nothing behind; each row keeps its own
    SAVEPOINT so a key two producers raced on replays instead of rolling the
    batch back. A key already used by ANOTHER agent is refused rather than
    replayed — handing back somebody else's task would cross a tenant line.
    """
    via_agent = agent is not None and not workspace
    out = []
    for p in payloads:
        key = (p.get("idempotency_key") or "").strip()
        existing = AgentTask.objects.filter(idempotency_key=key).first() if key else None
        if existing is not None:
            if existing.created_by_agent_id != (agent.pk if agent else None):
                raise ValueError(f"idempotency_key {key!r} is already used by another agent")
            out.append(existing)
            continue

        fields = {f: _norm_field(f, p[f]) for f in _TASK_FIELDS if p.get(f) is not None}
        fields.update({f: p[f] for f in _TASK_CREATE_EXTRAS if p.get(f) is not None})
        fields["status"] = _norm_status(fields.get("status", AgentTask.SUGGESTED))
        ask_kind = p.get("ask_kind") or AgentTask.ASK_NONE
        if ask_kind not in _ASK_KINDS:
            raise ValueError(f"ask_kind must be review|question, got {ask_kind!r}")
        if p.get("links"):
            fields["links"] = _plain_links(p["links"])
        # An unknown project reference files the task nowhere rather than failing
        # the create: the task is the thing worth keeping, and a typo in "P7" must
        # not cost the agent the work it just recorded. The response carries
        # `project_ext_id: null`, so the miss is visible.
        ref = str(p.get("project") or "").strip()
        raised_by = p.get("raised_by") or None
        if raised_by and agent is not None and not Turn.objects.filter(pk=raised_by, agent=agent).exists():
            raise ValueError(f"raised_by {raised_by} is not a turn of {agent.slug}")
        project = _project_ref(agent, ref, workspace) if ref else None
        parent_ref = str(p.get("parent") or "").strip()
        parent = None
        ws = _task_workspace(agent, project, workspace)
        if parent_ref:
            # A subtask lives in its parent's project: one named with no project
            # inherits it, so `{"parent": "T66"}` alone files it correctly.
            if not ref:
                found = get_task(agent, parent_ref) if agent is not None else work.task_by_key(parent_ref)
                project = found.project if found is not None else None
                ws = _task_workspace(agent, project, found.workspace_id if found is not None else workspace)
            parent = resolve_parent(AgentTask(agent=agent, created_by_agent=agent, project=project,
                                              workspace_id=ws), parent_ref, agent)
        waiting_on = _workspace_member(ws, p["waiting_on_email"], instead="assigned") \
            if p.get("waiting_on_email") else None
        if "assignee" in p and p["assignee"] is not None:
            assignee_agent, assignee_user = resolve_assignee(ws, p["assignee"])
        else:
            assignee_agent, assignee_user = (agent if via_agent else None), None
        kind = _kind(p.get("kind"))
        if kind == AgentTask.IDEA:
            _require_words(p.get("human_why"))
        ext_id = _claim_ext_id(agent, (p.get("ext_id") or "").strip()) if via_agent else ""
        labels = work.resolve_labels(ws, p.get("labels") or [], create=True) if p.get("labels") else []
        milestone = work.resolve_milestone(project, p.get("milestone")) if p.get("milestone") else None
        rank = float(p["position"]) if p.get("position") is not None else work.top_rank(ws)
        number, task_key = work.allocate_number(ws)
        try:
            with transaction.atomic():  # savepoint
                task = AgentTask.objects.create(
                    workspace_id=ws,
                    number=number,
                    key=task_key,
                    agent=assignee_agent,
                    assignee_user=assignee_user,
                    created_by_agent=agent,
                    created_by_user=by_user if getattr(by_user, "is_authenticated", False) else None,
                    ext_id=ext_id,
                    project=project,
                    parent=parent,
                    kind=kind,
                    ask_kind=ask_kind,
                    idempotency_key=key or None,
                    raised_by_id=raised_by,
                    waiting_on_user=waiting_on,
                    milestone=milestone,
                    rank=rank,
                    **fields,
                )
                if p.get("human_why"):
                    set_human_why(task, p["human_why"])
                if labels:
                    task.labels.set(labels)
                work.record_created(task, by_user=by_user, by_agent=agent)
            out.append(task)
            _link_participants(task)
        except IntegrityError:
            replay = (AgentTask.objects.filter(created_by_agent=agent, idempotency_key=key).first()
                      if key and agent is not None else None)
            if replay is not None:
                out.append(replay)
                continue
            if agent is not None and ext_id and _ext_id_taken(agent, ext_id):  # a concurrent create took it
                raise DuplicateTaskError(f"{agent.slug} already has a task {ext_id}") from None
            raise
    return out


def _project_ref(agent: Agent | None, ref: str, workspace: str):
    """A project named by an agent (alias, handle or address), else by address
    or handle in `workspace`."""
    if agent is not None:
        return get_project(agent, ref)
    ws, _, handle = ref.rpartition("/")
    return get_workspace_project(ws or workspace, handle)


class DuplicateTaskError(Exception):
    """An explicit `ext_id` the agent already uses (in any letter case)."""


def _ext_id_taken(agent: Agent, ext_id: str) -> bool:
    return AgentTask.objects.filter(created_by_agent=agent, ext_id__iexact=ext_id).exists()


def _claim_ext_id(agent: Agent, explicit: str) -> str:
    """The alias a new task filed through an agent's route gets.

    Explicit: refused if the agent already has it in ANY case — lookups are
    case-insensitive (`get_task`), so "t1" beside "T1" would make one of them
    unreachable. An explicit `T<n>` also moves the counter past n.

    Auto: the next counter value that is free. An explicit id may already sit
    ahead of the counter, and handing it out again would 409 every auto create
    from then on (the counter bump rolls back with the failed batch).
    """
    if explicit:
        if "/" in explicit:
            # A task is addressed as `/tasks/{ext_id}/` — a slash cannot be routed.
            raise ValueError(f"ext_id {explicit!r} may not contain '/'")
        if work.looks_like_key(explicit) and not re.fullmatch(r"[Tt]\d+", explicit):
            raise ValueError(f"ext_id {explicit!r} looks like a task key; an alias may not")
        if _ext_id_taken(agent, explicit):
            raise DuplicateTaskError(f"{agent.slug} already has a task {explicit}")
        m = re.fullmatch(r"[Tt](\d+)", explicit)
        if m:
            Agent.objects.filter(pk=agent.pk, task_seq__lt=int(m.group(1))).update(
                task_seq=int(m.group(1)))
        return explicit
    while True:
        ext_id = next_task_ext_id(agent)
        if not _ext_id_taken(agent, ext_id):
            return ext_id


class UnknownPersonError(Exception):
    """canopy cannot route a wait to somebody it has never seen."""


def resolve_waiting_on(agent_or_ws, email: str):
    """The member of the workspace (an agent's, or a slug) with that email, or
    None to clear.

    Scoped to the workspace on purpose: "waiting on you" is a claim on somebody
    canopy can notify AND who can see the work, so routing it to a stranger
    would create a wait nobody will ever answer.
    """
    ws = agent_or_ws if isinstance(agent_or_ws, str) else agent_or_ws.workspace_id
    return _workspace_member(ws, email, instead="assigned")


def resolve_project_owner(agent: Agent, email: str):
    """A project's lead as a real person, or None to clear — the same
    workspace rule as `resolve_waiting_on`, because the lead is who an idea
    added on the team view emails (`ideas.notify`)."""
    return resolve_project_owner_in(agent.workspace_id, email)


def resolve_project_owner_in(workspace_slug: str, email: str):
    return _workspace_member(workspace_slug, email, instead="owner_note")


def _workspace_member(workspace_slug: str, email: str, *, instead: str):
    email = (email or "").strip().lower()
    if not email:
        return None
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(email__iexact=email).first()
    if user is None or not wsvc.is_member(user, workspace_slug):
        raise UnknownPersonError(
            f"{email or 'that person'} is not a member of this workspace — "
            f"put the name in `{instead}` instead"
        )
    return user


@transaction.atomic
def patch_task(task: AgentTask, data, *, by_user=None, by_agent=None) -> AgentTask:
    """Partial update — only fields present in `data` (a dict) are written, and
    each tracked field that changes is a `TaskEvent`. Atomic: a refused quote,
    assignee or tree move leaves the task exactly as it was.

    `assignee`: `""` (nobody), a member's email, or an agent's slug. `labels`
    REPLACES the list (names; unknown ones are made). `position` is the old
    integer order and sets `rank`; `before`/`after` (keys) place it between two
    tasks. Any change but a snooze wakes a snoozed task."""
    before = work.snapshot(task)
    for f in _TASK_FIELDS:
        if f in data:
            setattr(task, f, _norm_field(f, data[f]))
    if "links" in data:
        task.links = data["links"]
    if "waiting_on_email" in data:
        task.waiting_on_user = _workspace_member(task.workspace_id, data["waiting_on_email"],
                                                 instead="assigned")
    if "assignee" in data and data["assignee"] is not None:
        task.agent, task.assignee_user = resolve_assignee(task.workspace_id, data["assignee"])
    if data.get("position") is not None:
        task.rank = float(data["position"])
    if data.get("rank") is not None:
        task.rank = float(data["rank"])
    if "snoozed_until" in data:
        task.snoozed_until = data["snoozed_until"]
    elif task.snoozed_until is not None:
        task.snoozed_until = None
    moved = False
    if "project" in data:
        # Present-and-empty means "take it out of its project"; absent means
        # "leave it where it is" (the schema keeps the two apart).
        ref = (data["project"] or "").strip()
        namer = by_agent or task.created_by_agent or task.agent
        new_project = (_project_ref(namer, ref, task.workspace_id) if ref else None)
        if ref and new_project is None:
            raise ValueError(f"no project {ref!r}")
        moved = (new_project.pk if new_project else None) != task.project_id
        task.project = new_project
        if moved and task.milestone_id and "milestone" not in data:
            task.milestone = None  # a milestone is its project's
    if "parent" in data:
        task.parent = resolve_parent(task, data["parent"] or "", by_agent)
    elif moved and task.parent_id is not None:
        # Moving a subtask out of its parent's project, without naming a new
        # parent, would leave a child filed somewhere its parent is not.
        raise TaskTreeError(f"{task.ref} is a subtask of {task.parent.ref}; move the parent, "
                            f"or send \"parent\": \"\" to detach it first")
    if "milestone" in data:
        task.milestone = work.resolve_milestone(task.project, data["milestone"])
    if data.get("kind") is not None:
        task.kind = _kind(data["kind"])
    task.save()
    if data.get("before") is not None or data.get("after") is not None:
        work.move_task(task, before_key=data.get("before") or "", after_key=data.get("after") or "")
    if "labels" in data and data["labels"] is not None:
        task.labels.set(work.resolve_labels(task.workspace_id, data["labels"], create=True))
    if moved and task.project_id and task.project.workspace_id != task.workspace_id:
        work.rehome(task, task.project.workspace_id, by_user=by_user, by_agent=by_agent)
    if moved:
        # A subtree moves as one: a parent's subtasks follow it to its new project.
        for child in _descendants(task):
            child.project = task.project
            child.save(update_fields=["project", "updated_at"])
            if child.workspace_id != task.workspace_id:
                work.rehome(child, task.workspace_id, by_user=by_user, by_agent=by_agent)
            _link_participants(child)
    if "human_why" in data:
        set_human_why(task, data["human_why"] or [])
    if task.kind == AgentTask.IDEA and not task.human_whys.exists():
        # Atomic: refusing here leaves the task exactly as it was.
        raise ValueError(f"{task.ref} is an idea, and an idea carries a person's own words — "
                         f"give it at least one human_why quote")
    if "project" in data:
        _link_participants(task)
    work.record_changes(task, before, by_user=by_user, by_agent=by_agent)
    return task


def get_task(agent: Agent, ref: str) -> AgentTask | None:
    """A task an agent names: by key (`DIM-142`), or by alias ("T3") — its own
    first, then one it is assigned."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if work.looks_like_key(ref):
        found = work.task_by_key(ref)
        if found is not None:
            return found
    base = AgentTask.objects.select_related("agent", "project", "workspace", "assignee_user", "created_by_agent")
    return (base.filter(created_by_agent=agent, ext_id__iexact=ref).first()
            or base.filter(agent=agent, ext_id__iexact=ref).first())


def list_tasks(agent: Agent) -> list[AgentTask]:
    return list(AgentTask.objects.filter(agent_tasks_q(agent)).select_related("agent"))


# ---- actions: everything a person does TO a task ------------------------
#
# Five of them — approve, decline, reply, nudge, done — through one function.
# Each is recorded as an `AgentTaskAction` row, which is both the task's history
# ("who approved this and why" is the closing row) and, while `pending`, the
# agent's to-do: it drains pending rows on its next turn and marks each applied.
#
# An action that hands the agent work STARTS a turn rather than leaving a row
# for whenever the agent next runs (Jonathan, 2026-10-08): approve always does,
# nudge always does, answering an open question does (whoever answers), and any
# other reply does when an editor writes it. The task's own `on_approve` specs
# run when it has them; otherwise canopy-web writes the one turn from the card
# (`apps.harness.dispatch.enqueue_task_turn`). A row whose turn was enqueued is
# `applied` — the turn IS the follow-up.


class ClosedAskError(Exception):
    """The task is not in a state this action applies to — the ask is already
    closed, the task is finished, or (nudge) it is not in progress. Acting
    anyway would dispatch twice or start unapproved work."""


#: action -> (closes the ask?, new status or None, needs agent follow-up?)
_EFFECT = {
    AgentTaskAction.APPROVE: (True, AgentTask.IN_PROGRESS, True),
    AgentTaskAction.DECLINE: (True, AgentTask.DECLINED, False),
    AgentTaskAction.REPLY: (None, None, True),  # closes only a question
    AgentTaskAction.NUDGE: (False, None, True),
    AgentTaskAction.DONE: (True, AgentTask.DONE, False),
    # Triage: decide it without starting it. A review ask ("should I do this?")
    # is answered — yes, later — so it closes; a question stays open.
    AgentTaskAction.ACCEPT: (None, AgentTask.TODO, False),
    AgentTaskAction.BACKLOG: (None, AgentTask.BACKLOG, False),
    AgentTaskAction.SNOOZE: (False, None, False),
    AgentTaskAction.DUPLICATE: (True, AgentTask.DECLINED, False),
}

#: How long a snooze lasts when the comment names no date.
SNOOZE_DEFAULT = dt.timedelta(days=7)


def _snooze_until(comment: str):
    """A snooze's end: an ISO date (or datetime) in the comment, else a week."""
    text = (comment or "").strip()
    if text:
        try:
            when = dt.datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"snooze takes a date (YYYY-MM-DD) or nothing, got {text!r}") from None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        if when <= timezone.now():
            raise ValueError("snooze until a date in the future")
        return when
    return timezone.now() + SNOOZE_DEFAULT


@transaction.atomic
def act(task: AgentTask, *, action: str, comment: str = "", by: str, by_user=None,
        actor_workspace_ids: set, may_start_turns: bool = False,
        by_agent_itself: bool = False,
        ) -> tuple[AgentTask, AgentTaskAction, list[Turn]]:
    """Do one of the actions to `task`; returns (task, action row, turns).

    `accept`/`backlog` decide a task without starting it (→ todo / backlog);
    `snooze` takes it out of Triage until the date in `comment` (default a
    week); `duplicate` cancels it as a duplicate of the task `comment` names.
    Only approve, nudge and an answer start an agent's turn, and only on a
    task an agent is assigned.

    `may_start_turns` is whether the actor holds the tier that may start an
    agent turn (editor — the QuickTurn gate). It decides only whether a REPLY
    wakes the agent: an approve is the decision a viewer exists to make, and it
    starts the work whoever makes it (as `on_approve` always has); a nudge is
    editor-gated at the route.

    `by_agent_itself` is the task's own agent acting on its own card. It decides
    whether ANSWERING a question wakes the agent: an answer is the response the
    agent explicitly asked for, so anyone allowed to answer (the viewer tier)
    starts the turn that carries it — except the agent answering itself, whose
    turn would only wake itself.

    Atomic, and that is the whole ballgame: `dispatch()` raises on a bad
    `on_approve` spec, and committing the action first would leave the ask
    closed and its work never run — permanently, since approving a closed ask is
    refused. Rolling back leaves it open and retryable, with no action row.

    The task row is re-read under a lock before the ask is checked. Two tabs
    holding the same open card would otherwise both pass the check on their own
    stale copies and dispatch the work twice; locked, the second one waits,
    then sees the ask closed and gets `ClosedAskError`.
    """

    from . import proposals  # cycle: agents.proposals -> agents.services  # noqa: PLC0415

    if action not in _EFFECT:
        raise ValueError(f"action must be one of {'|'.join(_EFFECT)}, got {action!r}")
    comment = (comment or "").strip()
    if action == AgentTaskAction.REPLY and not comment:
        raise ValueError("a reply needs words — comment must not be empty")

    task = (AgentTask.objects.select_for_update(of=("self",))
            .select_related("agent", "project").get(pk=task.pk))
    before = work.snapshot(task)
    name = task.key
    deciding = (AgentTaskAction.APPROVE, AgentTaskAction.DECLINE, AgentTaskAction.ACCEPT,
                AgentTaskAction.BACKLOG, AgentTaskAction.DUPLICATE)
    if action in deciding:
        if task.ask_kind and not task.ask_is_open and action in (AgentTaskAction.APPROVE,
                                                                  AgentTaskAction.DECLINE):
            raise ClosedAskError(f"{name} has no open ask")
        # A plain task with nothing to ask is decidable only while it is live —
        # approving a finished one would quietly re-open it.
        if task.status not in LIVE_STATUSES and (not task.ask_kind or action not in (
                AgentTaskAction.APPROVE, AgentTaskAction.DECLINE)):
            raise ClosedAskError(f"{name} is already {task.status}")
        # ...and approvable only once: approving starts a turn, so approving a
        # plain task that is already in progress would be a nudge any viewer
        # could send. Nudge is the action for that, and it is an editor's.
        if (action == AgentTaskAction.APPROVE and not task.ask_kind
                and task.status == AgentTask.IN_PROGRESS):
            raise ClosedAskError(f"{name} is already in progress — nudge it instead")
    duplicate_of = None
    if action == AgentTaskAction.DUPLICATE:
        duplicate_of = work.task_by_key(comment) if comment else None
        if duplicate_of is None and comment and task.created_by_agent_id:
            duplicate_of = get_task(task.created_by_agent, comment)
        if duplicate_of is None or duplicate_of.workspace_id != task.workspace_id or duplicate_of.pk == task.pk:
            raise ValueError("duplicate needs the key of the task this one duplicates, in the comment")
    snooze_until = _snooze_until(comment) if action == AgentTaskAction.SNOOZE else None
    # A proposed project's decision card: who may start or decline it is narrower
    # than who may act on a card (decision 4(b)) — `proposals.before_act`.
    proposals.before_act(task, action, by_user)
    if action == AgentTaskAction.NUDGE and task.status != AgentTask.IN_PROGRESS:
        # A suggested task is started by approving it; nudging it would start
        # work nobody approved. A finished one has nothing to nudge.
        raise ClosedAskError(f"{name} is {task.status}, not in progress — nothing to nudge")
    if action == AgentTaskAction.NUDGE and task.agent_id is None:
        raise ClosedAskError(f"{name} is not assigned to an agent — there is nobody to nudge")

    closes, status, follow_up = _EFFECT[action]
    if action == AgentTaskAction.REPLY:
        # A reply on a question IS the answer; on anything else it is a note.
        closes = task.ask_kind == AgentTask.ASK_QUESTION and task.ask_is_open
    elif action in (AgentTaskAction.ACCEPT, AgentTaskAction.BACKLOG):
        closes = task.ask_kind == AgentTask.ASK_REVIEW and task.ask_is_open
    if task.agent_id is None:
        # Nobody's queue to put it on, and no agent to start: the action is the
        # whole of it. Approving a person's task moves it to In progress.
        follow_up = False

    row = AgentTaskAction(
        agent=task.agent, task=task, action=action, comment=comment, by=by,
        by_user=by_user if getattr(by_user, "is_authenticated", False) else None,
    )
    turns: list[Turn] = []
    # Answering a question runs `on_approve` when the card has one; without one,
    # the card's own turn carries the answer (below) — the agent asked for it.
    runs_on_approve = action == AgentTaskAction.APPROVE or (action == AgentTaskAction.REPLY and closes)
    starts_card_turn = task.agent_id is not None and (
        action in (AgentTaskAction.APPROVE, AgentTaskAction.NUDGE)
        or (action == AgentTaskAction.REPLY and closes and not by_agent_itself)
        or (action == AgentTaskAction.REPLY and not closes and may_start_turns))
    if runs_on_approve and task.on_approve:
        turns = dispatch(task, action=row, actor_workspace_ids=actor_workspace_ids)
        task.dispatched_at = timezone.now()
        follow_up = False  # the dispatched turn IS the follow-up
    elif starts_card_turn:
        # No spec of its own: the card's own turn. The row is saved first because
        # its pk keys the turn (one enqueue per click; a replay is the same turn).
        # An answer to a question is its own kind of board turn ("ANSWERED BY").
        row.status = AgentTaskAction.APPLIED
        row.applied_at = timezone.now()
        row.save()
        answers = action == AgentTaskAction.REPLY and closes
        turn, _created = enqueue_task_turn(task, action=row, kind="answer" if answers else None)
        turns = [turn]
        task.dispatched_at = timezone.now()
        follow_up = False
    if closes and task.ask_is_open:
        task.ask_closed_at = timezone.now()
        # Answered: nobody is waiting on a person any more.
        task.waiting_on_user = None
    if status:
        task.status = status
    elif runs_on_approve and turns:
        task.status = AgentTask.IN_PROGRESS  # the agent has the ball now
    # Anything that happens to a snoozed task wakes it; a snooze sets it.
    task.snoozed_until = snooze_until
    task.save()
    proposals.after_act(task, action, comment)  # a decision card starts or archives its project

    row.status = AgentTaskAction.PENDING if follow_up else AgentTaskAction.APPLIED
    row.applied_at = None if follow_up else (row.applied_at or timezone.now())
    row.save()
    actor = by_user if getattr(by_user, "is_authenticated", False) else None
    by_agent = task.agent if by_agent_itself else None
    work.record_action(task, row, by_user=actor, by_agent=by_agent)
    work.record_changes(task, before, by_user=actor, by_agent=by_agent)
    if duplicate_of is not None:
        work.add_relation(task, "duplicate", duplicate_of, by_user=actor, by_agent=by_agent)
    return task, row, turns


def pending_actions(agent: Agent):
    """The agent's queue: actions it still has to carry out, oldest first —
    on the tasks it was assigned when each was done."""
    return (agent.task_actions.filter(status=AgentTaskAction.PENDING)
            .select_related("agent", "task", "by_user")
            .order_by("created_at", "id"))


def mark_applied(action_row: AgentTaskAction, result_note: str = "") -> AgentTaskAction:
    action_row.status = AgentTaskAction.APPLIED
    action_row.applied_at = timezone.now()
    if result_note:
        action_row.result_note = result_note
    action_row.save(update_fields=["status", "applied_at", "result_note"])
    return action_row


def next_task_ext_id(agent: Agent) -> str:
    """T1, T2, … from a counter that only goes up — the same rule projects use,
    and for the same reason: a reused id makes an old link point at new work."""
    Agent.objects.filter(pk=agent.pk).update(task_seq=F("task_seq") + 1)
    agent.refresh_from_db(fields=["task_seq"])
    return f"T{agent.task_seq}"


#: Statuses a task is still LIVE in. A done or declined card waits on nobody.
LIVE_STATUSES = list(AgentTask.LIVE_STATUSES)


def waiting_q(prefix: str = ""):
    """"Somebody has to do something" — as ONE predicate.

    Two shapes count, and both are real:
      * an open ASK (a review or question nobody has answered), and
      * a live task PARKED ON A PERSON, which is most of what the fleet's
        boards actually hold — "waiting on Andrea for the numbers" is a wait
        even though nothing is being asked.

    One definition because several consumers read it (the inbox, the waiting
    badge, push, the per-project waiting count), and this codebase has already
    paid for the same predicate written three times. `prefix` (e.g. "tasks__")
    lets a query on a related model — a project annotating its tasks — reuse it.
    """
    p = prefix
    return Q(**{f"{p}status__in": LIVE_STATUSES}) & (
        (~Q(**{f"{p}ask_kind": ""}) & Q(**{f"{p}ask_closed_at__isnull": True}))
        | Q(**{f"{p}waiting_on_user__isnull": False})
    )


def filter_tasks(qs, *, user=None, project: str = "", status: str = "", waiting: str = "",
                 ask: str = "", batch: str = "", parent: str = "", assignee: str = "",
                 created_by: str = "", label: str = "", priority: str = "", milestone: str = "",
                 category: str = "", kind: str = "", due_before=None, due_after=None, q: str = "",
                 snoozed: str = ""):
    """The task list's filters, shared by the per-agent and workspace routes.

    `waiting=me` is what lands in a person's inbox: tasks parked on them, plus
    open asks routed to nobody — an unrouted ask waits on whoever looks at it.
    `assignee` / `created_by`: `me`, an email, `agent:<slug>` (or a bare slug),
    or `none`. `label`, `priority`, `category`, `status` take comma lists.
    `project`: an alias ("P3"), a handle, an address, or `none`. `snoozed=hide`
    leaves out tasks snoozed into the future; `only` keeps just those.
    """
    def _split(value: str) -> list[str]:
        return [v.strip() for v in (value or "").split(",") if v.strip()]

    def _who(value: str, *, agent_field: str, user_field: str) -> Q:
        value = (value or "").strip()
        if value == "none":
            return Q(**{f"{agent_field}__isnull": True, f"{user_field}__isnull": True})
        if value == "me":
            return Q(**{user_field: user}) if getattr(user, "is_authenticated", False) else Q(pk__in=[])
        if "@" in value:
            return Q(**{f"{user_field}__email__iexact": value})
        return Q(**{f"{agent_field}__slug": value.removeprefix("agent:")})

    if project == "none":
        qs = qs.filter(project__isnull=True)
    elif project:
        ws, _, handle = project.rpartition("/")
        pq = Q(project__ext_id__iexact=project) | Q(project__handle__iexact=handle or project)
        if ws:
            pq = Q(project__workspace_id=ws, project__handle__iexact=handle)
        qs = qs.filter(pq)
    if status:
        qs = qs.filter(status__in=_split(status))
    if category:
        cats = set(_split(category))
        qs = qs.filter(status__in=[st for st, c in AgentTask.CATEGORY.items() if c in cats])
    if waiting == "me":
        unrouted_ask = (~Q(ask_kind="") & Q(ask_closed_at__isnull=True)
                        & Q(waiting_on_user__isnull=True))
        mine = Q(waiting_on_user=user) if getattr(user, "is_authenticated", False) else Q(pk__in=[])
        qs = qs.filter(waiting_q()).filter(mine | unrouted_ask)
    if ask == "open":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=True)
    elif ask == "closed":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=False)
    if batch:
        qs = qs.filter(batch_key=batch)
    if parent == "none":
        qs = qs.filter(parent__isnull=True)
    elif parent:
        qs = qs.filter(Q(parent__key__iexact=parent) | Q(parent__ext_id__iexact=parent))
    if assignee:
        qs = qs.filter(_who(assignee, agent_field="agent", user_field="assignee_user"))
    if created_by:
        qs = qs.filter(_who(created_by, agent_field="created_by_agent", user_field="created_by_user"))
    if label:
        for name in _split(label):
            qs = qs.filter(labels__name__iexact=name)
    if priority:
        qs = qs.filter(priority__in=[int(p) for p in _split(priority) if p.isdigit()])
    if milestone == "none":
        qs = qs.filter(milestone__isnull=True)
    elif milestone:
        qs = qs.filter(Q(milestone__name__iexact=milestone) | Q(milestone_id=int(milestone))
                       if milestone.isdigit() else Q(milestone__name__iexact=milestone))
    if kind:
        qs = qs.filter(kind=kind)
    if due_before:
        qs = qs.filter(due__lte=due_before)
    if due_after:
        qs = qs.filter(due__gte=due_after)
    if q:
        text = q.strip()
        qs = qs.filter(Q(title__icontains=text) | Q(key__iexact=text) | Q(next_action__icontains=text)
                       | Q(ext_id__iexact=text))
    if snoozed == "hide":
        qs = qs.filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=timezone.now()))
    elif snoozed == "only":
        qs = qs.filter(snoozed_until__gt=timezone.now())
    if label:
        qs = qs.distinct()
    return qs


# ---- Agent credentials (per-agent secret store, encrypted at rest) ----------
# Spec: docs/superpowers/specs/2026-09-05-agent-credentials-design.md

#: Where a resolved value came from. During migration a secret can exist in BOTH
#: stores and the box silently prefers canopy-web, so the screen must say which
#: is live rather than only that something is set.
SOURCE_CANOPY_WEB = "canopy-web"
#: Stored here as zero-knowledge ciphertext (canopy-web#1550).
SOURCE_CANOPY_WEB_ZK = "canopy-web-zk"
SOURCE_UNSET = "unset"


def set_agent_credentials(agent, values: dict[str, str], *, user=None, minted: bool = False) -> int:
    """Upsert named secrets. NON-CLOBBERING: a ref absent from `values` is left
    alone, so a single-field edit can never wipe the rest (the same rule
    RunnerCredentialIn follows, for the same reason).

    Rejects a blank value rather than storing one: "" is how a UI says "I did not
    type anything", and storing it makes a declared ref read as SET while
    resolving to nothing — the worst of both.

    Zero-knowledge is the canopy-web vault's only mode (Jonathan, 2026-10-17):
    on a zero-knowledge agent — every agent created since, and every agent that
    held nothing (migration 0059) — a value written in plaintext (`canopy vault
    import`, or one canopy-web made itself: `minted`, Connect Google mailbox,
    canopy-pat rotation) is sealed to every reader of the agent (`zks1`,
    `zk_seal.py`) and the plaintext forgotten. Only an older agent that already
    held custodial values is still written custodially until it is converted
    (`manage.py list_custodial_vault_agents` lists them).
    """

    del minted  # every write is sealed now; kept for callers' clarity
    blank = sorted(k for k, v in values.items() if not (v or "").strip())
    if blank:
        raise ValueError(f"blank value for: {', '.join(blank)}")

    by = user if getattr(user, "is_authenticated", False) else None
    if agent.zk_enabled:
        from django.db import transaction

        from .credentials import zk, zk_seal  # cycle: agents.credentials.zk -> agents.services  # noqa: PLC0415

        # Under the agent row lock, like every ZK write: a settle's
        # compare-and-swap (`expect`) must see this write or come after it.
        with transaction.atomic():
            agent = zk._locked(agent)
            sealed = {name.strip(): zk_seal.seal_for_agent(agent, name.strip(), value.strip())
                      for name, value in values.items()}
            for name, value_zk in sealed.items():
                AgentCredential.objects.update_or_create(
                    agent=agent, name=name,
                    defaults={"value_zk": value_zk, "kv": agent.zk_kv, "value_enc": "", "updated_by": by})
        return len(sealed)

    written = 0
    for name, value in values.items():
        AgentCredential.objects.update_or_create(
            agent=agent,
            name=name.strip(),
            defaults={
                "value_enc": AgentCredential.seal(agent, name.strip(), value.strip()),
                "updated_by": user if getattr(user, "is_authenticated", False) else None,
            },
        )
        written += 1
    return written


def delete_agent_credential(agent, name: str) -> int:

    deleted, _ = AgentCredential.objects.filter(agent=agent, name=name).delete()
    return deleted


def agent_credential_status(agent) -> list[dict]:
    """Every ref the agent DECLARES, against what is actually set — booleans and
    timestamps, never values.

    This is the screen that would have caught the 2026-09-05 failure: ACE's
    mailbox had been dead since May and seeing that required SSH-ing to a box and
    running `gog auth list`. A credential that lives only in a vault and a
    keyring is a credential nobody is watching.

    Declared-but-unset rows appear (that is the question the page answers), and
    so do stored-but-undeclared ones — an orphan left behind when a ref was
    removed from its declaration is a live secret nothing accounts for, and hiding
    it is how it stays that way.
    """

    from django.db.models import Count, Max

    stored = {c.name: c for c in AgentCredential.objects.filter(agent=agent).select_related("updated_by")}
    declared = [str(n) for n in (agent.runtime_secrets or []) if str(n).strip()]
    reads = {
        r["name"]: r for r in AgentCredentialRead.objects.filter(agent=agent)
        .values("name").annotate(last=Max("created_at"), n=Count("id"))
    }

    rows: list[dict] = []
    for name in declared + [n for n in sorted(stored) if n not in declared]:
        cred = stored.get(name)
        read = reads.get(name) or {}
        rows.append({
            "name": name,
            "declared": name in declared,
            "set": cred is not None,
            "source": ((SOURCE_CANOPY_WEB_ZK if cred.is_zk else SOURCE_CANOPY_WEB)
                       if cred else SOURCE_UNSET),
            "updated_at": cred.updated_at if cred else None,
            "updated_by_email": (cred.updated_by.email if cred and cred.updated_by_id else None),
            "last_read_at": read.get("last"),
            "read_count": read.get("n", 0),
        })
    return rows


def credential_reads(agent, name: str, *, limit: int = 100) -> list[dict]:
    """The read history of one credential, newest first."""

    qs = (AgentCredentialRead.objects.filter(agent=agent, name=name)
          .select_related("user", "runner").order_by("-created_at")[:limit])
    return [{
        "name": r.name,
        "user_email": r.user.email if r.user_id else "",
        "runner": str(r.runner_id) if r.runner_id else "",
        "runner_name": r.runner.name if r.runner_id else "",
        "via": r.via, "turn_id": r.turn_id, "created_at": r.created_at,
    } for r in qs]


def declared_credential_names(agent) -> set[str]:
    """Every ref the agent declares: `runtime_secrets` (the names, written by
    `canopy agent vault`)
    plus `runtime_sources` (where each lives). The set `?refs=` is scoped to."""
    names = {str(n).strip() for n in (agent.runtime_secrets or []) if str(n).strip()}
    names |= {str(n).strip() for n in (agent.runtime_sources or {}) if str(n).strip()}
    return names


def declare_credential_names(agent, names) -> list[str]:
    """Add `names` to the agent's declared refs (`runtime_secrets`); return the
    ones that were new.

    Called when the agent's owner or an admin stores a value: someone who may
    write the list anyway is not widening anything by naming a secret, and a
    stored-but-undeclared value is one the agent's scoped reads never get —
    the Credentials page showed it "not declared" with no way to fix it there."""
    current = [str(n) for n in (agent.runtime_secrets or [])]
    known = set(current) | {str(n) for n in (agent.runtime_sources or {})}
    added = sorted({str(n).strip() for n in names if str(n).strip()} - known)
    if added:
        agent.runtime_secrets = current + added
        agent.save(update_fields=["runtime_secrets"])
    return added


def resolve_agent_credentials(agent, refs: set[str] | None = None) -> dict[str, str]:
    """PLAINTEXT. The only path that returns values, and only to a runner.

    `refs=None` returns every stored value (the unscoped, original behaviour).
    A set returns only those refs that the agent also DECLARES — a ref the
    caller names but the agent does not declare is never handed out, so a scoped
    caller cannot widen its read by guessing names."""

    qs = AgentCredential.objects.filter(agent=agent).select_related("agent")
    if refs is not None:
        qs = qs.filter(name__in=sorted(set(refs) & declared_credential_names(agent)))
    # A zero-knowledge agent never reaches here (resolve branches to zk.py);
    # plaintext() refuses a zk value outright if one ever does.
    return {c.name: c.plaintext() for c in qs}


def record_credential_reads(agent, names, *, user=None, runner=None, via="", turn_id="",
                            license=None) -> None:
    """One `AgentCredentialRead` row per ref handed out (canopy-web#1538)."""

    AgentCredentialRead.objects.bulk_create([
        AgentCredentialRead(
            agent=agent, name=str(n)[:120],
            user=user if getattr(user, "is_authenticated", False) else None,
            runner=runner, via=via[:16], turn_id=(turn_id or "")[:120], license=license,
        )
        for n in sorted(set(names))
    ])


def reveal_agent_credential(agent, name: str, *, user) -> str | None:
    """PLAINTEXT of ONE stored value, for a person who stepped up — and the
    record that they saw it. None when nothing is stored under `name`.

    The record is written FIRST and its failure propagates: unlike a runner's
    resolve (which must not be starved of secrets by an audit hiccup), a human
    reveal that cannot be recorded does not happen.
    """

    cred = AgentCredential.objects.filter(agent=agent, name=name).select_related("agent").first()
    if cred is None:
        return None
    if cred.is_zk:
        # Nothing to show: canopy-web holds only ciphertext (canopy-web#1550).

        raise ZeroKnowledgeValue(f"{name} is a zero-knowledge value; canopy-web cannot reveal it")

    who = getattr(user, "email", "") or str(getattr(user, "pk", ""))
    # The credential's own read history (the same log /credentials/resolve
    # writes), so Settings → Credentials shows a reveal beside a box's reads.
    AgentCredentialRead.objects.create(
        agent=agent, name=name, user=user if getattr(user, "is_authenticated", False) else None,
        via="reveal",
    )
    events.record(
        [{
            "source": "agents.credentials",
            "kind": "agent.credentials.revealed",
            "level": "warn",
            # Blank key: every reveal is its own row, never coalesced into a count.
            "key": "",
            "summary": f"{who} revealed {agent.slug}'s {name}",
            "payload": {"agent": agent.slug, "name": name, "by": who,
                        "user_id": getattr(user, "pk", None), "via": "browser+passkey"},
        }],
        workspace=agent.workspace,
    )
    return cred.plaintext()


def caller_runs_agent(user, agent) -> bool:
    """True when `user` pairs a live runner that this agent's routing could
    actually send work to.

    Tighter than workspace membership, deliberately: plaintext should reach a box
    that runs the agent, not everyone who can see it. Mirrors the runner
    credential fetch, whose trust boundary is "the caller who can claim turns as
    this runner".
    """

    if role_cap.cap_of(user):
        return False  # acting as a lower role: reads go through its license instead

    # ASSIGNED, not necessarily ENABLED. `enabled=False` removes a runner from
    # the automatic rotation — it stops being a fallback — but it does NOT stop
    # work being directed there: `claim_next_turn` checks `pinned_runner` first
    # and returns before any assignment lookup ("a pin trumps everything below
    # it"). That combination IS the explicit-only runner Jonathan asked for on
    # 2026-09-06: "I don't want it to fire ace commands as a fall back, only if
    # we explicit trigger it."
    #
    # So requiring `enabled=True` here contradicted the claim path: a pinned turn
    # would land on the box, which would then be refused the very secrets it
    # needs to run the agent — a failure at the far end of a long round trip,
    # reading as a broken agent rather than a routing rule.
    #
    # The trust boundary is unchanged in substance: an assignment row at all
    # means this agent's work may be directed at this runner, and the caller must
    # still PAIR it. Disabling governs automatic routing, not trust.
    #
    # AND the owner must be one of the agent's admins (`runner_may_hold_agent`).
    # An assignment row alone is written by the EDITOR tier, which an
    # auto-approved access request hands out — so without this, an editor paired a box, added it to the list
    # (disabled, at the bottom) and read every secret, both vault keys and the
    # owner's GitHub token through `/credentials/resolve`.
    if not agent.is_admin(user):
        return False
    if RunnerAssignment.objects.filter(
        agent=agent, runner__owner=user,
    ).exclude(runner__status=Runner.RETIRED).exists():
        return True
    # An agent with no order of its own follows its workspace's: a box of this
    # user's in that order is one the agent's work is directed at.

    inh = inherited_orders([agent.id]).get(agent.id)
    return bool(inh) and any(r.owner_id == user.pk for r in inh.runners)


def runner_may_hold_agent(runner, agent, *, standings=None) -> bool:
    """May this box run AS this agent — hold its prompt, its caller's token, its
    secrets and its owner's GitHub identity?

    Only when its owner is one of the agent's admins (its owner, a
    workspace owner, or an `AgentAdmin`). A runner speaks with its owner's
    authority, so this is "is the owner trusted with the whole agent?", asked of
    the box. The one predicate behind every place an agent's identity leaves
    canopy: claiming an agent turn, the per-turn GitHub token, credential
    resolve, and the routing writes that point work at a box.

    The agent's OWN canopy login (`Agent.user`) is NOT a holder: pairing a box
    as an agent has been refused since #1049, so a box owned by an agent login
    is a pre-#1049 leftover, and the agent's own login is no admin of it.

    Fails closed on a runner with no owner.

    A sensitive-data box (`is_sensitive_data_box`) is dedicated: it holds only an
    agent with a capability that handles sensitive data, whoever owns it — so no
    other agent's full profile, secrets or GitHub identity ever shares a box with a
    sensitive conversation (spec
    2026-10-22-sensitive-data-agents §1, "A dedicated box").

    `standings` (`access.Standings` holding the runner's owner and the agent)
    answers the admin question from one read, for a caller asking many pairs."""
    if getattr(runner, "owner_id", None) is None:
        return False
    if is_sensitive_data_box(runner) and not sensitive_data.sensitive_capabilities(agent.interface):
        return False
    if standings is not None:
        return standings.is_admin(runner.owner, agent)
    return agent.is_admin(runner.owner)



# ---- 1Password vault + import (spec 2026-09-06) -------------------------------

def set_agent_vault(agent, *, vault=None, service_key=None):
    """Set the agent's vault name and/or its service-account token.

    A change re-asks the agent's cloud boxes to bootstrap it (see below).

    Non-clobbering on the KEY specifically: renaming a vault must not silently
    wipe the credential that reads it, which is the shape of every other
    credential write here."""

    fields = []
    if vault is not None:
        agent.op_vault = vault.strip()
        fields.append("op_vault")
    if service_key and service_key.strip():
        agent.op_sa_token_enc = encrypt_secret(service_key.strip())
        fields.append("op_sa_token_enc")
    if fields:
        agent.save(update_fields=[*fields, "updated_at"])
        # What a box said about the OLD vault/key no longer describes this one: drop the
        # verdict to "not reported" rather than leave a stale False (or True) on the
        # checklist, and ask the cloud boxes that take its turns to re-run bootstrap,
        # which is what produces a fresh one.
        from .setup import request_setup  # cycle: agents.setup -> agents.oauth_api -> agents.services  # noqa: PLC0415

        AgentBootstrapReport.objects.filter(agent=agent).update(env_ok=None)
        request_setup(agent)
    return agent_vault_status(agent)


def agent_vault_status(agent):
    """Vault config plus how much of the agent is actually locatable.

    `locatable` is the number of declared refs that carry a source. It is the
    difference between "the import found nothing" and "this deployment was never
    told where anything lives" — the same failure with two completely different
    fixes."""

    declared = [str(n) for n in (agent.runtime_secrets or []) if str(n).strip()]
    plan = vault_import.plan_import(declared, agent.runtime_sources or {})
    return AgentVaultOut(
        vault=agent.op_vault,
        key_set=bool(agent.op_sa_token_enc),
        declared=len(declared),
        locatable=sum(1 for i in plan if i.kind in ("op", "value")),
    )


def resolve_agent_vault(agent) -> tuple[str, str]:
    """PLAINTEXT vault config, for a runner. Empty token when none is set."""

    token = decrypt_secret(agent.op_sa_token_enc) if agent.op_sa_token_enc else ""
    return agent.op_vault, token


def resolve_shared_vault(agent) -> tuple[str, str]:
    """PLAINTEXT shared-vault config for this agent's TENANT.

    The sibling of resolve_agent_vault one level up. Returns ("", "") when the
    workspace has not been configured, which is what keeps this additive: the
    box falls back to its compiled-in default and behaves exactly as before.

    Deliberately reads the workspace rather than deriving a name, because the
    whole defect this closes was a derived name — "Canopy-Shared" compiled into
    bootstrap_agents.sh, correct for one tenant and silently wrong for the next.
    """

    ws = agent.workspace.shared_vault_source() if agent.workspace else None
    if ws is None:
        return "", ""
    token = decrypt_secret(ws.shared_op_sa_token_enc) if ws.shared_op_sa_token_enc else ""
    return ws.shared_op_vault, token


# ---- what the BOX observed (spec 2026-09-07) ---------------------------------

def record_bootstrap_report(agent, *, runner_name, client_creds_ok, mailbox_ok,
                            gog_client="", detail="", turn_client="",
                            turn_ready=None, env_ok=None):
    """Upsert one box's view of this agent. Latest wins — this is current state,
    not a log: the question it answers is "can this agent run RIGHT NOW", and a
    history of that would bury the answer under every prior boot."""

    fields = {
        "client_creds_ok": bool(client_creds_ok),
        "mailbox_ok": bool(mailbox_ok),
        "gog_client": (gog_client or "").strip(),
        "turn_client": (turn_client or "").strip(),
        "detail": (detail or "").strip()[:2000],
    }
    # The tri-states are written ONLY when the box actually looked. "I did not
    # check" is not an observation and must not overwrite one — the same rule
    # the runner's `projects` follows, for the same reason.
    #
    # They were written as None instead, which erased the last real answer: the
    # updater's credentials-only pass (every 30 minutes) does not run
    # `op inject`, so minutes after a full bootstrap recorded `env_ok` for all
    # five agents, every one of them read "not checked" (labs, 2026-09-24).
    if turn_ready is not None:
        fields["turn_ready"] = bool(turn_ready)
    elif not mailbox_ok:
        # Except that a stored `turn_ready` TRUE cannot outlive a pass that just found no
        # working mailbox: turns read mail through that mailbox, so "turns can read it" is
        # no longer known. A box with no token computes no turn_ready at all, which left
        # lex's row saying true for a day while its mailbox was down and the setup
        # checklist read "done" off it (2026-10-20). A stored false stays: it names why.
        AgentBootstrapReport.objects.filter(
            agent=agent, runner_name=runner_name.strip(), turn_ready=True,
        ).update(turn_ready=None)
    if env_ok is not None:
        fields["env_ok"] = bool(env_ok)

    row, _ = AgentBootstrapReport.objects.update_or_create(
        agent=agent, runner_name=runner_name.strip(), defaults=fields,
    )
    return row


def bootstrap_reports(agent) -> list:
    """Every LIVE box's view of this agent, newest first.

    A retired runner's last report is left out. Rows key on the box's NAME (see
    `AgentBootstrapReport.runner_name`), so retiring a runner never touched them,
    and a box retired on 2026-10-04 (cloud-ec2-test) went on answering "can this
    agent run" for echo/eva/ada — a frozen all-green row from a machine that no
    longer exists, read as a second healthy box by anyone counting rows. A row is
    hidden only when EVERY runner of that name is retired: a report from a name
    with no runner row at all (a laptop that reports before it pairs) still shows,
    and unretiring the runner brings its row back, because nothing is deleted.
    """

    retired = set(
        Runner.objects.filter(status=Runner.RETIRED).values_list("name", flat=True)
    ) - set(
        Runner.objects.exclude(status=Runner.RETIRED).values_list("name", flat=True)
    )
    return list(
        AgentBootstrapReport.objects.filter(agent=agent)
        .exclude(runner_name__in=retired)
        .order_by("-reported_at")
    )
