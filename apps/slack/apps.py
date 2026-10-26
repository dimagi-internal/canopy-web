from django.apps import AppConfig


class SlackConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.slack"
    label = "slack"

    def ready(self) -> None:
        # Posts agent replies back into the Slack thread (apps/slack/relay.py).
        from apps.slack import signals  # noqa: F401

        # Moving an agent revokes and re-homes its channel ingests (agent_move.py).
        # Registered from here, as apps/mail does, so the move needs no Slack import.
        from apps.agents import move

        from .agent_move import participant

        move.register(participant)
        # Approved channel reads, on the off-request clock (ingest_scheduler.py).
        from . import ingest_scheduler

        ingest_scheduler.register()

        # Slack-bound sessions' people follow their channel's (apps/slack/audience.py).
        from apps.common import ticks  # noqa: PLC0415 — app registry: only once apps are ready

        from . import audience  # noqa: PLC0415 — app registry: only once apps are ready

        ticks.register("slack.audience_sweep", audience.sweep, audience.SWEEP_EVERY_S)

        # Every session row says which Slack thread it is bound to, read from
        # session metadata only (no Slack call) — registered here so
        # canopy_sessions needs no Slack import.
        from apps.canopy_sessions import channel_bindings

        from .share import binding_of

        channel_bindings.register("slack", binding_of)
