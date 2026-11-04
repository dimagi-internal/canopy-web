from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agents"

    def ready(self) -> None:
        from . import departure  # noqa: PLC0415 — ready(): the registry loads after the app registry

        departure.register()
        from apps.events import audit  # noqa: PLC0415 — ready(): wiring after the app registry is loaded

        audit.register_workspace_resolver("agents", _audit_workspace)

        # Vault-key holders are told an approved device or new admin waits for a
        # zero-knowledge agent's key; the canopy-pat rotation and stale-proposal
        # expiry run on the same off-request clock.
        from django.db.models.signals import post_save  # noqa: PLC0415 — ready(): kept beside the connect it serves

        from apps.common import ticks
        from apps.workspaces.models import Workspace  # noqa: PLC0415 — ready(): models load only after the app registry

        from . import signals  # noqa: F401,PLC0415 — ready(): connects the task-collection page invalidation
        from .credentials import pat_rotation
        from .models import Agent  # noqa: PLC0415 — ready(): models load only after the app registry

        # Every agent has its own vault from the moment it exists, linked to the
        # agent (credentials.vault_members) — made here so no read path writes.
        post_save.connect(_make_vault, sender=Agent, dispatch_uid="agents.make_vault")

        # A project is never shared across organisations: moving a workspace under
        # another parent ends the shares that would now straddle two trees.
        post_save.connect(_prune_shares, sender=Workspace, dispatch_uid="agents.prune_shares")

        # A new root workspace starts with the built-in roles (read only, read and write, editor).
        post_save.connect(_seed_roles, sender=Workspace, dispatch_uid="agents.seed_roles")

        ticks.register("agents.zk_notify", _zk_notify, 60)
        ticks.register("agents.pat_rotation", _pat_rotation, pat_rotation.interval_s, dedupe=False)
        ticks.register("agents.skill_catalog", _skill_catalog, 5 * 60)


def _make_vault(sender, instance, created, raw=False, **kwargs) -> None:
    if created and not raw:
        from .credentials import vault_members  # noqa: PLC0415 — a signal handler: imported once the app is ready

        vault_members.vault_for_agent(instance)


def _seed_roles(sender, instance, created, raw=False, **kwargs) -> None:
    if created and not raw and not instance.parent_id:
        from . import rolecatalog  # noqa: PLC0415 — a signal handler: imported once the app is ready

        rolecatalog.ensure_seeded(instance.pk)


def _prune_shares(sender, instance, created, raw=False, **kwargs) -> None:
    if not created and not raw:
        from . import services  # noqa: PLC0415 — a signal handler: imported once the app is ready

        services.prune_cross_tree_shares(instance)


def _zk_notify() -> None:
    from .credentials import zk_notify

    zk_notify.maybe_sweep()


def _pat_rotation() -> None:
    """`pat_rotation.tick_if_due` (its own cache lock), then stale-proposal expiry
    — guarded on its own: a failed rotation must not keep stale proposals open."""
    import logging

    from . import proposals
    from .credentials import pat_rotation

    try:
        pat_rotation.tick_if_due(pat_rotation.interval_s())
    finally:
        try:
            proposals.expire_stale()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("proposed-project expiry failed")


def _audit_workspace(request, path_args):
    """The tenant of a setting changed at `/api/agents/{slug}…` (`events.audit`)."""
    if not (request.path.startswith("/api/agents/") and "slug" in path_args):
        return None
    from .models import Agent  # noqa: PLC0415 — models load only after the app registry

    qs = Agent.objects.filter(slug=path_args["slug"])
    pinned = getattr(request, "workspace_slug", None)
    if pinned:
        qs = qs.filter(workspace__slug=pinned)
    agent = qs.select_related("workspace").first()
    return agent.workspace if agent is not None else None


def _skill_catalog() -> None:
    from . import skill_catalog  # noqa: PLC0415 — a tick job: imported when it runs, once the app registry is ready

    skill_catalog.refresh_all()
