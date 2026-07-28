"""Viewer presence over WebSocket: one socket per browser tab.

Navigation re-keys the connection with a fresh `presence.enter` rather than
reconnecting.

Two rules carry the security weight of this surface:

1. The page key is CLIENT-SUPPLIED. Its workspace segment is checked against
   the user's memberships before any group is joined — otherwise a user
   could observe who is viewing a workspace they cannot access.
2. Visibility is enforced HERE, not on the client. An opted-out user joins
   the group (so they still see others) but is never written to Redis, so
   no client — tampered, stale, or otherwise — can expose them.
"""
from __future__ import annotations

import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.workspaces.services import user_workspace_slugs

from . import presence_keys, presence_store
from .models import show_presence_for

GLOBAL_WORKSPACE = "global"


class PresenceConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        # Set connection-scoped state before the auth check: Channels still
        # dispatches disconnect() to this consumer after an early close() (a
        # rejected anonymous connection), and _leave_current() reads
        # self.group / self.page_key unconditionally.
        self.page_key: str | None = None
        self.group: str | None = None
        self.sub_location = ""
        self.visible = False

        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        self.user = user
        self.connection_id = uuid.uuid4().hex
        self.visible = await database_sync_to_async(show_presence_for)(user)
        self.workspaces = await database_sync_to_async(lambda: set(user_workspace_slugs(user)))()
        await self.accept()

    async def disconnect(self, code):
        await self._leave_current()

    async def receive_json(self, content, **kwargs):
        message_type = content.get("type", "")
        if message_type == "presence.enter":
            await self._enter(content)
        elif message_type == "presence.heartbeat":
            await self._heartbeat(content)

    # -- frame handlers --

    async def _enter(self, content):
        page_key = str(content.get("page_key") or "")
        sub_location = str(content.get("sub_location") or "")[:120]

        parsed = presence_keys.parse_page_key(page_key)
        if parsed is None:
            return  # malformed — drop silently, never confirm key shapes
        _app, workspace, _resource = parsed
        if workspace != GLOBAL_WORKSPACE and workspace not in self.workspaces:
            return  # not a member — drop silently, no existence leak

        if page_key != self.page_key:
            await self._leave_current()
            self.page_key = page_key
            self.group = presence_keys.group_name(page_key)
            await self.channel_layer.group_add(self.group, self.channel_name)

        self.sub_location = sub_location
        await self._write()
        await self._broadcast()

    async def _heartbeat(self, content):
        if self.page_key is None:
            return
        self.idle = bool(content.get("idle"))
        await self._write()
        # Only an idle transition changes what others see; a plain keepalive
        # does not need a broadcast.
        if self.idle != getattr(self, "_last_broadcast_idle", None):
            self._last_broadcast_idle = self.idle
            await self._broadcast()

    # -- helpers --

    async def _write(self):
        if not self.visible or self.page_key is None:
            return
        await presence_store.touch(
            self.page_key,
            user_id=self.user.id,
            connection_id=self.connection_id,
            name=getattr(self.user, "get_full_name", lambda: "")() or self.user.username,
            email=self.user.email or "",
            sub_location=self.sub_location,
            idle=bool(getattr(self, "idle", False)),
        )

    async def _leave_current(self):
        if self.group is None or self.page_key is None:
            return
        if self.visible:
            await presence_store.forget(
                self.page_key, user_id=self.user.id, connection_id=self.connection_id
            )
        group, page_key = self.group, self.page_key
        await self.channel_layer.group_discard(group, self.channel_name)
        self.group, self.page_key = None, None
        await self.channel_layer.group_send(
            group, {"type": "presence.roster_changed", "page_key": page_key}
        )

    async def _broadcast(self):
        if self.group is None:
            return
        await self.channel_layer.group_send(
            self.group, {"type": "presence.roster_changed", "page_key": self.page_key}
        )

    async def presence_roster_changed(self, event):
        """Every connection recomputes the roster itself.

        The `self` flag is per-recipient, so a single pre-rendered payload
        cannot be shared across the group. At tens of viewers per page the
        extra Redis reads are cheaper than the bookkeeping to avoid them.
        """
        page_key = event.get("page_key")
        if page_key != self.page_key:
            return
        viewers = await presence_store.roster(page_key)
        await self.send_json({
            "event": "presence.roster",
            "data": {
                "page_key": page_key,
                "viewers": [
                    {
                        "email": v["email"],
                        "name": v["name"],
                        "sub_location": v["sub_location"],
                        "idle": v["idle"],
                        "self": v["user_id"] == self.user.id,
                    }
                    for v in viewers
                ],
            },
        })
