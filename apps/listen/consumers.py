"""`/ws/listen/<id>/?role=phone|laptop`: the relay between a phone and its owner's laptop.

Binary messages are end-to-end encrypted frames. This consumer passes each one to the
other role as it is: it never reads, stores or logs one, not even its size per frame
(only totals reach the database, on the role's beat). The server's own messages are
text: `{"type":"peer","role","connected"}` and `{"type":"ended","reason"}`.

Who: the session's owner — the phone by its session cookie, the laptop by the runner's
own key (held to sessions on that runner) or the owner's token. One socket per role
(a second is closed 4009, as soon as it has been accepted, so the client can read why).
Over 64 KiB, or over 60 messages in a second, closes that socket 4008.

A frame sent while the other role is away is held in this socket's memory for at most
10 s (64 frames) and passed on when it arrives, so neither end loses its first message
to who connected first. It is never written anywhere.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.realtime.channels_auth import still_signed_in

from . import services
from .models import ROLES, ListenSession

MAX_FRAME = 64 * 1024
MAX_PER_SECOND = 60
HOLD_S = 10.0
HOLD_MAX = 64

UNAUTHENTICATED, BAD_REQUEST, NOT_YOURS = 4001, 4000, 4003
TOO_MUCH, ROLE_TAKEN = 4008, 4009


class ListenConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self._holding = False
        self._ended = False
        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=UNAUTHENTICATED)
            return
        query = parse_qs((self.scope.get("query_string") or b"").decode("latin1"))
        role = (query.get("role") or [""])[0]
        try:
            sid = uuid.UUID(str(self.scope["url_route"]["kwargs"]["session_id"]))
        except (KeyError, ValueError):
            await self.close(code=BAD_REQUEST)
            return
        if role not in ROLES:
            await self.close(code=BAD_REQUEST)
            return
        runner_id = self.scope.get("runner_id")
        if runner_id is not None and role != "laptop":
            await self.close(code=NOT_YOURS)  # a runner's key is the laptop, never the phone
            return
        outcome, detail = await database_sync_to_async(services.claim_role)(
            user, sid, role, self.channel_name, runner_id=runner_id)
        if outcome == "missing":
            await self.close(code=NOT_YOURS)
            return
        await self.accept()
        if outcome == "taken":
            await self.close(code=ROLE_TAKEN)
            return
        if outcome == "ended":
            self._ended = True
            await self._send_note({"type": "ended", "reason": detail or "ended"})
            await self.close()
            return
        self._sid, self._role, self._peer = sid, role, services.other(role)
        self._holding, self._peer_present = True, bool(detail)
        self._held: deque[tuple[float, bytes]] = deque()
        self._sent = 0
        self._window, self._count = time.monotonic(), 0
        self._groups = [services.session_group(sid), services.role_group(sid, role)]
        for group in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)
        await self._send_note({"type": "peer", "role": self._peer, "connected": self._peer_present})
        await self.channel_layer.group_send(services.role_group(sid, self._peer), {
            "type": "listen.peer", "role": role, "connected": True})
        self._beat = asyncio.create_task(self._beat_loop())

    async def disconnect(self, code):
        if not getattr(self, "_holding", False):
            return
        self._holding = False
        self._beat.cancel()
        for group in self._groups:
            await self.channel_layer.group_discard(group, self.channel_name)
        sent, self._sent = self._sent, 0
        await database_sync_to_async(services.release_role)(self._sid, self._role, self.channel_name, sent=sent)
        if not self._ended:
            await self.channel_layer.group_send(services.role_group(self._sid, self._peer), {
                "type": "listen.peer", "role": self._role, "connected": False})

    async def receive(self, text_data=None, bytes_data=None):
        if not self._holding or self._ended or bytes_data is None:
            return  # a client's text is not part of the protocol; nothing reads it
        if len(bytes_data) > MAX_FRAME or self._over_rate():
            await self.close(code=TOO_MUCH)
            return
        if not self._peer_present:
            self._hold(bytes_data)
            return
        await self._relay(bytes_data)

    # -- from the channel layer ------------------------------------------------
    async def listen_frame(self, message):
        await self.send(bytes_data=message["b"])

    async def listen_peer(self, message):
        if message.get("role") != self._peer:
            return
        self._peer_present = bool(message.get("connected"))
        await self._send_note({"type": "peer", "role": self._peer, "connected": self._peer_present})
        if self._peer_present:
            while self._held:
                _at, frame = self._held.popleft()
                await self._relay(frame)

    async def listen_ended(self, message):
        self._ended = True
        await self._send_note({"type": "ended", "reason": message.get("reason") or "ended"})
        await self.close()

    # -- helpers -----------------------------------------------------------------
    async def _relay(self, frame: bytes) -> None:
        self._sent += len(frame)
        await self.channel_layer.group_send(services.role_group(self._sid, self._peer),
                                            {"type": "listen.frame", "b": frame})

    def _hold(self, frame: bytes) -> None:
        now = time.monotonic()
        while self._held and (now - self._held[0][0] > HOLD_S or len(self._held) >= HOLD_MAX):
            self._held.popleft()
        self._held.append((now, frame))

    def _over_rate(self) -> bool:
        now = time.monotonic()
        if now - self._window >= 1.0:
            self._window, self._count = now, 0
        self._count += 1
        return self._count > MAX_PER_SECOND

    async def _send_note(self, note: dict) -> None:
        await self.send(text_data=json.dumps(note, separators=(",", ":")))

    async def _beat_loop(self) -> None:
        while True:
            await asyncio.sleep(services.BEAT_S)
            sent, self._sent = self._sent, 0
            state = await database_sync_to_async(services.beat)(self._sid, self._role, self.channel_name, sent=sent)
            if not await still_signed_in(self.scope):
                await self.close(code=UNAUTHENTICATED)
                return
            if state == "ended" and not self._ended:
                # The `ended` notice went to a channel layer this process can't hear.
                ended = await database_sync_to_async(self._end_reason)()
                await self.listen_ended({"reason": ended})
                return
            if state == "":
                await self.close(code=ROLE_TAKEN)
                return

    def _end_reason(self) -> str:
        return ListenSession.objects.filter(pk=self._sid).values_list("end_reason", flat=True).first() or "ended"
