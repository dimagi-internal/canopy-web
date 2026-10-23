from django.apps import AppConfig


def _is_tool_route(method: str, path: str) -> bool:
    from .api_tools import is_tool_route  # noqa: PLC0415 — api_tools loads fastmcp and the API schema; only a caller-token REST request pays for it

    return is_tool_route(method, path)


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp"
    label = "mcp"

    def ready(self):
        # REST takes a caller token only where this server would (canopy-web#1782).
        # Registered rather than imported: `tokens` sits a layer below `mcp`.
        from apps.tokens import middleware  # noqa: PLC0415 — AppConfig.ready() wiring, after the app registry is loaded

        middleware.caller_token_routes = _is_tool_route
