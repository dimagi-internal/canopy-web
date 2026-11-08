from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    label = "contacts"

    def ready(self) -> None:
        # HCP revocation notifications still owed to a client's webhook (HCP 4.2.3).
        from apps.common import ticks

        ticks.register("contacts.hcp_oauth_deliver", _hcp_deliver, 60)
        # A person's own record of deleting one of their sessions' words.
        from . import session_reviewers  # noqa: F401, PLC0415 — ready(): connects its receiver


def _hcp_deliver() -> None:
    from .hcp_webhooks import maybe_deliver

    maybe_deliver()
