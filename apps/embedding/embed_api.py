"""The embedded widget's own surface: what may this host offer, to this user.

One route so far. It answers the picker's question, which is a three-way one —
**agent x host x user** — and which nothing could answer before: `AppCredential`
covered app x tenant, `Agent.workspace` covered agent x tenant, and the app↔agent
edge did not exist. So a host named its single agent in its own settings
(ace-web's `CANOPY_AGENT_SLUG`) and canopy had no record of the choice.

Mounted under `/api/embed` rather than on the agents router (spec §12 left this
open) because the resource here is not an agent — it is *this embedding app's
offer to this caller*. Hanging it off `/api/agents` would read as a filtered
agent index, and the next person would reasonably add `?app=` to it, which is
precisely the parameter that must not exist.
"""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, ProblemError
from apps.api.route_gates import gate
from apps.tokens.models import AppCredential, DelegatedToken, EmbedAuditLog
from apps.tokens.schemas import WsTicketOut
from apps.workspaces import services as wsvc

from .audit import record as audit
from .rate_limit import MintRateLimitError, check_mint_limit
from . import defaults as agent_defaults
from .schemas import (
    EmbedSelfTokenIn,
    EmbedAgentOut,
    EmbedDefaultAgentIn,
    EmbedDefaultAgentOut,
    EmbedSelfOut,
    EmbedSelfTokenOut,
    EmbedSessionMemoryOut,
)

embed_router = Router(auth=session_auth, tags=["embed"])

#: How long a widget's delegated token lives.
#:
#: Fifteen minutes, not an hour. The client refetches when a token is within
#: `REFRESH_SKEW_MS` (5 min) of expiring, so a shorter life costs one extra mint
#: every ten minutes per open panel and nothing else — while cutting how long a
#: token that leaks out of a browser stays usable. It cannot go much below this:
#: at a TTL under the skew, every single call would refetch.
TOKEN_TTL_SECONDS = 15 * 60


def _acting_app(request: HttpRequest):
    """The `AppCredential` whose delegated token authenticated this request.

    Stamped by `BearerTokenAuthMiddleware`, so it can only come from the token —
    never a path, query or body the caller controls. That is what stops one host
    enumerating (or borrowing) another host's allowlist.

    Absent on every other auth path. A browser session has no app behind it, and
    the honest answer there is a refusal: falling back to "all agents this user
    can see" would silently turn this into an unscoped agent index, which is the
    opposite of what it exists to express.
    """
    app = getattr(request, "delegated_app", None)
    if app is None:
        raise HttpError(403, "this endpoint answers for an embedding app; present a delegated token")
    if app.revoked_at is not None:
        # Defence in depth, and no longer the load-bearing check: since
        # `DelegatedToken.lookup` filters on the app's revocation, a revoked
        # app's token does not authenticate anywhere, so this is unreachable
        # through the normal path. It was the ONLY place that re-checked, which
        # is what made the gap look closed — every other bearer-authenticated
        # route kept serving such a token for up to an hour.
        raise HttpError(403, "this embedding app's credential has been revoked")
    return app


@embed_router.get("/agents", response=list[EmbedAgentOut],
                  summary="Agents this embedding app may offer to this user")
@gate("member")  # delegated token required; app allowlist ∩ caller's workspaces
def list_embeddable_agents(request: HttpRequest) -> list[EmbedAgentOut]:
    """Agents this embedding app may offer, that you can also reach.

    Two conditions apply, and an agent appears only if both hold: an
    administrator has allowed this app to offer it, and you are a member of the
    workspace that owns it. So an empty list means one of those is missing —
    most often that nothing has been allowed for this app yet.

    Each row carries the workspace a session started with that agent will
    belong to. At most one row is `default`: the agent the page chose, else your
    own preferred agent, else the site's default — each only if it is listed.
    """
    # Rationale (deliberately NOT in the docstring — a route's docstring is its
    # published OpenAPI description; see #757). Neither condition is sufficient
    # alone: the allowlist alone would let a host offer an agent to someone with
    # no access to it, and membership alone is just "every agent you can see",
    # which ignores what the host was permitted to embed. Fails closed: an app
    # with no agent rows offers nothing.
    app = _acting_app(request)
    rows = list(_offered_rows(request, app))
    chosen = agent_defaults.resolve([a.pk for a in rows], app=app, user=request.user,
                                    bound_agent_id=getattr(request, "bound_agent_id", None))
    return [
        EmbedAgentOut(
            slug=a.slug,
            name=a.name,
            description=a.description,
            avatar_url=a.avatar_url,
            workspace=a.workspace_id,
            default=a.pk == chosen.default_id,
            preferred=a.pk == chosen.preferred_id,
        )
        for a in rows
    ]


