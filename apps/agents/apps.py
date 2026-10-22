from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agents"

    def ready(self) -> None:
        # Vault-key holders are told an approved device or new admin waits for a
        # zero-knowledge agent's key; the canopy-pat rotation and stale-proposal
        # expiry run on the same off-request clock.
        from django.db.models.signals import post_save  # noqa: PLC0415 — ready(): kept beside the connect it serves

        from apps.common import ticks

        from .credentials import pat_rotation
        from .models import Agent  # noqa: PLC0415 — ready(): models load only after the app registry

        # Every agent has its own vault from the moment it exists, linked to the
        # agent (credentials.vault_members) — made here so no read path writes.
        post_save.connect(_make_vault, sender=Agent, dispatch_uid="agents.make_vault")

        ticks.register("agents.zk_notify", _zk_notify, 60)
        ticks.register("agents.vault_reconcile", _vault_reconcile, 60)
        ticks.register("agents.workspace_vault_reconcile", _workspace_vault_reconcile, 60)
        ticks.register("agents.pat_rotation", _pat_rotation, pat_rotation.interval_s, dedupe=False)
        # A knowledge base delta waiting to be indexed (`kb_index`): the clock only kicks the
        # builder thread, and picks up a version whose builder died with its task.
        ticks.register("agents.kb_index", _kb_index, 30)


def _make_vault(sender, instance, created, raw=False, **kwargs) -> None:
    if created and not raw:
        from .credentials import vault_members  # noqa: PLC0415 — a signal handler: imported once the app is ready

        vault_members.vault_for_agent(instance)


def _vault_reconcile() -> None:
    """Key copies follow membership: a person who stopped being a member of an
    agent's vault (or whose machine's person did) loses them within a minute,
    whatever changed it — a role, a license, a link."""
    from .credentials import vault_members  # noqa: PLC0415 — a tick: runs once the app is ready

    vault_members.reconcile_all()


def _workspace_vault_reconcile() -> None:
    """A workspace vault's key copies follow its membership, within a minute."""
    from .credentials import workspace_vaults  # noqa: PLC0415 — a tick: runs once the app is ready

    workspace_vaults.reconcile_all()


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


def _kb_index() -> None:
    from . import kb_index  # noqa: PLC0415 — a tick: imported once the app is ready

    kb_index.tick()
