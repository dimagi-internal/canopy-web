"""`/api/a2a/…` — A2A Agent Card discovery. Discovery only; see agent_card.py.

Public in the login middleware (`/api/a2a/` in PUBLIC_PATH_PREFIXES), because
an A2A client fetches a card before it holds any credential. Each route
self-enforces: the public card exists only for an agent that offers outsiders
something, and the extended card requires `session_auth` (a canopy session or
a PAT — never a contact token, which leaves the request anonymous).
"""
from __future__ import annotations

from django.http import Http404, HttpRequest, HttpResponse
from ninja import Router

from apps.api.auth import session_auth

from . import agent_card
from .models import Agent
from .schemas import A2AAgentCardOut

router = Router(tags=["a2a"])

# Cards change when an owner republishes an interface, which is rare; five
# minutes bounds how long a card can advertise a capability after it is
# withdrawn (and advertising grants nothing — every turn is authorized when it
# is enqueued). The extended card is per-person, so `private` + `Vary`.
PUBLIC_CACHE = "public, max-age=300"
EXTENDED_CACHE = "private, max-age=60"


def _agent_or_404(slug: str) -> Agent:
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None:
        raise Http404("No such agent card.")
    return agent


def _respond(request: HttpRequest, response: HttpResponse, card: A2AAgentCardOut,
             cache_control: str, vary: str | None = None):
    tag = agent_card.etag(card)
    headers = {"ETag": tag, "Cache-Control": cache_control}
    if vary:
        headers["Vary"] = vary
    if tag in [t.strip() for t in request.headers.get("If-None-Match", "").split(",")]:
        not_modified = HttpResponse(status=304)
        for k, v in headers.items():
            not_modified[k] = v
        return not_modified
    for k, v in headers.items():
        response[k] = v
    return card


# The missing-card 404 is deliberately identical for "no such slug" and "this
# agent offers outsiders nothing", so a private agent's existence never leaks.
@router.get("/agents/{slug}/.well-known/agent-card.json",
            response={200: A2AAgentCardOut, 304: None},
            auth=None, by_alias=True, exclude_none=True,
            summary="An agent's public A2A Agent Card")
def public_agent_card(request: HttpRequest, slug: str, response: HttpResponse):
    """The agent's public Agent Card (A2A v1.0): what it offers people outside its workspace.

    Served only for an agent that has published a capability for contacts or
    unknown callers; any other slug is a 404. Supports `If-None-Match`.
    """
    card = agent_card.public_card(_agent_or_404(slug))
    if card is None:
        raise Http404("No such agent card.")
    return _respond(request, response, card, PUBLIC_CACHE)


@router.get("/agents/{slug}/extendedAgentCard",
            response={200: A2AAgentCardOut, 304: None},
            auth=session_auth, by_alias=True, exclude_none=True,
            summary="An agent's authenticated extended A2A Agent Card")
def extended_agent_card(request: HttpRequest, slug: str, response: HttpResponse):
    """The Agent Card as the signed-in caller sees it (A2A `GetExtendedAgentCard`).

    The public card's skills plus every capability the caller may invoke
    themselves. A 404 when that is nothing. Supports `If-None-Match`.
    """
    card = agent_card.extended_card(_agent_or_404(slug), request.user)
    if card is None:
        raise Http404("No such agent card.")
    return _respond(request, response, card, EXTENDED_CACHE, vary="Authorization, Cookie")
