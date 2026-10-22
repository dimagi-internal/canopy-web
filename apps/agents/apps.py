from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agents"

    def ready(self) -> None:
        # Vault-key holders are told an approved device or new admin waits for a
        # zero-knowledge agent's key; the canopy-pat rotation and stale-proposal
        # expiry run on the same off-request clock.
        from apps.common import ticks

        from .credentials import pat_rotation

        ticks.register("agents.zk_notify", _zk_notify, 60)
        ticks.register("agents.pat_rotation", _pat_rotation, pat_rotation.interval_s, dedupe=False)


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
