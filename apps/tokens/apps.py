from django.apps import AppConfig


class TokensConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tokens"
    label = "tokens"

    def ready(self):
        from corsheaders.signals import check_request_enabled

        from .cors import connected_site_origin

        check_request_enabled.connect(connected_site_origin, dispatch_uid="tokens.connected_site_origin")
