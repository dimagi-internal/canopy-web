from django.apps import AppConfig


class HostGrantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.host_grants"
    label = "host_grants"
    verbose_name = "Host grants and canopy as a host"

    def ready(self):
        # A sensitive conversation keeps a host's resource links only under the schemes
        # its site declares, and only from the site's own tools; `harness` (core) asks
        # through these registries rather than importing a feature app.
        from apps.harness import sensitivity  # noqa: PLC0415 — the app registry is ready only now

        from .site_needs import is_site_tool, resource_schemes_for  # noqa: PLC0415 — as above

        sensitivity.register_resource_schemes(resource_schemes_for)
        sensitivity.register_site_tools(is_site_tool)
