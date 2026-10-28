from django.apps import AppConfig


def _is_tool_route(method: str, path: str) -> bool:
    from .api_tools import is_tool_route  # noqa: PLC0415 — api_tools loads fastmcp and the API schema; only a caller-token REST request pays for it

    return is_tool_route(method, path)


def _log_act_as_call(request, grant, status: int) -> None:
    from .audit import log_act_as_rest_call  # noqa: PLC0415 — as _is_tool_route: only an act-as REST request pays for it

    log_act_as_rest_call(request, grant, status)


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp"
    label = "mcp"

    def ready(self):
        # REST takes a caller token only where this server would (canopy-web#1782).
        # Registered rather than imported: `tokens` sits a layer below `mcp`.
        from apps.tokens import middleware  # noqa: PLC0415 — AppConfig.ready() wiring, after the app registry is loaded

        middleware.caller_token_routes = _is_tool_route
        # Every REST request an act-as-you token makes is a call-log row (this app's table).
        middleware.act_as_call_log = _log_act_as_call
