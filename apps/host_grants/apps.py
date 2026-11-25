from django.apps import AppConfig


class HostGrantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.host_grants"
    label = "host_grants"
    verbose_name = "Host grants and canopy as a host"

    def ready(self):
        # A sensitive conversation keeps a host's resource links only under the schemes
        # its site declares, and only from the site's own tools; `harness` (core) asks
        # through these registries rather than importing a feature app.
        from apps.harness import sensitivity  # noqa: PLC0415 — the app registry is ready only now

        from .site_needs import is_site_tool, resource_schemes_for  # noqa: PLC0415 — as above

        sensitivity.register_resource_schemes(resource_schemes_for)
        sensitivity.register_site_tools(is_site_tool)
        _connect_reply_push()


def _connect_reply_push() -> None:
    """Reply push to relay sites (`reply_relay.py`): the ledger's and the turn's end
    signals, the off-request clock's retries, and a person's deletion of a
    conversation's words."""
    from django.dispatch import receiver  # noqa: PLC0415 — the app registry is ready only now

    from apps.common import ticks  # noqa: PLC0415 — as above
    from apps.harness.signals import (  # noqa: PLC0415 — as above
        session_content_deleted,
        session_turn_finished,
        turn_events_appended,
    )

    from . import reply_relay  # noqa: PLC0415 — as above

    @receiver(turn_events_appended, dispatch_uid="host_reply_stream", weak=False)
    def _rows(sender, turn, rows, **kwargs):
        try:
            reply_relay.on_rows(turn, rows)
        except Exception:  # noqa: BLE001 — never break the runner's append over a host's webhook
            reply_relay.logger.exception("host reply push failed")

    @receiver(session_turn_finished, dispatch_uid="host_reply_finished", weak=False)
    def _finished(sender, turn, **kwargs):
        from django.db import transaction  # noqa: PLC0415 — as above

        turn_id = turn.pk

        def _after():
            try:
                from apps.harness.models import Turn  # noqa: PLC0415 — as above

                row = Turn.objects.filter(pk=turn_id).first()
                if row is not None:
                    reply_relay.on_finished(row)
            except Exception:  # noqa: BLE001 — never break a turn's close over a host's webhook
                reply_relay.logger.exception("host reply push failed")

        # After the close commits, so the status line (written after the close by a
        # rule's rejection) is in the ledger and the row a thread reads is there.
        transaction.on_commit(_after)

    @receiver(session_content_deleted, dispatch_uid="host_reply_forget", weak=False)
    def _forget(sender, session, turn_ids, **kwargs):
        reply_relay.forget_session(turn_ids)

    ticks.register("host_grants.reply_deliver", reply_relay.maybe_deliver, reply_relay.TICK_SECONDS)
