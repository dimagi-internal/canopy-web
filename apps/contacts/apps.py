from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    label = "contacts"

    def ready(self) -> None:
        # HCP revocation notifications still owed to a client's webhook (HCP 4.2.3).
        from apps.common import ticks

        ticks.register("contacts.hcp_oauth_deliver", _hcp_deliver, 60)


def _hcp_deliver() -> None:
    from .hcp_oauth import maybe_deliver

    maybe_deliver()
