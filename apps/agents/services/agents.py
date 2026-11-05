from __future__ import annotations

from django.conf import settings
from django.db import transaction

from apps.harness.models import Turn, agent_turns_q
from apps.tokens.models import PersonalToken
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from ..definition import definition_key, siblings
from ..models import Agent, AgentSkill, AgentSync
from ._base import _aware
from .projects import task_read_q
from .agent_credentials import set_agent_credentials

# ---- agents ----
#: What an agent IS on its boxes: the code they clone and what they install with
#: it. Changing one on an existing agent is its admins' (agents/api.py upsert).
DEFINITION_FIELDS = ("repo_url", "repo_ref", "runtime_secrets",
                     "runtime_sources", "clis", "cli_models")


def _without_op_refs(sources: dict) -> dict:
    """`runtime_sources` as stored: an older plugin still publishes `{"op": "op://..."}`
    per ref; that pointer leads nowhere now, so only the rest of the entry is kept."""
    return {name: ({k: v for k, v in src.items() if k != "op"} if isinstance(src, dict) else src)
            for name, src in sources.items()}


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
    for field in ("repo_url", "repo_ref", "runtime_secrets",
                  "runtime_sources", "runner_preference", "clis", "cli_models"):
        value = getattr(data, field, None)
        if value is not None:
            defaults[field] = _without_op_refs(value) if field == "runtime_sources" else value
    # Same rule for the Google account: the plugin's re-upserts omit it.
    google_account = getattr(data, "google_account", None)
    if google_account is not None:
        defaults["google_account"] = google_account.strip().lower()
    agent, _ = Agent.objects.update_or_create(
        slug=data.slug, defaults=defaults, create_defaults={**defaults, "workspace": workspace, "owner": owner}
    )
    new_email = (agent.email or "").strip().lower()
    if agent.user_id is not None and new_email and (agent.user.email or "").lower() != new_email:
        # Its login is the account at its email: a NEW email unlinks the old account
        # (the API view derives the new link, `agent_login.rederive_after_email_change`).
        # A blank one does not: a re-upsert that omits the email must not cut an agent
        # off from its login.
        from ..agent_login import unlink  # noqa: PLC0415 — cycle: agent_login imports services

        unlink(agent)
    return agent


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
    # An agent's login is ALWAYS the account at its own email, never any other.
    if agent.email and (linked.email or "").lower() != agent.email.strip().lower():
        raise AgentUserLinkError(f"{agent.slug}'s canopy login is the account at its own email "
                                 f"({agent.email}), not {linked.email}")
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


def agent_token_ttl_days() -> int:
    """How long an agent's own canopy token lives: the PAT default (180 days)."""
    return int(getattr(settings, "PAT_DEFAULT_TTL_DAYS", 180))


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

    It expires like any PAT (`settings.PAT_DEFAULT_TTL_DAYS`): a login that signs in as
    an account must not outlive everyone's attention. Automatic rotation
    (`credentials/pat_rotation.py`) replaces it well before then."""

    from ..agent_login import (  # cycle: agents.agent_login -> agents.services  # noqa: PLC0415
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
        user=user, label=(label or f"{agent.slug} agent").strip()[:100], ttl_days=agent_token_ttl_days())
    return raw, token, user, created


def give_agent_canopy_login(agent: Agent, actor, *, email: str = "") -> dict:
    """The agent's canopy login in one step: mint
    its token (`mint_agent_canopy_token`), store it as its login (`pat_rotation.CANOPY_PAT_REF`) sealed to whoever
    may read the agent (`zks1`), and turn on automatic rotation. The raw
    token never leaves this function — the browser button that calls it has no use for
    it, and a response that carried it would put a live login in a page's memory.

    Refuses BEFORE minting when nobody could read the stored token yet (no vault key,
    no approved machine): a token minted and then not storable would be a live login
    with nowhere to go."""
    from django.db import transaction

    from ..credentials import (  # cycle: agents.credentials.pat_rotation -> agents.services  # noqa: PLC0415
        pat_rotation,
        zk_seal,
    )

    if not zk_seal.recipients_for(agent):
        raise AgentTokenRefused(
            "nobody can open this agent's secrets yet — set up your key under Settings → "
            "Security → You (one passkey touch), then try again", 409)
    with transaction.atomic():
        raw, token, user, created = mint_agent_canopy_token(
            agent, actor, email=email, label=f"{agent.slug} agent")
        set_agent_credentials(agent, {pat_rotation.CANOPY_PAT_REF: raw}, user=actor)
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
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
        "sync_count": agent.syncs.count(),
        "skill_count": agent.skills.count(),
        # A figure on a card every member reads: what anyone may read.
        "task_count": agent.tasks.filter(task_read_q(None)).count(),
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