def _offered_rows(request: HttpRequest, app):
    """The agents this site may offer this visitor: its allowlist ∩ their
    workspaces, narrowed to the one agent a bound token names."""
    reachable = wsvc.user_workspace_slugs(request.user)
    rows = (
        Agent.objects.filter(embedding_apps__app=app, workspace_id__in=reachable)
        .order_by("slug")
        .distinct()
    )
    # A token the site bound to one agent (a page that chose it) offers just that.
    bound = getattr(request, "bound_agent_id", None)
    if bound:
        rows = rows.filter(pk=bound)
    return rows


@embed_router.put("/default-agent", response=EmbedDefaultAgentOut,
                  summary="Make an agent this site offers you your default")
@gate("self", setting=True)
def set_embed_default_agent(request: HttpRequest, payload: EmbedDefaultAgentIn) -> EmbedDefaultAgentOut:
    """Make one of the agents this site offers you the one every panel opens on
    for you — here, on other sites and on canopy's own pages — unless a page
    chose its own agent. `agent: ""` clears it. Only your own preference, and
    only for a person's account."""
    # A site may set this for the visitor it acts for (the panel's "Make this my
    # default"). That is narrower than the memory switches a site may not touch:
    # it can only name an agent this site already offers the visitor, and it
    # changes which agent a panel opens on, never what anyone may see or do.
    from apps.contacts import people  # noqa: PLC0415 — contacts imports embedding at load

    app = _acting_app(request)
    if people.agent_of_login(request.user) is not None or not people.is_human_account(request.user):
        raise ProblemError(403, "Only a person has a default agent", type_=TYPE_FORBIDDEN)
    slug = (payload.agent or "").strip()
    if not slug:
        agent_defaults.set_preferred(request.user, None)
        return EmbedDefaultAgentOut(agent="")
    agent = _offered_rows(request, app).filter(slug=slug).first()
    if agent is None:
        raise HttpError(404, "This site does not offer you that agent")
    agent_defaults.set_preferred(request.user, agent)
    return EmbedDefaultAgentOut(agent=agent.slug)


# --- canopy-web embedding its own widget ------------------------------------
#
# The one host that is also canopy. Worth having despite the oddity: the
# reason to embed an agent is to talk to it about what is on the page, and the
# pages where that is most useful — a stale agent inbox, a feature set worth
# deprecating — are canopy's own. This is the only way to use it where the
# work is.
#
# It does NOT exercise the cross-origin boundary (the frame is same-origin
# here), so it is dogfooding for the UI, the context and the session lifecycle,
# not for the origin discipline. That still needs a real third-party host.


def _self_app() -> AppCredential | None:
    """The app whose widget canopy shows on its own pages, or None when off.

    One column on one row, where this used to be a NAME read from
    `EMBED_SELF_APP` and looked up. The setting made canopy a special case and
    failed silently in the one way that matters: a name that did not resolve
    produced no widget and no error, on either side.
    """
    from . import embed_apps

    return embed_apps.self_app()


@embed_router.get("/self", response=EmbedSelfOut,
                  summary="Whether canopy-web offers the widget on its own pages")
