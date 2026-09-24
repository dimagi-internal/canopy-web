"""A site acting for a user reaches `site ∩ user` — never the user's whole canopy.

A `DelegatedToken` is what a connected site's widget (or its server) holds when
one of its visitors turned out to be an existing canopy user
(`contact_api.contact_token`, `assurance=host_signed`), and what canopy's own
widget mints for itself (`/api/embed/token`). It used to authenticate EVERY
route as that user: a token handed to ace-web for Jonathan could read his
chats in any workspace, change workspace settings, mint PATs — anything he can
do anywhere in canopy. The site only ever needed its own agents' chat.

So the token now reaches the intersection of two things:

* **the user's own rights** — unchanged, every view still applies them; and
* **what the SITE may do** — two limits, both enforced here:

  1. *Which surface* (`reaches`): the embed API, the chat-session API, and a
     read-only view of the harness turns/sessions/runners that hosts show as
     "what is the agent doing". Anything else answers 403 from the middleware,
     before a view runs, naming the rule.
  2. *Which agents* (`offered_agent_ids`): only the agents the site's OWN
     registration offers, which are in that registration's one workspace. A
     session, turn or runner belonging to any other agent is 404 — the same
     answer a stranger gets, so a site cannot even learn it exists.

Only when the TOKEN is the identity. When canopy's own session cookie already
signed the request in (canopy embedding its own widget, same-origin), the person
at the browser is who is acting and the surface limit does not apply — but the
agent limit still does, because the widget is still speaking as the site.

The surface list is the complete set of calls canopy-widget, canopy-client and
ace-web make with this token, read from their source on 2026-09-24 — ace-web's
server (`apps/canopy/client.py`: create/send/stop, turns by id, unclaimable,
transcript) and frontend (`frontend/src/canopy/api.ts`: runners, sessions feed,
turns list, turn events). A host that needs more asks for it here, by name.
"""
from __future__ import annotations

import re

#: `(methods, path)` — None means any method. Matched against `path_info` (no
#: script prefix), in both the flat form and the tenant form `/api/w/<ws>/…`.
_SURFACE: list[tuple[frozenset[str] | None, re.Pattern]] = [
    (None, re.compile(r"^/api/embed/")),
    (None, re.compile(r"^/api/(?:w/[^/]+/)?canopy-sessions(?:/|$)")),
    # Read-only: what the agent is doing, for a host's "active runs" view and
    # for a host's server following a run it started. Never enqueue, cancel or
    # anything on a runner — those speak for canopy's fleet, not for a visitor.
    (frozenset({"GET", "HEAD"}),
     re.compile(r"^/api/(?:w/[^/]+/)?harness/(?:turns|sessions|runners)(?:/|$)")),
]

#: The one websocket a delegated token may open: its chat's live stream.
#: Matched on the path's END, because behind a script prefix (`/canopy` on
#: labs) the ASGI path can carry it.
_WS_SURFACE = re.compile(r"(?:^|/)ws/canopy-sessions/[0-9a-fA-F-]+/?$")


def reaches(method: str, path: str) -> bool:
    """Whether a site's delegated token may be used on this route at all."""
    method = (method or "").upper()
    return any(
        (methods is None or method in methods) and pattern.match(path or "")
        for methods, pattern in _SURFACE
    )


def reaches_ws(path: str) -> bool:
    return bool(_WS_SURFACE.search(path or ""))


def refusal(path: str) -> str:
    return (
        f"not_delegable: a site's token acts for its visitor only in that site's "
        f"chat — {path} is outside it. Sign in to canopy to use it directly."
    )


def offered_agent_ids(app) -> set | None:
    """The agents this acting site may reach, or None when no site is acting.

    None is "no limit from this rule", not "nothing": a browser session or a
    PAT is the person themself, and their own ACL is the whole answer. An empty
    set is a site offering nothing, which reaches nothing.

    Scoped to the registration's own workspace as well as its agent list —
    `set_agents` already refuses a foreign agent, so this is defence in depth
    against a row written some other way.
    """
    if app is None:
        return None
    from .models import AppCredentialAgent

    return set(
        AppCredentialAgent.objects.filter(app=app, agent__workspace_id=app.workspace_id)
        .values_list("agent_id", flat=True)
    )


def acting_app(request):
    """The site acting on this request, if any — only ever from the token."""
    return getattr(request, "delegated_app", None)


def offered_for(request) -> set | None:
    return offered_agent_ids(acting_app(request))


def turn_agent_id(turn):
    """The agent a turn belongs to: its own, or its chat session's."""
    if turn.agent_id:
        return turn.agent_id
    session = turn.chat_session if turn.chat_session_id else None
    return session.agent_id if session is not None else None
