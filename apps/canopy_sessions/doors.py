"""The one send preamble every chat door shares.

A chat send reaches `send.send_message` through several doors — the REST
send, the chat socket, an app view's `ui/message`, the contact API (and its app
views), and Slack. Each used to repeat the same preamble: stamp the site's runner
requirements, cap the client nonce, send, turn a refusal into a 422, run the dev
stub inline. `send_from` does it once; what genuinely differs between doors is a
row of `DOORS` or an explicit argument, never a copy.

What stays at the door: who may send (each door's own ACL query), the initiator
(each door knows its own asker), and door-only follow-ups (the REST send clears
the sender's server draft; Slack posts its own acknowledgement).

One rule is here, not at the door: a SENSITIVE conversation takes a message only
from one of its own people on one of its chat surfaces — the gate that reads it
back (`sensitivity.reads_content`). Whoever may not read what the agent answers may
not steer it either, and a send never makes the sender the key's holder
(`NotInIt`). Every door passes the surface it is (`chat_surface`).
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from ninja.errors import HttpError

from apps.harness import sensitivity

from . import services
from . import send

#: The client nonce is capped where it is used, not in the schemas, so the
#: request models (and `generated.ts`) stay unbounded `str`s.
CLIENT_ID_MAX = 100


@dataclass(frozen=True)
class Door:
    # A refused send (`ValueError`: an unplaceable runner, a forbidden pin) is a
    # 422 on an HTTP door; elsewhere it propagates to the door's own handler.
    http: bool
    # Run the dev/test stub inline (`send.maybe_execute_inline`). Slack never
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


class NotInIt(PermissionError):
    """A send into a sensitive conversation from someone not in it, or through a
    door that is not its chat surface (a PAT, MCP, a minted session, Slack)."""

    def __init__(self):
        super().__init__(sensitivity.NOT_IN_IT_SEND)


def send_from(
    door: str, *, session, text: str, user, initiator, runner_requirements, chat_surface: bool,
    client_id: str | None = "", context: dict | None = None, **send_kwargs,
):
    """Send `text` into `session` through `door`; returns `(message, turn)`.

    `runner_requirements`: the floor a site's token carries (`sensitive_data`), unioned into
    the session BEFORE the send so the turn is routed under it — never lifting
    one already set. REQUIRED, with no default: every door that can be reached
    through a site's token must pass the token's requirements, and a door that
    cannot (Slack) says `()` out loud. A default once let the signed-in app-view
    door send a sensitive-data site's member's turn with no floor at all. `context`: request provenance to bind around the send for a
    door with no HTTP request behind it (the socket; `apps/common/request_context`).
    Everything else (`placement`, `origin`, `origin_ref`, `parent`, ...) is
    `send.send_message`'s own. `chat_surface`: whether this request is a person's
    chat surface (`sensitivity.request_is_chat_surface` / `scope_is_chat_surface`),
    REQUIRED for the same reason.

    NotInIt (403 on an HTTP door) for a sensitive conversation `reads_content`
    refuses this sender."""
    spec = DOORS[door]
    services.add_runner_requirements(session, runner_requirements)
    contact = getattr(initiator, "contact", None)
    if not sensitivity.reads_content(session, chat_surface=chat_surface,
                                     user=None if contact is not None else user, contact=contact):
        if not spec.http:
            raise NotInIt()
        raise HttpError(403, sensitivity.NOT_IN_IT_SEND)
    if context is not None:
        from apps.common import request_context

        bound = request_context.bound(context)
    else:
        bound = nullcontext()
    try:
        with bound:
            message, turn = send.send_message(
                session=session, text=text, user=user,
                client_id=(client_id or "")[:CLIENT_ID_MAX], initiator=initiator,
                **send_kwargs,
            )
    except ValueError as exc:
        if not spec.http:
            raise
        raise HttpError(422, str(exc)) from None
    if spec.inline:
        send.maybe_execute_inline(turn)
    return message, turn
