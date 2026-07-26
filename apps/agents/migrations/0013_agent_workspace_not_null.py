"""Home every unhomed agent, then constrain `Agent.workspace` NOT NULL.

WHY THIS EXISTS
---------------
`Agent.workspace` was added nullable in 0006 purely so the `AddField` needed no
default on existing rows; 0007 immediately homed every existing agent. The
nullability was never reverted, and in the years since, SIX independent tenancy
predicates grew a `workspace_id IS NULL` leg meaning "allow" — because a
nullable tenant FK invites `if agent.workspace_id and <membership check>`, which
short-circuits to *ungated* on precisely the row that declares no tenant. Four
were fixed one site at a time (PRs #378, #421, #423). This migration removes the
state those legs existed to handle, so the remaining two (`claim_next_turn` and
`_runner_schedule_qs`) can be deleted in the same change and the class cannot
recur.

WHERE UNHOMED AGENTS GO (derived from the data, not invented)
-------------------------------------------------------------
Production has zero unhomed agents (0007 homed every pre-existing row, and the
only creation path, `apps.agents.api.upsert_agent`, always homes a new one), so
this is a no-op there. It must still be *correct* for a dev box, a staging DB,
or a restored snapshot where a row was created straight through the ORM. The
target is resolved in this order, most-evidenced first:

1. **The modal workspace among already-homed agents** — but ONLY when that
   workspace is a clear plurality, not a coin flip. This is the tenant this
   deployment's agents demonstrably live in; an unhomed row is, by
   construction, one that either predates 0007's reach or was hand-created
   afterward by a non-API path, and putting it where its siblings already are
   is the answer the data gives — a *narrowing*, since an unhomed agent is
   today visible to every authenticated caller through the fail-open legs, and
   afterwards only to that workspace's members. If two or more workspaces tie
   for the most already-homed agents, there is no data-derived answer at
   all — picking one anyway would be exactly the silent, unlogged tenancy
   decision this migration exists to end, so it RAISES instead, naming every
   tied workspace and its count.
2. **The sole workspace, if the deployment has exactly one.** Covers a dev or
   test DB whose single tenant is not named `dimagi` and which has no homed
   agent to be modal about. There is no other candidate, so there is no choice
   to get wrong — this path stays automatic even though step 1 now raises on
   ambiguity, because "only one workspace exists" is certainty, not plurality.
3. **The default `dimagi` workspace** (created exactly as 0007 creates it, owned
   by the first superuser else the first user). Only reached when there are
   several workspaces and not one homed agent — i.e. no evidence at all. This is
   not a fresh invention: it is the same target 0007 already picked for every
   agent in this deployment's history, so it is the established default rather
   than a new one.
4. **No users at all** → raise. A `Workspace` requires a `created_by` user, so
   there is genuinely nothing to home to; failing loudly with an actionable
   message beats an opaque NOT NULL violation two lines later. (Unreachable in
   practice: creating an agent requires an authenticated user.)

Deliberately NOT done: granting anyone membership of the target workspace. 0007
did that because it was making a pre-tenancy world tenanted for the first time
and nobody would otherwise have retained access. Here the target workspace
already exists with its own members; adding more would be a privilege
escalation dressed as a data fix.

VISIBILITY
----------
The claimed no-op is only trustworthy if a violation of it is loud. Every row
this migration homes is logged (agent slug + destination workspace) through
the `apps` logger at WARNING — the level `config/settings/base.py` keeps on by
default in every environment, deploy logs included — and finding zero unhomed
agents is logged too, so "nothing happened" is an observed fact in the log,
not an assumption about what didn't print.

REVERSIBILITY
-------------
Reversible. The `AlterField` reverse restores `null=True`; the data step's
reverse is a no-op, because "un-home the agents we homed" is neither
recoverable (we do not record which rows we touched) nor desirable (a row with
a tenant is strictly safer than one without). Reversing therefore leaves the
column nullable and every agent still homed — exactly the state a re-run of
this migration would find, so forward/back/forward is idempotent.
"""
import logging

from django.conf import settings
from django.db import migrations, models
from django.db.models import Count

logger = logging.getLogger(__name__)

