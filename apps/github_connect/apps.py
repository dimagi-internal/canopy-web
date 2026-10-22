from django.apps import AppConfig


class GithubConnectConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.github_connect"
    label = "github_connect"
    verbose_name = "GitHub connection"
