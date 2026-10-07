"""The per-session multiplayer WebSocket (SP3).

One socket per session carries presence, the co-edited draft, and the streamed
turn. It uses realtime.groups for the (chat-agnostic) group name and realtime's
fan-out for turn events; the draft/presence/participant domain is chat's own.
"""
from __future__ import annotations

import logging
import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.harness import initiator as who
from apps.realtime.groups import chat_user_group, session_group

from . import access, agui, attach, drafts, presence, serializers, stream_map
from . import services as chat_services
from .models import Draft, Message, Session, SessionParticipant

_EDIT_ROLES = {SessionParticipant.OWNER, SessionParticipant.EDITOR}
# `draft.take_over` is a no-op now (everyone has their own draft) but stays
# listed so a viewer's old client is still told `forbidden`.
_EDIT_ACTIONS = (
    "draft.update", "draft.take_over", "draft.discard", "draft.set_visibility", "chat.send",
)


#: Query value that switches this socket to AG-UI. Opt-in per CONNECTION, and
#: absent means canopy's own frames, byte for byte as before — which is what
#: makes this projection additive rather than a migration. `canopy-ui` is
#: published to public npm at 0.7.0 with ace-web downstream, so a client that
#: does not ask must not be able to notice this exists.
AGUI_PROTOCOL = "ag-ui"

#: Frames an embedded widget never receives (see `services.WIDGET_HIDDEN_ROLES`).
#: Dropped on canopy's frame BEFORE the AG-UI projection, because the projected
#: snapshot carries the original frame verbatim and would otherwise leak the
#: rows back. `session.activity` is untouched, so the panel still says the agent
#: is working while its tools run.
_TOOL_EVENTS = frozenset({"chat.tool_use", "chat.tool_result"})


def without_tools(frame: dict) -> dict | None:
    """`frame` as an embedded widget sees it, or None to send nothing."""
    event = frame.get("event")
    if event in _TOOL_EVENTS:
        return None
    if event == "session.state":
        data = frame.get("data") or {}
        messages = data.get("messages")
        if messages:
            kept = [m for m in messages if m.get("role") not in chat_services.WIDGET_HIDDEN_ROLES]
            if len(kept) != len(messages):
                return {**frame, "data": {**data, "messages": kept}}
    return frame

log = logging.getLogger(__name__)


