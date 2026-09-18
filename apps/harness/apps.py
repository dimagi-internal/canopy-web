from django.apps import AppConfig


class HarnessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.harness"
    verbose_name = "Agent execution harness"

    def ready(self) -> None:
        # Importing is what CONNECTS the page-invalidation receiver. A signals
        # module nothing imports is dead code that looks alive — how
        # `apps/mcp/page_tools.py` shipped with ten passing tests and no
        # feature. `tests/test_item_invalidation` asserts against the connected
        # receiver, not the function, so this line cannot go silently.
        from . import signals  # noqa: F401
