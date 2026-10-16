"""The retired review page's address: ``/w/<ws>/review/<id>/``.

A narrative review is now read and decided on a storyboard, but review links
already went out (in emails, Slack, chat), so this address forwards rather
than dies. A bare Django view — served before the SPA catch-all — so a plain
HTTP client (a mail scanner, curl) follows the same 302 a browser does.

- a narrative review, with the review's valid ``?t=`` token → 302 to its board
  WITH the board's share token; as a member of its workspace → 302 without one;
- signed out with no valid token → sign in, then back here;
- anyone else → 404 (existence is not leaked, and no board token is handed to
  someone who did not hold the review's);
- a review with no narrative (a findings review) → the SPA, whose page stays.
"""
from __future__ import annotations

from urllib.parse import urlencode
from uuid import UUID

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound, HttpResponseRedirect

from apps.common.script_prefix import self_full_path
from apps.reviews.models import ReviewRequest


def review_page(request: HttpRequest, ws: str, rid: str) -> HttpResponse:
    from apps.reviews.api import destination_for
    from config.views import spa_view

    try:
        review = ReviewRequest.objects.filter(pk=UUID(rid), workspace_id=ws).first()
    except ValueError:
        review = None
    if review is None:
        return HttpResponseNotFound("Not found", content_type="text/plain")
    verdict, url = destination_for(request, review)
    if verdict == "go":
        return HttpResponseRedirect(request.META.get("SCRIPT_NAME", "") + url)
    if verdict == "page":
        return spa_view(request)
    if verdict == "login":
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?{urlencode({'next': self_full_path(request)})}")
    return HttpResponseNotFound("Not found", content_type="text/plain")