# Hardcoded, not imported from apps.workspaces.services: a migration must keep
# describing the schema as it was, and 0007 hardcodes the same literal.
DEFAULT_WORKSPACE_SLUG = "dimagi"
DEFAULT_WORKSPACE_NAME = "Dimagi"


def _resolve_target(apps):
    """The workspace slug to home unhomed agents into. See the module docstring
    for why each step is the answer the data gives."""
    Agent = apps.get_model("agents", "Agent")
    Workspace = apps.get_model("workspaces", "Workspace")

    # 1. Modal workspace among already-homed agents — but only if it is an
    #    unambiguous plurality. A tie means the data does not actually pick a
    #    winner, so raise and say what tied rather than silently favoring
    #    whichever slug sorts first.
    counts = list(
        Agent.objects.filter(workspace__isnull=False)
        .values("workspace_id")
        .annotate(n=Count("pk"))
        .order_by("-n", "workspace_id")
    )
    if counts:
        top_n = counts[0]["n"]
        tied = sorted(c["workspace_id"] for c in counts if c["n"] == top_n)
        if len(tied) > 1:
            raise RuntimeError(
                "agents.0013 cannot resolve a home for unhomed agents: "
                f"{len(tied)} workspaces are tied at {top_n} already-homed "
                f"agent(s) each ({', '.join(tied)}) — there is no data-derived "
                "answer, and picking one anyway would be exactly the silent "
                "tenancy decision this migration exists to end. Home the "
                "tied workspaces' agents (or the unhomed ones) by hand to "
                "break the tie, then re-run the migration."
            )
        return tied[0]

    # 2. The only workspace there is.
    slugs = list(Workspace.objects.order_by("slug").values_list("slug", flat=True)[:2])
    if len(slugs) == 1:
        return slugs[0]

    # 3. The default workspace, created the same way 0007 creates it.
    existing = Workspace.objects.filter(slug=DEFAULT_WORKSPACE_SLUG).first()
    if existing is not None:
        return existing.slug
    user_app, user_model = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(user_app, user_model)
    owner = (
        User.objects.filter(is_superuser=True).order_by("id").first()
        or User.objects.order_by("id").first()
    )
    if owner is None:
        # 4. Nothing to home to, and a Workspace cannot be created without a user.
        raise RuntimeError(
            "agents.0013 cannot make Agent.workspace NOT NULL: this database has "
            "agents with no workspace, no workspace to home them in, and no user "
            "to own a new workspace. Create a user and a workspace (or delete the "
            "orphaned agents) and re-run the migration."
        )
    raw = getattr(settings, "AUTH_ALLOWED_EMAIL_DOMAIN", "") or ""
    ws = Workspace.objects.create(
        slug=DEFAULT_WORKSPACE_SLUG,
        display_name=DEFAULT_WORKSPACE_NAME,
        created_by=owner,
        auto_join_domains=[d.strip().lower() for d in raw.split(",") if d.strip()],
    )
    return ws.slug


def home_unhomed_agents(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    slugs = list(Agent.objects.filter(workspace__isnull=True).values_list("slug", flat=True))
    if not slugs:
        # The production path: 0007 already homed everything. Logged so "no
        # unhomed agents" is an observed fact in the deploy log, not an
        # assumption about a migration that printed nothing.
        logger.warning("agents.0013: found no unhomed agents; workspace backfill is a no-op.")
        return
    target = _resolve_target(apps)
    for slug in slugs:
        logger.warning(
            "agents.0013: homing unhomed agent %r into workspace %r", slug, target,
        )
    Agent.objects.filter(slug__in=slugs).update(workspace_id=target)


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0012_agent_runner_preference"),
        ("workspaces", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(home_unhomed_agents, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="agent",
            name="workspace",
            field=models.ForeignKey(
                help_text=(
                    "The tenant that owns this agent. NOT NULL: an agent always "
                    "has exactly one tenant, so no tenancy predicate anywhere may "
                    "treat NULL as 'allow'."
                ),
                on_delete=models.deletion.PROTECT,
                related_name="agents",
                to="workspaces.workspace",
            ),
        ),
    ]
