"""The per-session multiplayer WebSocket (SP3).

One socket per session carries presence, the co-edited draft, and the streamed
turn. It uses realtime.groups for the (chat-agnostic) group name and realtime's
fan-out for turn events; the draft/presence/participant domain is chat's own.
"""
from __future__ import annotations

import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.harness import services as harness_services
from apps.harness.models import Turn
from apps.realtime.groups import session_group

from . import attach, drafts, participants, presence, serializers, stream_map
from . import services as chat_services
from .models import Message, Session, SessionParticipant

_EDIT_ROLES = {SessionParticipant.OWNER, SessionParticipant.EDITOR}
_EDIT_ACTIONS = ("draft.update", "draft.take_over", "draft.discard", "chat.send")


class SessionConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        raw_id = self.scope["url_route"]["kwargs"]["session_id"]
        session = await self._get_session(raw_id)
        if session is None:
            await self.close(code=4004)
            return
        if not await database_sync_to_async(participants.can_access)(session, user):
            await self.close(code=4003)
            return
        self.session = session
        self.user = user
        # can_access auto-joins a workspace member as editor, so a role always
        # exists by now; default to editor defensively.
        self.role = await database_sync_to_async(participants.role_for)(session, user) or SessionParticipant.EDITOR
        self.group = session_group(session.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await database_sync_to_async(presence.touch)(session.id, user.id)
        await database_sync_to_async(chat_services.attach_session)(session)
        await self.send_json(await self._snapshot())
        await self._broadcast({"type": "presence.joined", "user_id": user.id})

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if not group:
            return
        await database_sync_to_async(presence.leave)(self.session.id, self.user.id)
        await database_sync_to_async(chat_services.detach_session)(self.session)
        await self._broadcast({"type": "presence.left", "user_id": self.user.id})
        await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get("action")
        data = content.get("data") or {}
        if action == "presence.heartbeat":
            await database_sync_to_async(presence.touch)(self.session.id, self.user.id)
            # Keep the attach count alive for as long as the socket is open, so a
            # >1h session doesn't lose it and miscount the detach edge (Plan 4 Task 1).
            await database_sync_to_async(attach.renew)(self.session.id)
            return
        if action == "chat.stop":
            await self._chat_stop(data)
            return
        if action in _EDIT_ACTIONS and self.role not in _EDIT_ROLES:
            await self._error("forbidden", "You do not have edit access to this session.")
            return
        if action == "draft.update":
            await self._draft_update(data)
        elif action == "draft.take_over":
            await self._draft_take_over()
        elif action == "draft.discard":
            await self._draft_discard()
        elif action == "chat.send":
            await self._chat_send()

    # -- actions --
    async def _error(self, code, message="", detail=None):
        payload = {"code": code, "message": message}
        if detail is not None:
            payload["detail"] = detail
        await self.send_json({"event": "session.error", "data": payload})

    async def _draft_update(self, data):
        try:
            draft = await database_sync_to_async(drafts.update_draft)(
                self.session,
                expected_version=int(data.get("version", 0)),
                body=str(data.get("body", "")),
                editor=self.user,
            )
        except drafts.DraftVersionMismatch as exc:
            await self._error(
                "draft_version_mismatch", "Draft changed since your last edit.",
                {"current_version": exc.current_version, "current_body": exc.current_body},
            )
            return
        except drafts.DraftLockHeld as exc:
            await self._error(
                "draft_lock_held", "Another teammate is editing.",
                {"holder_user_id": exc.holder_id, "expires_at": None},
            )
            return
        await self._broadcast_draft(draft)

    async def _draft_take_over(self):
        try:
            draft = await database_sync_to_async(drafts.take_over)(self.session, editor=self.user)
        except drafts.DraftLockHeld as exc:
            await self._error(
                "draft_lock_held", "Another teammate is editing.",
                {"holder_user_id": exc.holder_id, "expires_at": None},
            )
            return
        await self._broadcast({
            "type": "draft.lock_changed", "draft_id": str(draft.pk),
            "holder_user_id": self.user.id, "expires_at": None,
        })
        await self._broadcast_draft(draft)

    async def _draft_discard(self):
        draft = await database_sync_to_async(self._discard_active)()
        await self._broadcast({"type": "draft.discarded", "draft_id": str(draft.pk)})
        await self._broadcast_draft(draft)

    async def _chat_send(self):
        # Any editor may send the shared draft (commit ignores lock/version). Broadcast
        # draft.committed + the cleared draft FIRST so co-editors' UI resets even if
        # execution below is a no-op / races a concurrent turn.
        user_message_id = await database_sync_to_async(self._commit_and_send)()
        draft = await database_sync_to_async(drafts.active_draft)(self.session)
        if user_message_id is not None:
            await self._broadcast({
                "type": "draft.committed", "draft_id": str(draft.pk),
                "user_message_id": user_message_id,
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
    def _commit_and_send(self):
        text = drafts.commit_active_draft(self.session)
        if not text.strip():
            return None
        msg, turn = chat_services.send_message(session=self.session, text=text, user=self.user)
        chat_services.maybe_execute_inline(turn)
        return str(msg.pk)

    def _discard_active(self):
        draft = drafts.active_draft(self.session)
        if draft.body:
            draft.body = ""
            draft.version += 1
            draft.save(update_fields=["body", "version", "updated_at"])
        return draft

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
        """
        # ALL non-terminal turns, not just the newest: a mid-reply send queues a
        # second turn behind the one still running, so Stop must reach both.
        # NOTE: not a bare `any(... for turn in turns)` — any() short-circuits
        # on the first truthy result, which would skip cancelling every turn
        # after the first non-None one.
        turns = Turn.objects.filter(chat_session=self.session, status__in=list(Turn.NON_TERMINAL))
        cancelled = False
        for turn in turns:
            if harness_services.cancel_turn(turn) is not None:
                cancelled = True
        if cancelled:
            return "turns"
        return "session" if chat_services.interrupt_session(self.session) == "sent" else ""

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
    async def chat_turn_event(self, message):
        evt = message["event"]
        turn_id = message.get("turn_id")
        mid = await database_sync_to_async(self._resolve_message_id_sync)(turn_id, evt.get("seq"))
        for frame in stream_map.turn_event_to_frames(evt, lambda _seq: mid):
            await self.send_json(frame)

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

    async def draft_updated(self, message):
        await self.send_json({"event": "draft.updated", "data": message["draft"]})

    async def draft_committed(self, message):
        await self.send_json({
            "event": "draft.committed",
            "data": {"draft_id": message["draft_id"], "user_message_id": message["user_message_id"]},
        })

    async def draft_discarded(self, message):
        await self.send_json({"event": "draft.discarded", "data": {"draft_id": message["draft_id"]}})

    async def draft_lock_changed(self, message):
        await self.send_json({
            "event": "draft.lock_changed",
            "data": {
                "draft_id": message["draft_id"],
                "holder_user_id": message["holder_user_id"],
                "expires_at": message["expires_at"],
            },
        })

    async def chat_stream_cancelled(self, message):
        await self.send_json({
            "event": "chat.stream_cancelled",
            "data": {"message_id": message.get("message_id"), "partial_len": message.get("partial_len", 0)},
        })

    async def session_stop(self, message):
        # "requested" comes from here, the moment we publish to the runner.
        # "stopped"/"failed" come from the runner itself, up the session stream
        # as `stop:` events (stream_map) — this handler serves the first only.
        await self.send_json({
            "event": "session.stop", "data": {"state": message.get("state", "requested")},
        })

    async def presence_joined(self, message):
        await self.send_json({"event": "presence.joined", "data": {"user_id": message["user_id"]}})

    async def presence_left(self, message):
        await self.send_json({"event": "presence.left", "data": {"user_id": message["user_id"]}})

    # -- helpers --
    async def _broadcast(self, message):
        await self.channel_layer.group_send(self.group, message)

    async def _broadcast_draft(self, draft):
        await self.channel_layer.group_send(
            self.group, {"type": "draft.updated", "draft": serializers.draft_dto(draft)}
        )

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
        parts = list(self.session.participants.select_related("user").all())
        draft = drafts.active_draft(self.session)
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
                current_user_id=self.user.id,
                participants=parts,
                present_ids=sorted(presence.present_ids(self.session.id)),
                draft=draft,
                messages=messages,
            ),
        }
