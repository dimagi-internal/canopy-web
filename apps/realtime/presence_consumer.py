"""Viewer presence over WebSocket: one socket per browser tab.

Navigation re-keys the connection with a fresh `presence.enter` rather than
reconnecting.

Two rules carry the security weight of this surface:

1. The page key is CLIENT-SUPPLIED. Its workspace segment is checked against
   the user's memberships before any group is joined — otherwise a user
   could observe who is viewing a workspace they cannot access. Membership
   is checked LIVE on every `presence.enter`, never cached for the life of
   the connection: a long-lived socket must not keep granting access to a
   workspace the user has since been removed from. A REJECTED enter (bad
   key or lost membership) also tears down any group the connection
   currently holds — otherwise a member revoked mid-session would keep
   receiving their old workspace's roster broadcasts until they disconnect.
2. Visibility is enforced HERE, not on the client. An opted-out user joins
   the group (so they still see others) but is never written to Redis, so
   no client — tampered, stale, or otherwise — can expose them. Like
   membership, visibility is re-read on every `presence.enter` rather than
   cached at connect time, so flipping "Show me as viewing" off bounds the
   exposure window to "until you next navigate" rather than "until you
   close the tab". An opted-out enter also actively FORGETS any field this
   connection already wrote, rather than letting it age out of Redis on the
   60s field TTL — otherwise flipping the toggle off and staying on the same
   page keeps re-broadcasting a roster that still contains you.

Presence is an enhancement, never a dependency: every Redis call made from
a frame handler is wrapped, degrading to "no write" / "empty roster" rather
than letting a Redis blip raise out of `receive_json` and kill the consumer
(which would take the page's socket down with it).
"""
from __future__ import annotations

import logging
import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.workspaces import services as workspace_services
from apps.workspaces.models import Workspace

from . import presence_keys, presence_store
from .models import show_presence_for

logger = logging.getLogger(__name__)

#: This deployment's app segment. A page key naming any OTHER app is
#: rejected outright: cross-app roster keys are namespaced precisely so two
#: sibling deployments cannot share a roster, and honouring a foreign app
#: segment here would hand that namespacing back to the client.
APP_NAME = "canopy"

GLOBAL_SENTINEL = presence_keys.GLOBAL_SENTINEL


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
        self.idle = False
        self._last_broadcast_idle = False

        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        self.user = user
        self.connection_id = uuid.uuid4().hex
        # Deliberately NOT snapshotting visibility or workspace membership
        # here — both are re-read fresh on every presence.enter (see module
        # docstring) so a long-lived socket can't keep trusting a stale
        # connect-time answer.
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
            # Malformed — drop silently, never confirm key shapes. Still
            # tear down any group this connection currently holds: a
            # rejected enter must never leave a stale subscription in place
            # (see the non-member branch below for why this matters).
            await self._leave_current()
            return
        app, workspace, resource = parsed
        if app != APP_NAME:
            # A key for a different app. Treat exactly like a malformed one —
            # this deployment only ever hosts its own rosters.
            await self._leave_current()
            return
        # Rebuild the canonical form from the (whitespace-stripped) parsed
        # parts rather than trusting the raw client string — parse_page_key
        # strips each segment, so "canopy: ws :activity" and "canopy:ws:activity"
        # must resolve to the SAME group/Redis key, not silently fragment the
        # roster across two.
        canonical_key = f"{app}:{workspace}:{resource}"

        if workspace == GLOBAL_SENTINEL:
            # Belt and braces. WORKSPACE_RE cannot match the sentinel, so no
            # legitimately-shaped slug collides with it — but workspace
            # creation enforces no charset at all, so a row literally named
            # "~global" could still be created and would turn this branch
            # into "skip the membership gate for that tenant's roster".
            # One indexed PK lookup, and only on global pages.
            if await Workspace.objects.filter(pk=GLOBAL_SENTINEL).aexists():
                await self._leave_current()
                return
        else:
            member = await database_sync_to_async(workspace_services.is_member)(
                self.user, workspace
            )
            if not member:
                # Not a member — drop silently, no existence leak. ALSO tear
                # down whatever group this connection currently holds:
                # without this, a member whose access to their CURRENT
                # workspace is revoked mid-session keeps receiving that
                # workspace's roster broadcasts (and stays writeable in its
                # Redis hash) until they disconnect or the TTL expires, even
                # though every subsequent enter is correctly rejected from
                # here on. No frame is sent back to this client either way —
                # the departure broadcast this triggers only reaches the
                # OTHER viewers of the page being left.
                await self._leave_current()
                return

        if canonical_key != self.page_key:
            await self._leave_current()
            self.page_key = canonical_key
            self.group = presence_keys.group_name(canonical_key)
            await self.channel_layer.group_add(self.group, self.channel_name)
            # Idle state is per-page, not per-connection — arriving on a new
            # page must not carry over "idle" from whatever page the user
            # was previously parked on.
            self.idle = False
            self._last_broadcast_idle = False

        # Re-read fresh on every enter (see module docstring rule 2) — this
        # is the ONLY place visibility is (re-)computed for this connection.
        self.visible = await database_sync_to_async(show_presence_for)(self.user)

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
        if self.idle != self._last_broadcast_idle:
            self._last_broadcast_idle = self.idle
            await self._broadcast()

    # -- helpers --

    async def _write(self):
        if self.page_key is None:
            return
        if not self.visible:
            # Not just "skip the write" — actively remove anything this
            # connection wrote earlier. A re-enter on the SAME key with
            # visibility flipped off never passes through _leave_current, so
            # without this the previously-written field survives on its 60s
            # TTL and the broadcast that follows re-serves a roster that
            # still contains the user who just opted out.
            await self._forget()
            return
        try:
            await presence_store.touch(
                self.page_key,
                user_id=self.user.id,
                connection_id=self.connection_id,
                name=getattr(self.user, "get_full_name", lambda: "")() or self.user.username,
                email=self.user.email or "",
                sub_location=self.sub_location,
                idle=bool(self.idle),
            )
        except Exception:
            logger.warning("presence: write failed, skipping", exc_info=True)

    async def _forget(self):
        if self.page_key is None:
            return
        try:
            await presence_store.forget(
                self.page_key, user_id=self.user.id, connection_id=self.connection_id
            )
        except Exception:
            logger.warning("presence: forget failed, entry will expire on TTL", exc_info=True)

    async def _leave_current(self):
        if self.group is None or self.page_key is None:
            return
        await self._forget()
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
        try:
            viewers = await presence_store.roster(page_key)
        except Exception:
            # Degrade to "nobody here" rather than raising out of the
            # channel-layer dispatch and tearing the socket down.
            logger.warning("presence: roster read failed, serving empty", exc_info=True)
            viewers = []
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
