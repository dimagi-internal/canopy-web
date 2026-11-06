from django.apps import AppConfig


class EmbeddingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.embedding"
    label = "embedding"
    verbose_name = "Host sites and embedding"

    def ready(self):
        from corsheaders.signals import check_request_enabled

        from .cors import host_site_origin

        # The dispatch_uid predates the move; it names the hook, not the app.
        check_request_enabled.connect(host_site_origin, dispatch_uid="tokens.host_site_origin")