@gate("authenticated")
def embed_self(request: HttpRequest) -> EmbedSelfOut:
    """Drives the frontend's decision to mount the widget at all.

    Deliberately says nothing about *which* agents are available — that is
    `/api/embed/agents`, which answers for the caller and is the only place
    that intersects the app's allowlist with the viewer's memberships.
    """
    app = _self_app()
    return EmbedSelfOut(
        enabled=app is not None,
        app=app.name if app else "",
        # No preselected agent any more. `EMBED_SELF_AGENT` existed to skip the
        # picker, which the frame already does whenever exactly one agent is on
        # offer — so the setting only ever duplicated a decision the app's own
        # agent list already makes, in a place nobody could see it.
        agent="",
    )


def _asks_for_grant(request: HttpRequest) -> bool:
    """Whether the panel's mint body is `{"grant": true}` (`EmbedSelfTokenIn`). Read by
    hand, not as a Ninja body: older callers send no body at all, and that must stay a
    plain mint rather than a 400."""
    try:
        body = EmbedSelfTokenIn.model_validate_json(request.body or b"{}")
    except ValueError:
        return False
    return body.grant


@embed_router.post("/token", response=EmbedSelfTokenOut,
                   summary="Mint a delegated token for the caller, for canopy's own widget")
@gate("self", kind="ephemeral-credential", step_up="the caller's own embed token")
def embed_self_token(request: HttpRequest, page: str = "") -> EmbedSelfTokenOut:
    """The host-side token endpoint every embedder needs — for the host that is
    canopy itself.

    Issued DIRECTLY, for canopy's OWN signed-in user rather than for a visitor a
    host vouches for at `POST /api/auth/contact-token`. A
    third-party host must exchange because it holds a secret and canopy has to
    verify the assertion; here the two are one process, so there is no
    assertion to verify and no reason for canopy to hold a credential in order
    to talk to itself. It is not weaker: the endpoint is session-authenticated,
    so the caller already IS the user the token acts for, and the token it
    receives is the same short-lived revocable row any host would get.

    `page` names the canopy page the panel is on. When it is one canopy lets an
    agent act on as you (read-only, within your own access) AND the body says
    `"grant": true` — the panel sends it only once you have sent a message in it —
    canopy also takes a short grant to its own tools for that page, usable once you
    confirm it with your passkey in the panel; `host_grant` says whether it did. Any
    other value, or none, is simply no grant.
    """
    # Session-authenticated means ONLY a session. This route sits inside the
    # delegated-token surface (`^/api/embed/`), so without this a connected
    # site's token could mint itself a fresh one for canopy's OWN app: shedding
    # the site's assurance, outliving the site's revocation, and renewing itself
    # forever. canopy's panel calls it with the cookie and no Authorization
    # header (canopy-widget `mintToken`), so a header means it is not the panel.
    # Nor is a session cookie minted from a token (`is_machine`): the token it
    # returns is a chat surface (`sensitivity.request_is_chat_surface`), so minting
    # one from a PAT would read a sensitive conversation through an AI's door.
    from apps.common.views_debug import is_machine  # noqa: PLC0415 — loads the session store

    if (request.META.get("HTTP_AUTHORIZATION") or getattr(request, "delegated_app", None)
            or getattr(request, "via_mcp", False) or is_machine(request)):
        raise ProblemError(403, "Sign in to canopy in a browser to use its panel",
                           type_=TYPE_FORBIDDEN,
                           detail="This token is minted for a browser session only, "
                                  "never from another token.")
    app = _self_app()
    if app is None:
        raise HttpError(404, "no connected site shows its panel on canopy's own pages")

    # Cheaper to abuse than exchange in one specific way: it needs only a
    # stolen session cookie rather than an app secret, and every call writes a
    # `DelegatedToken` row. Keyed per user, who is who it mints for.
    try:
        check_mint_limit(request.user.pk)
    except MintRateLimitError as exc:
        audit(event=EmbedAuditLog.MINT, request=request, app=app, subject=request.user,
              ok=False, reason="rate_limited")
        raise HttpError(429, str(exc))

    # canopy as a HOST of its own MCP (apps/host_grants/self_host.py): the page's
    # scopes come from the server-side registry, never from the browser — the
    # browser only names which registered page it is on, and asks for a grant only
    # once the person has sent from the panel. Never fails the mint. The token
    # points at the grant, so a conversation reads through THIS page's grant
    # (`host_grants.lookup`), as for any other host.
    from apps.host_grants import self_host

    grant = self_host.grant_for_visitor(app, request.user, page) if _asks_for_grant(request) else None
    granted = grant is not None
    raw, token = DelegatedToken.issue(app=app, user=request.user, ttl_seconds=TOKEN_TTL_SECONDS,
                                      host_grant=grant)
    audit(event=EmbedAuditLog.MINT, request=request, app=app, subject=request.user,
          detail=f"ttl={TOKEN_TTL_SECONDS}s host_grant={granted} page={page[:40]!r}")
    return EmbedSelfTokenOut(token=raw, expires_at=token.expires_at.isoformat(), host_grant=granted)


