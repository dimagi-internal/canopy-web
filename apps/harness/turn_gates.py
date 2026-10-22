"""The gates a turn passes before `POST /api/harness/turns/` enqueues it.

Each raises the route's own HttpError (422/403/404) and otherwise returns what
the next step needs, so the route only strings them together and calls
`services.enqueue_turn`. Kept apart from the route so each gate can be tested on
its own; the behaviour, the order and every message are the route's.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.agents import access
from apps.agents.gates import agent_for_work
from apps.agents.services import runner_may_hold_agent
from apps.workspaces import permissions as perms
from apps.workspaces import role_cap
from apps.workspaces import services as wsvc

from . import initiator as who
from . import turn_mode as turn_modes
from .models import Runner, Turn
from .schemas import TurnIn


def check_shape(payload: TurnIn) -> None:
    """A turn targets an agent XOR a project, with a known origin and routing (422)."""
    if bool(payload.agent_slug) == bool(payload.project):
        raise HttpError(422, "a turn targets an agent_slug XOR a project")
    if payload.origin not in dict(Turn.ORIGIN_CHOICES):
        raise HttpError(422, f"unknown origin '{payload.origin}'")
    if payload.routing not in dict(Turn.ROUTING_CHOICES):
        raise HttpError(422, f"unknown routing '{payload.routing}'")


def capped_origin_ref(request: HttpRequest, payload: TurnIn) -> dict:
    """`origin_ref` as the turn will carry it, with the caller's `acts_as` cap
    applied to the request and stamped by the server (never the client's)."""
    # Started as a lower role: every gate below judges the capped caller, and the turn
    # carries the cap for the claim, its envelope and its credential reads. The cap is
    # the server's to stamp, never a client's origin_ref to assert.
    origin_ref = {k: v for k, v in (payload.origin_ref or {}).items() if k != "acts_as"}
    if payload.acts_as:
        role_cap.apply(request.user, payload.acts_as)
        origin_ref["acts_as"] = role_cap.cap_of(request.user)
    return origin_ref


def resolve_target(request: HttpRequest, payload: TurnIn):
    """`(agent, workspace)` the turn is for — exactly one set — after the
    editor gate on it (403), or 404/422 for a workspace the caller cannot name."""
    agent = workspace = None
    if payload.agent_slug:
        # Editor, not bare membership: this enqueues arbitrary prompt text to be
        # executed AS the agent with the agent's credentials, which is the
        # author/executor tier by the role ladder's own definition. Project and
        # session turns are deliberately NOT gated the same way — a session turn
        # is what a chat send produces, and talking to an agent is exactly what
        # the interaction tier is for.
        agent = agent_for_work(request, payload.agent_slug,
                                   forbidden="running a turn for this agent requires the editor or owner role")
    else:
        # A project turn carries its own tenant.
        ws_slug = getattr(request, "workspace_slug", None)
        if ws_slug:
            # current_workspace gates membership on an explicit slug, so a
            # non-member's enqueue cannot land in someone else's workspace. 404
            # rather than 403: the harness must not leak which tenants exist
            # (same rule as agent_for_read).
            try:
                workspace = wsvc.current_workspace(request.user, ws_slug)
            except ValueError:
                raise HttpError(404, "workspace not found")
        else:
            workspace = wsvc.user_default_workspace(request.user)
            if workspace is None:
                # None means 0 memberships OR 2+ (ambiguous), and the two deserve
                # different answers. A 404 for the ambiguous case is a lie that
                # cost real debugging time: the flat shim 404'd every project
                # enqueue for a 2-workspace user (which the actual prod user is)
                # while reporting "not found". There is nothing to leak here —
                # they are the caller's OWN workspaces — so name the fix.
                if wsvc.user_workspace_slugs(request.user):
                    raise HttpError(
                        422,
                        "you belong to multiple workspaces; enqueue via "
                        "/api/w/{workspace}/harness/turns/",
                    )
                raise HttpError(404, "workspace not found")
        # A project turn is a prompt run in a repo on somebody's box — the same
        # author tier as an agent turn. It was bare membership, so a viewer
        # could send an arbitrary prompt to run in any repo the fleet declares.
        if not perms.can(request.user, workspace, perms.AGENT_WORK):
            raise HttpError(403, "running a turn requires the editor role or above")
    return agent, workspace


def check_email_poster(request: HttpRequest, payload: TurnIn, agent) -> None:
    """Only one of the agent's admins (the runner that read its mailbox) may post
    an email turn (403)."""
    if payload.origin == Turn.ORIGIN_EMAIL:
        # An email turn names its OWN asker from `origin_ref` (the sender and our
        # receiver's Authentication-Results), and a DMARC-aligned sender who is a
        # member becomes a VERIFIED user initiator — so whoever may post one may
        # write that verdict. The one legitimate poster is the runner that read
        # the mailbox, authenticated as its owner, and a box holds an agent only
        # when its owner is one of the agent's admins (runner_may_hold_agent). An
        # editor who is not an admin posting "dmarc=pass" from the owner's address
        # would otherwise be promoted to the owner. A project turn has no mailbox.
        if agent is None or not agent.is_admin(request.user):
            raise HttpError(
                403,
                "an email turn is posted by the runner that read the agent's mailbox, "
                "whose owner is one of the agent's admins; send other work with "
                "origin=api",
            )


def resolve_pin(request: HttpRequest, payload: TurnIn, agent):
    """The runner `payload.runner_id` pins the turn to, or None — refused when
    the caller cannot see it (422) or it could never run the turn (403)."""
    from .api import runner_read_q  # cycle: api imports this module for the route

    pinned = None
    if payload.runner_id is not None:
        # The question here is exactly "can the caller SEE this runner?" — a runner
        # it cannot see must 422 as unknown, never be attachable because its UUID
        # was guessed. So this follows the READ predicate: pinning directs work at
        # a box, it does not speak for it, and any member can already enqueue a
        # turn this runner will claim (claim_next_turn re-checks the tenant either
        # way). Retired runners are excluded too — pinning to one strands the turn
        # forever, since nothing can claim it.
        pinned = (
            Runner.objects.exclude(status=Runner.RETIRED)
            .filter(runner_read_q(request))
            .filter(id=payload.runner_id)
            .first()
        )
        if pinned is None:
            raise HttpError(422, f"unknown or retired runner id: {payload.runner_id}")
        if agent is None and payload.project not in pinned.project_names():
            # claim_next_turn's pin arm skips target matching, so without this a
            # pinned repo prompt lands on a box that never declared the repo —
            # routing's one rule for a project turn (runner_target_q's
            # `project__in`) is that the box drives that repo. Unpinned project
            # turns route by declaration as before.
            raise HttpError(
                403,
                f"runner {pinned.name} does not declare the repo {payload.project!r}; "
                "pin a runner that does, or dispatch without a runner",
            )
        if agent is not None:
            # claim_next_turn refuses it anyway; say so now rather than leave a
            # turn queued that nothing will ever claim.
            if not runner_may_hold_agent(pinned, agent):
                raise HttpError(
                    403,
                    f"runner {pinned.name} cannot run {agent.slug}: its owner must "
                    "be the agent's owner, a workspace owner, or one of its admins",
                )
            # Pinning places the agent's work on one box: the agent's admins may
            # pin any box that can hold it, anyone else only a box they administer
            # (docs/architecture/access.md).
            if not access.may_pin_runner(request.user, agent, pinned):
                raise HttpError(
                    403,
                    f"pinning {agent.slug}'s turn to runner {pinned.name} is for the agent's "
                    "owner or admins, or for someone who administers that runner; dispatch "
                    "without a runner and the agent's routing places it",
                )
    return pinned


def check_asker(request: HttpRequest, payload: TurnIn, agent):
    """The turn's initiator (None for email: enqueue_turn names that sender),
    after asking THE rule and the turn-mode gate of the requester."""
    initiator = (None if payload.origin == Turn.ORIGIN_EMAIL
                 else who.for_request(request, via=payload.origin))
    if agent is not None and initiator is not None:
        check_access(request, payload, agent, initiator)
    check_requested_turn_mode(request, payload, agent, initiator)
    return initiator


#: Credentials that establish the person on THIS request — a signed-in browser
#: or their own token (an MCP OAuth token is one). Not a widget's delegated
#: token: an embedding host vouching for its visitor must not unlock `auto`.
TURN_MODE_VERIFIED = frozenset({who.SESSION, who.PAT})


def check_requested_turn_mode(request, payload: TurnIn, agent, initiator) -> None:
    """Who may ask for a turn's mode (TurnIn.turn_mode). Raises 422/403, else returns.

    Mirrors apps/harness/turn_mode.py: `manual` from anyone who may enqueue the
    turn at all (the editor gate above already ran), `auto` only from the agent's
    owner or an admin — `Agent.is_admin`, which counts workspace owners — on a
    verified credential. The claim re-checks the admin leg (`turn_mode.requested`).
    """
    mode = payload.turn_mode
    if not mode:
        return
    if agent is None:
        raise HttpError(422, "turn_mode applies only to an agent turn (agent_slug), not a project")
    if payload.origin == Turn.ORIGIN_EMAIL:
        # A runner posts email on a stranger's behalf; the caller here is the
        # runner's owner, not the person whose message the turn answers.
        raise HttpError(422, "turn_mode cannot be requested on an email turn")
    if mode == turn_modes.MANUAL:
        return
    verified = (initiator is not None and initiator.kind == who.USER
                and initiator.assurance in TURN_MODE_VERIFIED)
    if not verified:
        raise HttpError(
            403, "turn_mode=auto needs a signed-in session or your own personal access token")
    if not access.decide(agent, request.user, verified=verified,
                         origin=payload.origin).may_request_auto:
        raise HttpError(
            403,
            f"turn_mode=auto is for {agent.slug}'s owner or admins; you may request "
            "turn_mode=manual (or none — a workspace editor's turns run manual anyway), "
            "or ask its owner to make you an admin",
        )


def check_access(request, payload: TurnIn, agent, initiator) -> None:
    """Refuse at enqueue a turn the runner would refuse, saying what the caller
    CAN do — THE rule (`apps.agents.access.decide`), asked of the requester.

    The editor gate above already ran, so a person reaching this is a workspace
    editor or above and is refused only by a future tightening; it is asked here
    anyway so this door can never disagree with the others."""
    user = request.user if getattr(request.user, "is_authenticated", False) else None
    d = access.decide(agent, user, verified=initiator.kind == who.USER
                      and initiator.assurance in TURN_MODE_VERIFIED, origin=payload.origin)
    if d.access == access.NONE:
        raise HttpError(403, d.reason or f"{agent.slug} does not take work from you")
