from django.apps import AppConfig


class McpAppsHostConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp_apps_host"
    label = "mcp_apps_host"
    verbose_name = "MCP Apps hosting"

    def ready(self) -> None:
        # Lapsed View subscriptions (`AppViewSubscription`, 24 h), across every conversation.
        from apps.common import ticks  # noqa: PLC0415 — the clock registers once the app registry is ready

        ticks.register("mcp_apps.subscription_sweep", _sweep_subscriptions, 15 * 60)


def _sweep_subscriptions() -> None:
    from .mcp_apps_views import sweep_subscriptions  # noqa: PLC0415 — the views module loads the gateway and models

    sweep_subscriptions()