@embed_router.post("/ws-ticket", response=WsTicketOut,
                   summary="Trade this delegated token for a one-time socket ticket")
# a one-time socket ticket standing for the caller's own delegated token  # mints a 15-min DelegatedToken for the caller
@gate("self", kind="ephemeral-credential")
def embed_ws_ticket(request: HttpRequest) -> WsTicketOut:
    """A single-use ticket to open a chat socket with, in place of the token.

    Open the socket with `?ticket=<ticket>` instead of `?token=`: the ticket
    works once, within `expires_in` seconds, and stands for the delegated token
    this request presented. Fetch a fresh one for every connection, reconnects
    included.
    """
    # Only a delegated token is traded. A browser on canopy's own pages signs its
    # socket in with the session cookie, and a PAT client can send a header.
    from apps.tokens import ws_ticket

    raw = ws_ticket.bearer(request)
    if getattr(request, "delegated_app", None) is None or DelegatedToken.lookup(raw) is None:
        raise HttpError(400, "present the delegated token this ticket should stand for")
    return WsTicketOut(ticket=ws_ticket.mint(raw), expires_in=ws_ticket.TICKET_TTL_SECONDS)


@embed_router.get("/sessions/{session_id}/agent-memory", response=EmbedSessionMemoryOut,
                  summary="Agent memory in this session, for the widget to show")
# read-only: the caller's OWN session with an agent this app offers; writes stay person-only on /api/people/me/
@gate("self")
def embed_session_memory(request: HttpRequest, session_id: str) -> EmbedSessionMemoryOut:
    """What applies in this conversation of yours: per feature (`record` — agents
    may learn about you; `use` — agents are told what they learned), whether it is
    available to you, its default, this session's choice and the effective value.

    Read-only. Only your own session with one of the agents this app offers;
    anything else is 404. Change it in canopy, at `manage_path`."""
    # A site acting for the person is not the person (people_api._the_person_themself
    # refuses `caller_token` for the same reason): it may SHOW what applies in a
    # conversation it hosts, never change it — otherwise a host could switch on
    # learning for every visitor. The write stays on /api/people/me/…, which a
    # delegated token cannot reach (tokens/delegation.py); canopy's own widget,
    # signed in by its session cookie, reaches it and gets the real toggles.
    import uuid

    from apps.canopy_sessions.models import Session
    from apps.contacts import people
    from apps.contacts import services as contact_services

    from . import delegation

    app = _acting_app(request)
    if people.agent_of_login(request.user) is not None or not people.is_human_account(request.user):
        raise HttpError(404, "Not found")
    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        raise HttpError(404, "Not found") from None
    session = Session.objects.filter(pk=sid, created_by=request.user).first()
    offered = delegation.offered_agent_ids(app) or set()
    if session is None or session.agent_id not in offered:
        raise HttpError(404, "Not found")
    person = contact_services.person_for(user=request.user)
    if person is None:
        raise HttpError(404, "Not found")
    from apps.contacts.people_api import session_memory_payload

    return EmbedSessionMemoryOut(**session_memory_payload(person, session),
                                 manage_path=f"/w/{session.workspace_id}/sessions/{session.pk}")
