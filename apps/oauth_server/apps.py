from django.apps import AppConfig


class OauthServerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.oauth_server"
    label = "oauth_server"
    verbose_name = "MCP sign-in (OAuth)"
