"""WebSocket consumers over the realtime transport.

TurnConsumer live-tails one turn's append-only TurnEvent ledger. Replay is
cursor-based (?after=seq) reusing the same read the REST endpoint uses, then live
frames arrive via the turn.{id} group. Frames are idempotent on the client by seq.

SupervisorConsumer (Task 7) pushes runner-status + waiting-count deltas.
"""
from __future__ import annotations

import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.harness import runner_protocol
from apps.harness import services as harness_services
from apps.harness import claim, ledger
from apps.harness.models import Runner, Turn
from apps.workspaces.services import user_workspace_slugs

from . import groups
from .channels_auth import still_signed_in


class TurnConsumer(AsyncJsonWebsocketConsumer):
    REPLAY_PAGE = 500

    async def connect(self):
        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        raw_id = self.scope["url_route"]["kwargs"]["turn_id"]
        turn = await self._get_turn(raw_id)
        if turn is None:
            await self.close(code=4004)
            return
        if not await database_sync_to_async(groups.user_can_read_turn)(user, turn):
            await self.close(code=4003)
            return
        self.group = groups.turn_group(turn.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        # Cursor replay: send everything after the client's last-seen seq, then
        # live-tail via turn_event group messages. Paged to exhaustion — a turn
        # with more than REPLAY_PAGE unseen events (routine on the SP2 chat path,
        # where a turn streams token-level events) must not be silently truncated:
        # those rows predate the group join, so the live tail would never re-emit
        # them, leaving a hole between the replayed page and new appends.
        after = self._after_from_query()
        while True:
            batch = await self._replay_batch(turn, after)
            if not batch:
                break
            for event in batch:
                await self.send_json({"event": event})
            after = batch[-1]["seq"]
            if len(batch) < self.REPLAY_PAGE:
                break

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def turn_event(self, message):
        # group_send type="turn.event" dispatches here (dots -> underscores).
        await self.send_json({"event": message["event"]})

    # -- helpers --
    @database_sync_to_async
    def _get_turn(self, raw_id):
        try:
            turn_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            return None
        return (
            Turn.objects.select_related("agent", "chat_session")
            .filter(pk=turn_id)
            .first()
        )

    @database_sync_to_async
    def _replay_batch(self, turn, after: int):
        rows = turn.events.filter(seq__gt=after).order_by("seq")[: self.REPLAY_PAGE]
        return [groups.serialize_turn_event(row) for row in rows]

    def _after_from_query(self) -> int:
        raw = (self.scope.get("query_string") or b"").decode()
        for part in raw.split("&"):
            if part.startswith("after="):
                try:
                    return int(part[len("after="):])
                except ValueError:
                    return 0
        return 0


class SupervisorConsumer(AsyncJsonWebsocketConsumer):
    """Live /supervisor: a per-user group receives runner-status and
    waiting-count deltas; a snapshot is sent on connect. The socket spans all the
    user's workspaces (the fleet is cross-tenant, like /activity)."""

    async def connect(self):
        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        self.group = groups.supervisor_user_group(user.id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json(await self._snapshot(user))

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def supervisor_runner(self, message):
        await self.send_json(message)

    async def supervisor_waiting(self, message):
        await self.send_json(message)

    async def supervisor_sessions(self, message):
        await self.send_json(message)

    @database_sync_to_async
    def _snapshot(self, user):
        from .snapshot import supervisor_snapshot

        return supervisor_snapshot(user)


class RunnerConsumer(AsyncJsonWebsocketConsumer):
    """A runner's persistent control channel (RC1).

    PAT-authed via the handshake (channels_auth sets scope["user"]); the runner may
    connect only to a runner it owns (`runner_protocol.acting_runner_q` — a runner
    nobody owns is reachable by nobody, as over REST). Joins the per-runner group + its
    workspaces' runnable groups, so:
      - server → runner: a `wake` frame when a turn becomes claimable (enqueue
        publishes to runnable.{ws}); the runner responds by claiming.
      - runner → server: `claim` / `heartbeat` / `finish` frames, read and applied
        by the SAME functions the REST routes use (`apps/harness/runner_protocol.py`,
        over `HeartbeatIn` / `TurnFinishIn`), so the two surfaces can't drift.

    REST claim/heartbeat stay fully working alongside — this ADDS a real-time
    channel, it does not replace the durable claim/lease path (Postgres remains the
    source of truth; a dropped socket loses no work).
    """

    async def connect(self):
        user = self.scope.get("user")
        if not getattr(user, "is_authenticated", False):
            await self.close(code=4001)
            return
        raw_id = self.scope["url_route"]["kwargs"]["runner_id"]
        runner = await self._runner_for_user(user, raw_id)
        if runner is None:
            await self.close(code=4003)  # unknown or not owned — no existence leak
            return
        self._runner_pk = runner.id
        self._groups = [groups.runner_group(runner.id)]
        for ws in await self._runner_workspaces(runner):
            self._groups.append(groups.runnable_group(ws))
        for group in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        for group in getattr(self, "_groups", []):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def runner_wake(self, message):
        # runnable.{ws} group_send type="runner.wake" dispatches here.
        await self.send_json({"type": "wake"})

    async def runner_cancel(self, message):
        # runner.{id} group_send type="runner.cancel" — the user asked to stop a turn
        # this runner is executing. Forwarded down the socket; the executor checks its
        # cancel set every bridge poll (~0.5s), interrupts emdash, finishes CANCELLED.
        await self.send_json({"type": "cancel", "turn_id": message.get("turn_id")})

    async def runner_session_interrupt(self, message):
        # runner.{id} group_send type="runner.session_interrupt" — the user asked to
        # stop a SESSION rather than a turn. The turn-shaped cancel above can only
        # reach work a live Turn still owns; an agent/board/scheduled turn is
        # fire-and-continue and terminal seconds after delivery, so this is the only
        # route that reaches it. The runner presses Escape in that session's
        # terminal, keyed on session_key exactly as menu_answer is.
        await self.send_json({
            "type": "session_interrupt",
            "session_id": message.get("session_id"),
            "session_key": message.get("session_key"),
        })

    async def runner_stream(self, message):
        # runner.{id} group_send type="runner.stream" — start/stop live streaming a
        # session this runner backs. Forwarded to the runner socket; the runner also
        # syncs desired-streaming via GET /runners/{id}/streams, so a missed frame
        # only costs latency (like the wake channel).
        await self.send_json({
            "type": "stream",
            "session_id": message.get("session_id"),
            "session_key": message.get("session_key"),
            "desired": message.get("desired"),
        })

    async def runner_check_inbox(self, message):
        # runner.{id} group_send type="runner.check_inbox" — the Gmail doorbell
        # (apps/inbound/services.ring): a push said this mailbox changed; the
        # runner marks it due and reads it on its own poll thread. This handler
        # is load-bearing beyond forwarding: Channels RAISES on an unhandled
        # group-message type, so without it the doorbell frame not only never
        # arrived, it errored the consumer carrying wake/cancel too.
        await self.send_json({
            "type": "check_inbox",
            "mailbox": message.get("mailbox"),
        })

    async def runner_update_available(self, message):
        # runner.{id} group_send type="runner.update_available" — this box's
        # last heartbeat reported a code_sha that differs from the deployed
        # expectation. The runner kickstarts its SEPARATE updater job, which
        # re-checks staleness and the in-flight marker itself — the daemon never
        # installs anything on this signal, and the updater's 30-min timer stays
        # the rescue path for a daemon too dead to hear a frame.
        await self.send_json({
            "type": "update_available",
            "expected_sha": message.get("expected_sha"),
        })

    async def runner_menu_answer(self, message):
        # runner.{id} group_send type="runner.menu_answer" — a human answered, from
        # the web, the dialog an agent is blocked on. The runner presses the key in
        # the session's terminal; the server never touches a terminal itself.
        # Relayed field by field, so anything added to the published frame has to
        # be added HERE too or it is silently dropped — which is exactly what
        # happened to both fields below on 2026-08-12:
        #
        #   `selections` missing -> the runner fell back to the single-key
        #   `option` path and pressed a NUMBER at a multi-select, toggling one
        #   checkbox and answering nothing.
        #   `answer_id` missing  -> the runner's retire call carried an empty id,
        #   the server's id match refused it, the answer stayed queued and the
        #   poll tick pressed it a second time.
        #
        # Both were invisible to the unit tests because those built the frame by
        # hand. `test_realtime_menu_answer.py` now asserts this relay against
        # what `answer_menu` actually publishes.
        await self.send_json({
            "type": "menu_answer",
            "session_id": message.get("session_id"),
            "session_key": message.get("session_key"),
            "project": message.get("project"),
            "option": message.get("option"),
            "selections": message.get("selections"),
            "texts": message.get("texts"),
            "answer_id": message.get("answer_id"),
        })

    async def runner_close_session(self, message):
        # runner.{id} group_send type="runner.close_session" — a human closed this
        # session from the web. The runner deletes the emdash task and reports the
        # name in its `archived:` closing signal; the server wrote nothing, so the
        # emdash task stays the single source of truth for a local session and a
        # failed delete simply leaves the row where it is.
        await self.send_json({
            "type": "close_session",
            "session_id": message.get("session_id"),
            "session_key": message.get("session_key"),
            "project": message.get("project"),
        })

    async def receive_json(self, content, **kwargs):
        # Every frame, not just the handshake: a revoked token must stop claiming
        # and must stop reading as a live box (channels_auth.still_signed_in).
        if not await still_signed_in(self.scope):
            await self.close(code=4001)
            return
        action = content.get("action")
        if action == "claim":
            # `paused`: agent slugs the box has locally paused (its per-agent pause,
            # or a credential-resolve backoff) — the REST claim's `?paused=`.
            paused = content.get("paused") or []
            # `acts_as`: the lower mode the box claims in (role_cap) — the REST
            # claim's X-Canopy-Acts-As.
            turn = await self._claim([str(s) for s in paused if isinstance(s, str) and s][:50],
                                     acts_as=str(content.get("acts_as") or ""))
            await self.send_json({"type": "claim.result", "turn": turn})
        elif action == "heartbeat":
            refresh, cancel_ids, pause = await self._heartbeat(content)
            # The ack carries the durable refresh request back, the same thing the
            # REST reply's RunnerOut.refresh_pending says — this is the cloud
            # runner's primary beat, so it is the path that has to answer.
            # And the stops this runner owes (services.cancel_requested_turn_ids):
            # the `cancel` frame is a doorbell a reconnecting socket can miss; this
            # re-delivers it every beat until the turn is finished.
            # And whether it is paused, and why: a cloud box that paused itself
            # (no working Claude credential) lifts only a pause whose note is
            # its own, so it has to be told which kind it is under.
            await self.send_json({"type": "heartbeat.ack", "refresh_pending": bool(refresh),
                                  "cancel_turn_ids": cancel_ids, **pause})
        elif action == "start":
            ok = await self._start(content.get("turn_id"), content.get("session_id") or "")
            await self.send_json({"type": "start.ack", "ok": ok})
        elif action == "event":
            n = await self._append(content.get("turn_id"), content.get("events") or [])
            await self.send_json({"type": "event.ack", "count": n})
        elif action == "finish":
            ok, error = await self._finish(content)
            await self.send_json({"type": "finish.ack", "ok": ok, **({"error": error} if error else {})})
        else:
            await self.send_json({"type": "error", "detail": f"unknown action {action!r}"})

    # -- helpers (sync ORM, bridged) --
    @database_sync_to_async
    def _runner_for_user(self, user, raw_id):
        try:
            rid = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            return None
        return (
            Runner.objects.exclude(status=Runner.RETIRED)
            .filter(pk=rid)
            .filter(runner_protocol.acting_runner_q(user))
            .first()
        )

    @database_sync_to_async
    def _runner_workspaces(self, runner):
        if not runner.owner_id:
            return []
        return sorted(user_workspace_slugs(runner.owner))

    @database_sync_to_async
    def _claim(self, paused=None, acts_as=""):
        runner = Runner.objects.filter(pk=self._runner_pk).first()
        if runner is None:
            return None
        turn = claim.claim_next_turn(runner, exclude_slugs=paused or None, acts_as=acts_as)
        if turn is None:
            return None
        # THE claim payload — the one the REST route returns, credentials and
        # caller envelope included. This socket used to build its own subset,
        # and a confined turn arrived without the envelope the runner confines
        # by (apps/harness/claiming.py).
        from apps.harness.claiming import claim_payload

        return claim_payload(turn)

    @database_sync_to_async
    def _heartbeat(self, frame):
        """THE heartbeat the REST route records (`runner_protocol.heartbeat`), read
        off the frame field by field (`parse_heartbeat_frame`).

        This is the cloud runner's primary beat (every 20s), and
        `services.heartbeat` writes several fields every beat with absent =
        default (`degraded`/`note`, the code provenance, `profiles`/`envelope`,
        `ready`). A frame that a socket-side reader dropped a field from reset it
        every 20s: a degraded box read ONLINE, `code_sha` read empty, a confining
        box went 0/0. So the socket honours exactly the REST schema, and a
        malformed field costs that field, never the beat.
        """
        runner = Runner.objects.filter(pk=self._runner_pk).first()
        if runner is None:
            return False, [], {}
        runner_protocol.heartbeat(runner, runner_protocol.parse_heartbeat_frame(frame or {}))
        return (runner.refresh_pending(), list(getattr(runner, "cancel_turn_ids", None) or []),
                {"paused": bool(runner.paused), "paused_note": runner.paused_note or ""})

    def _turn_owned_sync(self, turn_id):
        """A turn THIS runner claimed — the only ones it may start/append/finish.
        Plain sync (called inside the database_sync_to_async handlers below)."""
        try:
            tid = uuid.UUID(str(turn_id))
        except (ValueError, TypeError):
            return None
        return (
            Turn.objects.select_related("agent", "chat_session")
            .filter(pk=tid, claimed_by_id=self._runner_pk)
            .first()
        )

    @database_sync_to_async
    def _start(self, turn_id, session_id):
        turn = self._turn_owned_sync(turn_id)
        if turn is None:
            return False
        harness_services.mark_running(turn, session_id=session_id)
        return True

    @database_sync_to_async
    def _append(self, turn_id, events):
        turn = self._turn_owned_sync(turn_id)
        if turn is None:
            return 0
        return ledger.append_events(turn, events)

    @database_sync_to_async
    def _finish(self, frame):
        """The REST finish (`runner_protocol.finish`): the same status check and
        session record. A refused frame answers `ok: false` with why; the socket
        stays up."""
        turn = self._turn_owned_sync(frame.get("turn_id"))
        if turn is None:
            return False, ""
        try:
            runner_protocol.finish(turn, runner_protocol.parse_finish_frame(frame))
        except runner_protocol.ProtocolError as exc:
            return False, exc.detail or exc.title
        return True, ""
