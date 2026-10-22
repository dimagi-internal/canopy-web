"""The one send preamble every chat door shares.

A chat send reaches `services.send_message` through several doors — the REST
send, the chat socket, an app view's `ui/message`, the contact API (and its app
views), and Slack. Each used to repeat the same preamble: stamp the site's runner
requirements, cap the client nonce, send, turn a refusal into a 422, run the dev
stub inline. `send_from` does it once; what genuinely differs between doors is a
row of `DOORS` or an explicit argument, never a copy.

What stays at the door: who may send (each door's own ACL query), the initiator
(each door knows its own asker), and door-only follow-ups (the REST send clears
the sender's server draft; Slack posts its own acknowledgement).
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from . import services

#: The client nonce is capped where it is used, not in the schemas, so the
#: request models (and `generated.ts`) stay unbounded `str`s.
CLIENT_ID_MAX = 100


@dataclass(frozen=True)
class Door:
    # A refused send (`ValueError`: an unplaceable runner, a forbidden pin) is a
    # 422 on an HTTP door; elsewhere it propagates to the door's own handler.
    http: bool
    # Run the dev/test stub inline (`services.maybe_execute_inline`). Slack never
    # has: its thread is answered by the status line, posted off the enqueue
    # signal, and its tests rely on the turn staying queued.
    inline: bool


DOORS = {
    "rest": Door(http=True, inline=True),
    "socket": Door(http=False, inline=True),
    "app_view": Door(http=True, inline=True),
    "contact": Door(http=True, inline=True),
    "contact_app_view": Door(http=True, inline=True),
    "slack": Door(http=False, inline=False),
}


def send_from(
    door: str, *, session, text: str, user, initiator, runner_requirements,
    client_id: str | None = "", context: dict | None = None, **send_kwargs,
):
    """Send `text` into `session` through `door`; returns `(message, turn)`.

    `runner_requirements`: the floor a site's token carries (ZDR), unioned into
    the session BEFORE the send so the turn is routed under it — never lifting
    one already set. REQUIRED, with no default: every door that can be reached
    through a site's token must pass the token's requirements, and a door that
    cannot (Slack) says `()` out loud. A default once let the signed-in app-view
    door send a ZDR site's member's turn with no floor at all. `context`: request provenance to bind around the send for a
    door with no HTTP request behind it (the socket; `apps/common/request_context`).
    Everything else (`placement`, `origin`, `origin_ref`, `parent`, ...) is
    `services.send_message`'s own."""
    spec = DOORS[door]
    services.add_runner_requirements(session, runner_requirements)
    if context is not None:
        from apps.common import request_context

        bound = request_context.bound(context)
    else:
        bound = nullcontext()
    try:
        with bound:
            message, turn = services.send_message(
                session=session, text=text, user=user,
                client_id=(client_id or "")[:CLIENT_ID_MAX], initiator=initiator,
                **send_kwargs,
            )
    except ValueError as exc:
        if not spec.http:
            raise
        from ninja.errors import HttpError

        raise HttpError(422, str(exc)) from None
    if spec.inline:
        services.maybe_execute_inline(turn)
    return message, turn
