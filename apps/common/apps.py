from django.apps import AppConfig

from apps.common import pg_datetime


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"

    def ready(self) -> None:
        pg_datetime.install()
