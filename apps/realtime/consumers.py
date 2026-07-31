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
from django.db.models import Q

from apps.harness import services as harness_services
from apps.harness.models import Runner, Turn
from apps.workspaces.services import user_workspace_slugs

from . import groups


def _serialize_turn(turn: Turn) -> dict:
    """The fields a runner needs to run a claimed turn, over the WS.

    Deliberately a subset of `schemas.TurnOut` (the REST claim's shape) — a
    runner does not need a turn's timestamps or lease — but every field it DOES
    carry must AGREE with TurnOut, and be derived the same way. This function
    previously re-implemented that derivation from the columns alone:

        target = turn.agent.slug if turn.agent_id else turn.project

    A SESSION turn has `agent_id` NULL and `project` "" — its agent and repo
    hang off `chat_session` (see Turn.target, which already handles all three
    shapes). So every session turn claimed over the socket reported `target=""`
    and carried no `origin_ref` at all. Downstream, in the cloud runner:

      * `_chat_session_id()` returned "" -> `_turn_cwd` fell through to a
        PER-TURN scratch dir. Claude Code resolves a session id by cwd path, so
        every turn of a conversation cold-started — the exact amnesia
        `--resume` continuity exists to remove.
      * `_ship_transcript_rows()` returned early -> the conversation was never
        written as durable Message rows. Observed live 2026-07-28: a turn ran to
        `done`, retained its raw transcript, and left a session with zero
        messages.

    The REST claim path was always correct because it serializes TurnOut, which
    is why only the WS-preferring cloud runner hit this. Read the values off the
    model's own properties rather than restating the rules a third time.
    """
    cs = turn.chat_session if turn.chat_session_id else None
    return {
        "id": str(turn.id),
        "prompt": turn.prompt,
        # Turn.target is THE definition — agent slug, else session's agent slug,
        # else session's project, else the repo. Never restate it here.
        "target": turn.target,
        "agent_slug": (
            turn.agent.slug if turn.agent_id
            else (cs.agent.slug if cs is not None and cs.agent_id else None)
        ),
        "project": turn.project or (cs.project if cs is not None else ""),
        "routing": turn.routing,
        # The routing source (spec 2026-07-27) — a runner that logs or reports
        # what it claimed should be able to say what KIND of work it was.
        "origin": turn.origin,
        # THE identity of a conversation: origin_ref.chat_session_id. Its absence
        # is what made a session turn unrecognizable as one. Never drop it.
        "origin_ref": turn.origin_ref or {},
        "workspace_slug": (
            turn.agent.workspace_id if turn.agent_id
            else (cs.workspace_id if cs is not None else turn.workspace_id)
        ),
    }


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
    user's workspaces (the fleet is cross-tenant, like /insights)."""

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
    connect only to a runner it owns (paired_by == user, or null — legacy-ungated,
    matching the REST _runner_visibility_q). Joins the per-runner group + its
    workspaces' runnable groups, so:
      - server → runner: a `wake` frame when a turn becomes claimable (enqueue
        publishes to runnable.{ws}); the runner responds by claiming.
      - runner → server: `claim` / `heartbeat` frames that call the SAME harness
        services the REST routes call, so the two surfaces can't drift.

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

    async def runner_interject(self, message):
        # runner.{id} group_send type="runner.interject" — a human message for a
        # turn this runner is running. Pushed down so the live agent sees it.
        await self.send_json({
            "type": "interject",
            "turn_id": message.get("turn_id"),
            "session_id": message.get("session_id"),
            "message": message.get("message"),
        })

    async def runner_cancel(self, message):
        # runner.{id} group_send type="runner.cancel" — the user asked to stop a turn
        # this runner is executing. Forwarded down the socket; the executor checks its
        # cancel set every bridge poll (~0.5s), interrupts emdash, finishes CANCELLED.
        await self.send_json({"type": "cancel", "turn_id": message.get("turn_id")})

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
        await self.send_json({
            "type": "menu_answer",
            "session_id": message.get("session_id"),
            "session_key": message.get("session_key"),
            "option": message.get("option"),
        })

    async def receive_json(self, content, **kwargs):
        action = content.get("action")
        if action == "claim":
            turn = await self._claim()
            await self.send_json({"type": "claim.result", "turn": turn})
        elif action == "heartbeat":
            await self._heartbeat(content.get("active_turn_ids") or [], content)
            await self.send_json({"type": "heartbeat.ack"})
        elif action == "start":
            ok = await self._start(content.get("turn_id"), content.get("session_id") or "")
            await self.send_json({"type": "start.ack", "ok": ok})
        elif action == "event":
            n = await self._append(content.get("turn_id"), content.get("events") or [])
            await self.send_json({"type": "event.ack", "count": n})
        elif action == "finish":
            ok = await self._finish(
                content.get("turn_id"),
                content.get("status") or Turn.DONE,
                content.get("result_note") or "",
            )
            await self.send_json({"type": "finish.ack", "ok": ok})
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
            .filter(Q(paired_by=user) | Q(paired_by__isnull=True))
            .first()
        )

    @database_sync_to_async
    def _runner_workspaces(self, runner):
        if not runner.paired_by_id:
            return []
        return sorted(user_workspace_slugs(runner.paired_by))

    @database_sync_to_async
    def _claim(self):
        runner = Runner.objects.filter(pk=self._runner_pk).first()
        if runner is None:
            return None
        turn = harness_services.claim_next_turn(runner)
        return _serialize_turn(turn) if turn else None

    @database_sync_to_async
    def _heartbeat(self, active_turn_ids, frame=None):
        """Provenance rides this frame, and it HAS to.

        `services.heartbeat` assigns code_branch/code_version/code_sha/
        code_committed_at unconditionally, so a call that omits them RESETS them.
        This path is the cloud runner's primary heartbeat (every 20s), so without
        the pass-through anything it reports over REST — at pairing, or from a
        lease-renewal beat — is erased moments later, and `code_sha` reads empty
        forever while looking exactly like a runner that never reported one.

        Same failure the laptop's `provenance.py` docstring records from the other
        side: six call sites, four silently clearing the field. The fix there was
        one stamping point; here it is not dropping what the stamp sent.

        A runner too old to send these simply sends nothing, which resets to empty
        — i.e. UNKNOWN, which is what an unknown-provenance runner should report,
        and what this path already did for everyone.
        """
        frame = frame or {}
        try:
            committed_at = int(frame.get("code_committed_at") or 0)
        except (TypeError, ValueError):
            # Provenance is decoration on a liveness call. A malformed value must
            # cost the runner its timestamp, never its heartbeat — losing the beat
            # would take it offline and stop it claiming.
            committed_at = 0
        runner = Runner.objects.filter(pk=self._runner_pk).first()
        if runner is not None:
            harness_services.heartbeat(
                runner,
                active_turn_ids=active_turn_ids,
                code_branch=str(frame.get("code_branch") or ""),
                code_version=str(frame.get("code_version") or ""),
                code_sha=str(frame.get("code_sha") or ""),
                code_committed_at=committed_at,
            )

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
        return harness_services.append_events(turn, events)

    @database_sync_to_async
    def _finish(self, turn_id, status, result_note):
        turn = self._turn_owned_sync(turn_id)
        if turn is None:
            return False
        harness_services.finish_turn(turn, status=status, result_note=result_note)
        return True
