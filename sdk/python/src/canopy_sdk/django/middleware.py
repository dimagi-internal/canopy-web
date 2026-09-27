"""``request.canopy_page_token`` for every view, computed only when read."""
from __future__ import annotations

from django.utils.functional import SimpleLazyObject

from .pages import page_token


class CanopyPageTokenMiddleware:
    """Add to ``MIDDLEWARE`` to expose ``request.canopy_page_token`` (a lazy
    string: "" for a route not in ``PAGE_SCOPES``). Runs in ``process_view``,
    after URL resolution, because the token names the resolved route."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.canopy_page_token = SimpleLazyObject(lambda: page_token(request))
        return None