class SessionConsumer(AsyncJsonWebsocketConsumer):
    #: Set at connect from the query string. Not a header: a browser cannot set
    #: headers on a WebSocket handshake, and the subprotocol field is already
    #: how Channels' auth layers are configured here.
    agui_mode = False
    #: Whether this socket is an embedded widget's, which never receives tool
    #: calls. From the CREDENTIAL (`scope["via_widget"]`, set by the auth
    #: middleware), never from anything the client can ask for.
    hide_tools = False

    def _negotiate_protocol(self) -> None:
        raw = (self.scope.get("query_string") or b"").decode("utf-8", "replace")
        self.agui_mode = f"protocol={AGUI_PROTOCOL}" in raw
        # The evidence for deleting the native wire. canopy's own clients ask
        # for AG-UI as of 2026-09-18, but a tab open across that deploy, an
        # un-upgraded ace-web, or any `canopy-ui` consumer below 0.9 still asks
        # for native. When this line has said `native` for nobody over a full
        # week, the native branch of `send_json` has no callers and can go —
        # the canopy FRAMES stay regardless, as the in-memory model both ends
        # already share. Grep: `canopy_sessions.protocol`.
        log.info("canopy_sessions.protocol negotiated=%s",
                 AGUI_PROTOCOL if self.agui_mode else "native")

    async def send_json(self, content, close=False):
        """Every frame leaves through here, which is why the projection lives here.

        Overriding the one choke point rather than editing ~20 call sites means
        a canopy frame added later is projected automatically — or, if it has no
        AG-UI meaning, silently dropped from the AG-UI stream rather than
        leaking canopy's vocabulary into a protocol stream. `test_agui_socket`
        pins the frames that are known to be unmapped, so "dropped" stays a
        decision somebody made rather than one nobody noticed.
        """
        if self.hide_tools and isinstance(content, dict):
            content = without_tools(content)
            if content is None:
                if close:
                    await self.close()
                return

        if not self.agui_mode:
            await super().send_json(content, close=close)
            return

        thread_id = str(getattr(self, "session", None) and self.session.id or "")
        for event in agui.project(content, thread_id=thread_id):
            await super().send_json(agui.encode(event))
        if close:
            await self.close()

    async def _site_may_open(self, session) -> bool:
        """`site ∩ user`: a connected site acting for its visitor opens only its
        own agents' chats — the same limit REST applies (apps/tokens/delegation.py)."""
        from apps.tokens import delegation

        offered = await database_sync_to_async(delegation.offered_agent_ids)(
            self.scope.get("delegated_app"))
        return offered is None or session.agent_id in offered

    async def connect(self):
        self._negotiate_protocol()
        self.hide_tools = bool(self.scope.get("via_widget"))
        user = self.scope.get("user")
        contact = self.scope.get("contact")
        if not getattr(user, "is_authenticated", False) and contact is None:
            await self.close(code=4001)
            return
        raw_id = self.scope["url_route"]["kwargs"]["session_id"]
        session = await self._get_session(raw_id)
        if session is None:
            await self.close(code=4004)
            return

        if contact is not None:
            # A LISTENER, not a participant. A contact joins to hear the agent
            # reply and nothing else: presence, co-edited drafts and stop are
            # multiplayer features for members, and every one of them is keyed
            # on a user id a contact does not have. Read-only is both what they
            # need and the smallest thing to get right — sending stays on the
            # HTTP surface, where the principal is checked once.
            if session.contact_id != contact.pk:
                await self.close(code=4003)
                return
            self.session = session
            self.user = None
            self.contact = contact
            self.read_only = True
            self.role = None
            self.group = session_group(session.id)
            await self.channel_layer.group_add(self.group, self.channel_name)
            await self.accept()
            await database_sync_to_async(chat_services.attach_session)(session)
            await self.send_json(await self._snapshot())
            return

        # The same authority REST uses (`access`). No auto-join: opening a chat
        # is not a grant, and a role comes from the rule, not from a row this
        # connection wrote.
        role = await database_sync_to_async(access.role_for)(user, session)
        if role is None or not await self._site_may_open(session):
            await self.close(code=4003)
            return
        self.session = session
        self.user = user
        self.contact = None
        self.read_only = False
        self.role = role
        self.group = session_group(session.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        self.user_group = chat_user_group(user.id)
        await self.channel_layer.group_add(self.user_group, self.channel_name)
        await self.accept()
        await database_sync_to_async(presence.touch)(session.id, user.id)
        await database_sync_to_async(chat_services.attach_session)(session)
        await self.send_json(await self._snapshot())
        # Carry WHO joined, not just their id.
        #
        # Everyone already in the room built their participant list from the
        # snapshot they took when THEY connected. A person joining this session
        # for the first time is therefore absent from it, and the presence row
        # renders `participants.filter(p => present.includes(p.user_id))` — so a
        # newcomer was invisible to everyone already here, permanently, until
        # they happened to reload. The id alone can never fix that: there is no
        # name to render it with.
        #
        # It looked like it worked because a SessionParticipant row is durable —
        # the second time the same person joins, everyone's snapshot already has
        # them. So it failed only for a genuinely new participant, which is
        # exactly the case the feature exists for. Caught by the first
        # two-browser e2e this surface ever had.
        await self._broadcast({
            "type": "presence.joined",
            "user_id": user.id,
            "participant": await database_sync_to_async(self._participant_dto)(session, user),
        })

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if not group:
            return
        if getattr(self, "read_only", False):
            # No presence row to leave and nobody to tell — a contact never
            # announced itself. The attach count still has to come down, or a
            # closed panel keeps the runner streaming forever.
            await database_sync_to_async(chat_services.detach_session)(self.session)
            await self.channel_layer.group_discard(group, self.channel_name)
            return
        user_group = getattr(self, "user_group", None)
        if user_group:
            await self.channel_layer.group_discard(user_group, self.channel_name)
        await database_sync_to_async(presence.leave)(self.session.id, self.user.id)
        await database_sync_to_async(chat_services.detach_session)(self.session)
        await self._broadcast({"type": "presence.left", "user_id": self.user.id})
        await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get("action")
        data = content.get("data") or {}
        if getattr(self, "read_only", False):
            # Every action below is keyed on a user id, a participant role, or
            # both. Refusing the whole set is the honest answer rather than
            # letting a contact reach one that happens not to dereference
            # `self.user` today.
            await self._error("read_only", "this connection can listen, not act.")
            return
        if action == "presence.heartbeat":
            await database_sync_to_async(presence.touch)(self.session.id, self.user.id)
            # Keep the attach count alive for as long as the socket is open, so a
            # >1h session doesn't lose it and miscount the detach edge (Plan 4 Task 1).
            await database_sync_to_async(attach.renew)(self.session.id)
            return
        if action == "chat.stop" or action in _EDIT_ACTIONS:
            # Re-asked on EVERY acting frame, never trusted from connect: a
            # member removed or demoted mid-session kept sending until they
            # closed the tab. `chat.stop` is an edit too — REST `POST /stop`
            # requires write, and the socket used to handle it before this check.
            self.role = await database_sync_to_async(access.role_for)(self.user, self.session)
            if self.role is None:
                await self._error("forbidden", "You no longer have access to this session.")
                await self.close(code=4003)
                return
            if self.role not in _EDIT_ROLES:
                await self._error("forbidden", "You do not have edit access to this session.")
                return
        if action == "chat.stop":
            await self._chat_stop(data)
            return
        if action == "draft.update":
            await self._draft_update(data)
        elif action == "draft.take_over":
            # Nothing to take: everyone has their own draft (spec 2026-09-26).
            # Still accepted because canopy-ui <= 0.12 sends it.
            return
        elif action == "draft.discard":
            await self._draft_discard()
        elif action == "draft.set_visibility":
            await self._draft_set_visibility(data)
        elif action == "chat.send":
            await self._chat_send(data if isinstance(data, dict) else {})

    # -- actions --
    async def _error(self, code, message="", detail=None):
        payload = {"code": code, "message": message}
        if detail is not None:
            payload["detail"] = detail
        await self.send_json({"event": "session.error", "data": payload})

    async def _draft_update(self, data):
        try:
            # `visibility` is deliberately NOT read here any more. It used to
            # ride this version-guarded frame, which meant a stale keystroke
            # echo could race a mode change and downgrade it server-side
            # (canopy-ui#… "hidden->live->hidden" regression) — the version
            # check that protects the BODY has nothing to do with a choice
            # the user makes independently of typing. See `draft.set_visibility`.
            # An in-flight 0.14 client that still sends the field is simply
            # ignored here, not erred on.
            draft = await database_sync_to_async(drafts.update_draft)(
                self.session, user=self.user,
                expected_version=int(data.get("version", 0)),
                body=str(data.get("body", "")),
            )
        except drafts.DraftVersionMismatch as exc:
            await self._error(
                "draft_version_mismatch", "Draft changed since your last edit.",
                {"current_version": exc.current_version, "current_body": exc.current_body},
            )
            return
        await self._broadcast_draft(draft)

    async def _draft_set_visibility(self, data):
        """The mode is its OWN idempotent frame, not a field on the version-
        guarded keystroke frame — applied unconditionally (no version check),
        so it can never race a keystroke and never itself causes a
        `draft_version_mismatch`. See `drafts.set_visibility`."""
        visibility = data.get("visibility")
        draft = await database_sync_to_async(drafts.set_visibility)(
            self.session, user=self.user,
            visibility=visibility if isinstance(visibility, str) else "",
        )
        await self._broadcast_draft(draft)

    async def _draft_discard(self):
        draft = await database_sync_to_async(drafts.discard_draft)(self.session, self.user)
        await self._broadcast({"type": "draft.discarded", "draft_id": str(draft.pk),
                               "author_id": self.user.id})
        await self._broadcast_draft(draft)

    async def _broadcast_draft(self, draft):
        """One group message, rendered per socket: the author's own tabs get the
        full draft (`draft.updated`), everyone else the peer view (`draft.typing`).
        Every caller serializes the socket user's OWN draft, so the author is
        `self.user` — set it rather than re-SELECT it on every keystroke."""
        draft.author = self.user
        await self.channel_layer.group_send(self.group, {
            "type": "draft.updated", "author_id": draft.author_id,
            "draft": serializers.draft_dto(draft),
            "peer": serializers.peer_draft_dto(draft),
        })

    async def _chat_send(self, data=None):
        # `text` + `client_id` make a send self-contained and retryable. Without
        # them the send depended on an earlier `draft.update` frame having
        # landed, and a phone resuming from the background can hold a socket
        # that reads OPEN but is dead: the frames vanished and the prompt was
        # lost (2026-09-23). With them the client can resend over HTTP under the
        # SAME client_id, and the turn's idempotency key (built from it) makes
        # the retry a no-op if this frame did arrive after all. A frame without
        # them (an older client) commits the server draft, as before.
        data = data or {}
        text = data.get("text") if isinstance(data.get("text"), str) else None
        client_id = str(data.get("client_id") or "")[:100]
        user_message_id = await database_sync_to_async(self._commit_and_send)(text, client_id)
        draft = await database_sync_to_async(drafts.draft_for)(self.session, self.user)
        if user_message_id is not None:
            # A send commits the SENDER's own draft. Broadcast draft.committed +
            # the cleared draft FIRST so the sender's other tabs reset (and
            # peers' typing row clears) even if execution below is a no-op /
            # races a concurrent turn. `client_id` is the sender's receipt: it is
            # how the sender knows THIS send landed, and how the reducer avoids
            # inserting a second copy of a line it already shows. It reaches
            # only the author (`draft_committed`).
            await self._broadcast({
                "type": "draft.committed", "draft_id": str(draft.pk),
                "user_message_id": user_message_id, "client_id": client_id,
                "author_id": self.user.id,
            })
        await self._broadcast_draft(draft)
        # turn events fan out to the session group automatically (realtime signal).

    async def _chat_stop(self, data):
        # Un-queue every queued turn, or signal the runner to interrupt an
        # executing one (harness_services.cancel_turn). Broadcast to the WHOLE
        # group so every participant's Stop UI resets, not just the sender's —
        # but only when something was actually cancelled/cancel-requested, so a
        # stray Stop with nothing to cancel doesn't flip everyone's UI to
        # "cancelled" for no reason.
        route = await database_sync_to_async(self._stop_session)()
        if not route:
            return
        if route == "session":
            # NOT `chat.stream_cancelled`. All that has happened is that a frame was
            # published to the runner; nothing has pressed Escape yet, and it may
            # well fail (#649 exists because it often did). Claiming "cancelled"
            # here would be the same false green that bug was about, moved onto the
            # new path. Say what is actually true — we asked — and let the runner
            # report whether it landed (`stop:stopped` / `stop:failed`).
            await self._broadcast({"type": "session.stop", "state": "requested"})
            return
        await self._broadcast({
            "type": "chat.stream_cancelled",
            "message_id": data.get("message_id"), "partial_len": 0,
        })

    # -- sync DB helpers --
    def _commit_and_send(self, text=None, client_id=""):
        committed = drafts.commit_draft(self.session, self.user)
        # The text the sender SAW is the one to send. The server draft is only a
        # copy it may or may not have received.
        text = committed if text is None else text
        if not text.strip():
            return None
        # A site's token carries its runner requirements (ZDR). Stamped before
        # the send so the turn is routed under them, as the REST send does. A
        # union — a socket without them never lifts a floor already set.
        chat_services.add_runner_requirements(self.session,
                                              self.scope.get("runner_requirements", ()))
        from apps.common import request_context

        # No HTTP request behind a socket frame, so nothing else would record what
        # sent it: bind the socket's own provenance (apps/harness/provenance.py),
        # and name a widget's host the way the REST send does (`who.channel`).
        with request_context.bound(_socket_context(self.scope)):
            msg, turn = chat_services.send_message(
                session=self.session, text=text, user=self.user, client_id=client_id,
                initiator=who.for_scope(self.scope, via=who.channel(self.scope, "chat")),
            )
        chat_services.maybe_execute_inline(turn)
        return str(msg.pk)

    def _stop_session(self) -> str:
        """Stop this session, by whichever route actually owns the running work.

        Returns WHICH route fired — "turns" | "session" | "" — because the two mean
        different things to the client and must not be reported identically. A
        cancelled turn is a cancellation that has happened; a published session
        interrupt is a request that has not been attempted yet.

        TURNS FIRST. A chat reply is owned by a live Turn: cancelling it is what
        records the intent, reaches a queued turn behind the running one, and lets
        the runner's bridge interrupt and finish it. That path is unchanged.

        THEN THE SESSION. If no turn moved, the work is not turn-shaped — which is
        the normal state of an agent, board or scheduled turn, because those are
        fire-and-continue and go terminal seconds after the prompt is delivered
        (runner execute.py). Stop used to end here, find nothing, and return False:
        no interrupt, no broadcast, not even a flicker, while the agent worked on
        for another ten minutes. They are all sessions, so stop them as sessions.

        Deliberately not both: a chat turn's cancel already interrupts the same
        terminal through the bridge, and firing a second Escape at it could land
        after the agent has moved on to something else.

        The logic lives in `services.stop_session`, shared with REST/MCP
        `POST /canopy-sessions/{id}/stop` so the two can never disagree again
        (canopy-web#1226: REST stopped at the turns and did nothing to an agent).
        """
        return chat_services.stop_session(
            self.session, by=chat_services.person_name((getattr(self, "scope", None) or {}).get("user")))

    def _resolve_message_id_sync(self, turn_id, seq):
        if turn_id:
            pk = (
                Message.objects.filter(turn_id=turn_id, content__source_seq=seq)
                .values_list("pk", flat=True).first()
            )
            if pk is not None:
                return str(pk)
            return f"{str(turn_id)[:8]}:{seq}"
        return f"seq:{seq}"

    # -- group frame handlers (dots -> underscores) --
    async def access_recheck(self, message):
        """This person's access changed somewhere (they left a workspace). Ask
        the session ACL again and close if the answer is now no — an open tab
        would otherwise keep receiving a conversation they can no longer read."""
        workspaces = message.get("workspaces")
        if workspaces and self.session.workspace_id not in workspaces:
            return
        self.role = await database_sync_to_async(access.role_for)(self.user, self.session)
        if self.role is None:
            await self._error("forbidden", "You no longer have access to this session.")
            await self.close(code=4003)

    async def chat_turn_event(self, message):
        evt = message["event"]
        turn_id = message.get("turn_id")
        mid = await database_sync_to_async(self._resolve_message_id_sync)(turn_id, evt.get("seq"))
        for frame in stream_map.turn_event_to_frames(evt, lambda _seq: mid):
            await self.send_json(frame)

    async def chat_user_message(self, message):
        """A ledger-sourced send (`services._publish_user_message`), fanned out to
        the whole session — the peer-visibility a transcript-sourced session gets
        for free from `post_session_stream`, which this session has no runner to
        ship a transcript through. Already the exact client frame; no `stream_map`
        translation needed, because the real Message id is already in hand."""
        await self.send_json({"event": "chat.user_message", "data": message["data"]})

    async def session_title_updated(self, message):
        await self.send_json({"event": "session.title_updated", "data": {"title": message["title"]}})

    async def session_menu(self, message):
        """The agent started — or stopped — waiting on a dialog.

        Fired on the EDGE by the session report (`replace_reported_sessions`),
        so a chat you already have open gains its buttons when the agent asks,
        and loses them when somebody answers at the laptop. `menu: null` is the
        retraction and must be sent: buttons that outlive their dialog press a
        number into what is now an ordinary prompt.
        """
        await self.send_json({"event": "session.menu",
                              "data": {"menu": message.get("menu")}})

    def _is_author(self, message) -> bool:
        user = getattr(self, "user", None)
        return user is not None and message.get("author_id") == user.id

    async def draft_updated(self, message):
        if self._is_author(message):
            await self.send_json({"event": "draft.updated", "data": message["draft"]})
        else:
            # canopy-ui <= 0.12 adopts ANY draft.updated into its own composer, so
            # a peer's draft must never arrive under that name.
            await self.send_json({"event": "draft.typing", "data": message["peer"]})

    async def draft_committed(self, message):
        if not self._is_author(message):
            return  # an old client would build a message out of its OWN box
        await self.send_json({
            "event": "draft.committed",
            "data": {"draft_id": message["draft_id"], "user_message_id": message["user_message_id"],
                     "client_id": message.get("client_id", "")},
        })

    async def draft_discarded(self, message):
        if self._is_author(message):
            await self.send_json({"event": "draft.discarded", "data": {"draft_id": message["draft_id"]}})

    async def chat_stream_cancelled(self, message):
        await self.send_json({
            "event": "chat.stream_cancelled",
            "data": {"message_id": message.get("message_id"), "partial_len": message.get("partial_len", 0)},
        })

    async def page_invalidate(self, message):
        """Data the attached page is showing has changed; it should re-read.

        Carries the resource URI and nothing else, which is
        `notifications/resources/updated`'s own shape — the receiver refetches
        through the tool it already declared, where its own authorization
        applies. A diff would be a second source of truth for data the page
        already knows how to load.
        """
        await self.send_json({
            "event": "page.invalidate",
            "data": {"uri": message.get("uri", "")},
        })

    async def session_turn_status(self, message):
        """Where the ask this session is waiting on currently stands.

        The whole status, not a delta — see `status_feed`. Sent on every
        transition AND carried in the connect snapshot, because a client goes
        and looks precisely BECAUSE something stopped, which is the case a
        live-only frame always misses (the same lesson `session.menu` learned).
        """
        await self.send_json({"event": "session.turn_status",
                              "data": {"status": message.get("status")}})

    async def session_queued(self, message):
        """The whole list of human sends not yet in the transcript, visible to
        everyone watching — see `queued_feed`. Whole, never a delta, for the
        same reason `session.turn_status` is: a just-connected client has no
        correct prior to apply one to."""
        await self.send_json({"event": "session.queued", "data": {"queued": message.get("queued") or []}})

    async def session_page_action(self, message):
        """The agent is asking the attached page to do something.

        Only the doorbell — `PageAction` is the record, and the page answers by
        POSTing the result rather than over this socket. A frame published to a
        group with no consumer is silently discarded (see
        `RunnerBinding.pending_answer`), so a page that never hears this simply
        leaves the row to expire and the agent is told the page was not open.
        """
        await self.send_json({"event": "session.page_action", "data": message["action"]})

    async def session_stop(self, message):
        # "requested" comes from here, the moment we publish to the runner.
        # "stopped"/"failed" come from the runner itself, up the session stream
        # as `stop:` events (stream_map) — this handler serves the first only.
        await self.send_json({
            "event": "session.stop", "data": {"state": message.get("state", "requested")},
        })

    async def presence_joined(self, message):
        data = {"user_id": message["user_id"]}
        # Optional so an older publisher on the same channel layer still works
        # during a rolling deploy: the client falls back to id-only behaviour.
        if message.get("participant"):
            data["participant"] = message["participant"]
        await self.send_json({"event": "presence.joined", "data": data})

    async def presence_left(self, message):
        await self.send_json({"event": "presence.left", "data": {"user_id": message["user_id"]}})

    # -- helpers --

    def _participant_dto(self, session, user):
        """The joining user as the same DTO the snapshot uses, so a client can
        append it to `participants` without a second shape to reconcile.

        Read, never written: this used to `ensure_participant`, which made
        merely connecting a durable access grant."""
        row = session.participants.select_related("user").filter(user=user).first()
        if row is not None:
            return serializers.participant_dto(row)
        return serializers.participant_dto_for(user, self.role)

    async def _broadcast(self, message):
        await self.channel_layer.group_send(self.group, message)

    @database_sync_to_async
    def _get_session(self, raw_id):
        try:
            # runner_binding comes along because the connect snapshot reads the
            # pending dialog off it — otherwise every socket open pays a second
            # query in a different sync context to answer "is it waiting on me".
            return Session.objects.select_related("runner_binding").get(
                pk=uuid.UUID(str(raw_id)))
        except (Session.DoesNotExist, ValueError):
            return None

    @database_sync_to_async
    def _snapshot(self):
        parts = [serializers.participant_dto(p)
                 for p in self.session.participants.select_related("user").all()]
        # Someone reading through a leg other than a participant row (a
        # runner-discovered session, their agent's thread) has no row, and no
        # longer gets one by connecting. They still belong on the roster while
        # they are here — the presence row renders participants ∩ present.
        have = {p["user_id"] for p in parts}
        present = set(presence.present_ids(self.session.id))
        if self.user is not None:
            present.add(self.user.id)
        missing = present - have
        if missing:
            from django.contrib.auth import get_user_model

            for u in get_user_model().objects.filter(pk__in=missing):
                parts.append(serializers.participant_dto_for(
                    u, access.role_for(u, self.session) or SessionParticipant.VIEWER))
        # My own draft, and everyone else's in progress. A contact has no draft
        # and sees every non-empty one as a peer's.
        if self.user is not None:
            if self.role in _EDIT_ROLES:
                # An editor gets a row to type into: canopy-ui <= 0.12 sends
                # `draft.update` only while `active_draft` is non-null, so a
                # null here would silently stop its live typing.
                own = drafts.draft_for(self.session, self.user)
            else:
                # A viewer cannot type, so connecting must write nothing.
                own = Draft.objects.filter(
                    session=self.session, author=self.user, slot="next").first()
            peers = drafts.peer_drafts(self.session, self.user)
        else:
            own = None
            peers = drafts.peer_drafts(self.session, None)
        # Tail-first: the connect snapshot ships the last N messages (the same
        # SESSION_TAIL_DEFAULT the REST load uses), never the head. Scroll-back
        # for earlier history is REST (GET /{id}/messages?before=); Plan 4 wires
        # it into the panel. The session.state frame shape is otherwise frozen.
        # ONE policy for both transports (services.visible_transcript): tail-first,
        # falling back to the binding tail for a local session with no Message rows.
        # REST and this snapshot MUST agree — see tests/test_transcript_parity.py.
        messages, _has_more, _oldest = chat_services.visible_transcript(self.session)
        return {
            "event": "session.state",
            "data": serializers.session_state_dto(
                session=self.session,
                # None for a contact, who has no user id. The DTO uses it only
                # to mark which draft/messages are the caller's own, and a
                # listener has none — where `self.user.id` raised on exactly the
                # connection this snapshot exists to serve.
                current_user_id=self.user.id if self.user else None,
                current_contact_id=self.contact.pk if getattr(self, "contact", None) else None,
                participants=parts,
                present_ids=sorted(presence.present_ids(self.session.id)),
                draft=own,
                peer_drafts=peers,
                messages=messages,
                queued=chat_services.queued_messages(self.session),
            ),
        }


def _socket_context(scope) -> dict:
    """A WebSocket's provenance context: client=websocket, its user agent and
    address, and the door that authenticated it (`channels_auth`)."""
    from apps.common import request_context as rc
    from apps.common.client_ip import from_scope

    headers = {k.decode("latin-1").lower(): v.decode("latin-1", "replace")
               for k, v in scope.get("headers") or []}
    method = scope.get("auth_method") or ("contact" if scope.get("contact") is not None else "")
    app = scope.get("delegated_app")
    cred = {"type": method, "id": None, "label": getattr(app, "name", "") or ""} if method else None
    ctx = {
        "request_id": rc.mint_request_id(headers.get("x-request-id", "")),
        "client": "websocket",
        "user_agent": rc.clean(headers.get("user-agent", "")),
        "ip": rc.clean(from_scope(scope), 64),
        "credential": cred,
    }
    return {k: v for k, v in ctx.items() if v}
