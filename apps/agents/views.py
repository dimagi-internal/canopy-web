"""Bare views of the agents app."""
from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.api.errors import problem_response

#: `GET|PUT|DELETE /interface`, `POST /interface/validate`, `PUT /interface/capabilities/{name}/sensitive`.
RETIRED_INTERFACE_PATH = r"^api/agents/(?P<slug>[^/]+)/interface(?:/validate|/capabilities/[^/]+/sensitive)?/?$"


# csrf_exempt: a stale caller gets this answer, not a CSRF 403. It reads nothing and writes nothing.
@csrf_exempt
def retired_interface_route(request: HttpRequest, slug: str) -> HttpResponse:
    """410 for the agent `interface` routes, whatever the method and whoever asks."""
    base = f"/api/agents/{slug}"
    return problem_response(
        410,
        detail=("This route is retired, and nothing was read or saved. Use read-mode and connected-sites "
                "instead. To check a change without saving it, send dry_run: true on a PUT to either. "
                "To say a site handles sensitive data, set the field sensitive on its connected site."),
        read_mode=f"{base}/read-mode",
        connected_sites=f"{base}/connected-sites",
        check_without_saving="dry_run",
        handles_sensitive="sensitive",
    )
