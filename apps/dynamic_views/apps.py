from django.apps import AppConfig


class DynamicViewsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.dynamic_views"

    def ready(self) -> None:
        from apps.connections import declarations  # noqa: PLC0415 — the registry of what a screen is

        declarations.register_surface("view", _connection_surface)
        declarations.register_share_target("project", _connection_share_target)


def _connection_surface(request, ref):
    from .api import connection_surface  # noqa: PLC0415 — models: the app registry is not ready at module load

    return connection_surface(request, ref)


def _connection_share_target(ref):
    from .api import connection_share_target  # noqa: PLC0415 — models: the app registry is not ready at module load

    return connection_share_target(ref)
