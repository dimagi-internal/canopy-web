"""Control-plane HTTP client. stdlib urllib; every call is short and synchronous."""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request

TIMEOUT = 10


class ClientError(Exception):
    pass


class Client:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        return self._call_api(f"/harness{path}", method=method, body=body, label=path)

    def _call_api(self, path: str, *, method: str, body: dict | None = None,
                  label: str = "") -> tuple[int, dict | None]:
        """Same transport as ``_call`` but rooted at ``/api`` rather than
        ``/api/harness`` — for the handful of runner calls that live in another
        app's namespace (the Gmail watch report is in ``apps/inbound``)."""
        label = label or path
        url = f"{self.base_url}/api{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read()[:200]
            except Exception:
                error_body = b"(could not read error body)"
            raise ClientError(f"{method} {label} -> {exc.code}: {error_body!r}") from exc
        except urllib.error.URLError as exc:
            raise ClientError(f"{method} {label} -> {exc.reason}") from exc
        if status == 204 or not raw:
            return status, None
        return status, json.loads(raw)

    def download_attachment(self, attachment_id: str, dest: "pathlib.Path") -> None:
        """Fetch a chat attachment's bytes to `dest`.

        NOT via _call: that one is rooted at /api/harness and json-decodes the
        body. Attachments live under /api/canopy-sessions and are raw bytes.
        """
        url = f"{self.base_url}/api/canopy-sessions/attachments/{attachment_id}/content"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ClientError(f"GET attachment {attachment_id} -> {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ClientError(f"GET attachment {attachment_id} -> {exc.reason}") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)

    def heartbeat(self, runner_id: str, active_turn_ids: list[str], degraded: bool = False,
                  note: str = "", host: str = "", ready: bool = True, ready_note: str = "",
                  code_branch: str | None = None, code_version: str | None = None,
                  code_sha: str | None = None, code_committed_at: int | None = None,
                  projects: list[str] | None = None) -> dict:
        """Report liveness. Code provenance is stamped HERE, not by callers.

        `services.heartbeat` assigns these unconditionally, so any heartbeat that
        omits one RESETS it server-side. There are six call sites in the loop
        (lease renewal, the CDP-down leg, drain-one, pause…) and four historically
        passed no branch at all — so the wrong-branch banner would blink off
        whenever one of those landed between two loop ticks. Defaulting to None
        and filling it in once, here, makes that unmissable rather than a
        convention every future call site has to remember.

        `projects` is the OPPOSITE contract, and deliberately so: the repos this
        box can drive, where None OMITS the field entirely and the server treats
        absence as "no report, keep the list you have". It cannot be defaulted
        here the way provenance is — only the caller knows whether it actually
        read emdash. Pass an empty LIST only when the box genuinely has none;
        never [] for a failed read, which would blank the list and make every
        repo turn on this runner unclaimable.
        """
        from . import provenance
        body = {"active_turn_ids": active_turn_ids, "degraded": degraded, "note": note,
                "host": host, "ready": ready, "ready_note": ready_note,
                "code_branch": provenance.code_branch() if code_branch is None else code_branch,
                "code_version": provenance.version() if code_version is None else code_version,
                "code_sha": provenance.code_sha() if code_sha is None else code_sha,
                "code_committed_at": (provenance.code_committed_at()
                                      if code_committed_at is None else code_committed_at)}
        if projects is not None:
            body["projects"] = projects
        _, payload = self._call("POST", f"/runners/{runner_id}/heartbeat", body)
        return payload or {}

    def set_paused(self, runner_id: str, paused: bool, note: str = "") -> dict:
        """Push a LOCAL pause change up as a command on the one shared state.

        Called only when the `~/.canopy/PAUSED` sentinel CHANGES, never on a level
        every tick: the server is the source of truth, and a runner re-asserting
        "not paused" every five seconds would silently lift any remote pause the
        moment it landed.
        """
        path = f"/runners/{runner_id}/{'pause' if paused else 'unpause'}"
        _, payload = self._call("POST", path, {"note": note} if paused else {})
        return payload or {}

    def list_runners(self) -> list[dict]:
        """The fleet this token can see. READ-ONLY, and the only call the updater
        makes: it needs `expected_code_sha` off its own row, and must never
        heartbeat (that would stamp the runner ONLINE and overwrite the provenance
        the real daemon reports — forging liveness for a daemon that may be dead)."""
        _, payload = self._call("GET", "/runners/")
        return payload if isinstance(payload, list) else []

    def resolve_session(self, runner_id: str, agent_slug: str, thread_key: str, *,
                        project: str = "", workspace: str = "") -> dict:
        """Ask the control plane whether THIS runner can reuse an existing emdash
        session for (target, thread) or must spawn fresh + rehydrate. See SessionLink.

        Pass EITHER agent_slug OR (project + workspace) — a project session is
        tenant-gated on its workspace, which the turn carries."""
        _, payload = self._call(
            "POST", f"/runners/{runner_id}/resolve-session",
            {"agent_slug": agent_slug, "project": project, "workspace": workspace,
             "thread_key": thread_key},
        )
        return payload or {}

    def record_session(self, runner_id: str, agent_slug: str, thread_key: str, *,
                       project: str = "", workspace: str = "",
                       emdash_task_id: str = "", session_id: str = "",
                       agent_task_ext_id: str | None = None, summary: str | None = None) -> dict:
        """Record/point the durable thread link at THIS runner's live session."""
        _, payload = self._call(
            "POST", f"/runners/{runner_id}/record-session",
            {"agent_slug": agent_slug, "project": project, "workspace": workspace,
             "thread_key": thread_key,
             "emdash_task_id": emdash_task_id, "session_id": session_id,
             "agent_task_ext_id": agent_task_ext_id, "summary": summary},
        )
        return payload or {}

    def report_sessions(
        self, runner_id: str, sessions: list[dict], archived: list[str] | None = None
    ) -> None:
        """Report the open emdash sessions this runner can see (wholesale), plus the
        task names it has seen ARCHIVED — the closing signal that lets the server
        retire a session instead of inferring it from absence."""
        self._call(
            "POST",
            f"/runners/{runner_id}/sessions",
            {"sessions": sessions, "archived": archived or []},
        )

    def sync_streams(self, runner_id: str) -> list[dict]:
        """The sessions a viewer is watching, which this runner should tail live."""
        _, payload = self._call("GET", f"/runners/{runner_id}/streams")
        return (payload or {}).get("streams", [])

    def post_session_stream(self, runner_id: str, session_id: str, events: list[dict]) -> None:
        """Ship live assistant events for a session this runner backs."""
        self._call("POST", f"/runners/{runner_id}/session-stream",
                   {"session_id": session_id, "events": events})

    def sync_closes(self, runner_id: str) -> list[dict]:
        """Sessions we have been asked to close and have not closed yet."""
        _, payload = self._call("GET", f"/runners/{runner_id}/closes")
        return (payload or {}).get("closes", [])

    def sync_menu_answers(self, runner_id: str) -> list[dict]:
        """Answers a human has given that we have not pressed yet."""
        _, payload = self._call("GET", f"/runners/{runner_id}/menu-answers")
        return (payload or {}).get("answers", [])

    def post_menu_answer_result(self, runner_id: str, session_id: str,
                                answer_id: str, outcome: str) -> None:
        """Retire an answer we have acted on. Echoing the id back is what stops a
        stale result clearing a NEWER answer — which would drop a real tap."""
        self._call("POST", f"/runners/{runner_id}/menu-answer-result",
                   {"session_id": session_id, "answer_id": answer_id, "outcome": outcome})

    def sync_backfills(self, runner_id: str) -> list[dict]:
        """Sessions the server asked this runner to ship full history for."""
        _, payload = self._call("GET", f"/runners/{runner_id}/backfills")
        return (payload or {}).get("backfills", [])

    def post_session_backfill(self, runner_id: str, session_id: str, messages: list[dict],
                              final: bool = True) -> None:
        """Ship a chunk of a session's transcript for the server to write as Message
        rows. `final=False` means more chunks follow, so the server keeps the ask
        set — see streams.chunk_rows for why one request is not enough."""
        self._call("POST", f"/runners/{runner_id}/session-backfill",
                   {"session_id": session_id, "messages": messages, "final": final})

    def claim(self, runner_id: str, paused_agents: list[str] | None = None) -> dict | None:
        # paused_agents (per-agent pause) → server skips those agents' queued turns.
        path = f"/runners/{runner_id}/claim"
        if paused_agents:
            from urllib.parse import urlencode
            path += "?" + urlencode({"paused": ",".join(sorted(paused_agents))})
        status, payload = self._call("POST", path)
        return payload if status == 200 else None

    def post_events(self, turn_id: str, events: list[dict]) -> None:
        self._call("POST", f"/turns/{turn_id}/events", {"events": events})

    def post_transcript(self, turn_id: str, lines: list[str], batch_id: str = "") -> bool:
        """Append raw JSONL to a turn's retained transcript. Returns False once the
        server reports the turn's per-turn ceiling is hit, so the caller can stop
        flushing for this turn (every later batch would be a silent no-op)."""
        _, payload = self._call(
            "POST", f"/turns/{turn_id}/transcript",
            {"lines": lines, "batch_id": batch_id},
        )
        return not (payload or {}).get("truncated", False)

    def sync_schedules(self, runner_id: str) -> list[dict]:
        """The schedules this runner may fire (tenant-scoped server-side). The runner
        evaluates their cron locally and reports what came due — the server stores the
        config, the runner is the tick. Response is a Page; callers want the items."""
        from urllib.parse import urlencode
        _, payload = self._call("GET", "/schedules/?" + urlencode({"runner_id": runner_id}))
        return (payload or {}).get("items", [])

    def fire_schedule(self, schedule_id: int, runner_id: str, slot: str) -> dict:
        """Report a due slot; the server materializes it as a normal turn.

        Safe to race — both macOS-account runners may report the same slot, and the
        server's slot-derived idempotency_key collapses it inside enqueue_turn. The
        route answers 201 either way, so a fresh turn and a replay are indistinguishable
        here (and both are success — there is nothing for the runner to reconcile).
        """
        from urllib.parse import urlencode
        path = f"/schedules/{schedule_id}/fire?" + urlencode({"runner_id": runner_id})
        _, payload = self._call("POST", path, {"slot": slot})
        return payload or {}

    def enqueue_turn(self, agent_slug: str, origin: str, idempotency_key: str, *,
                     prompt: str = "", origin_ref: dict | None = None,
                     routing: str = "prefer_local") -> dict:
        """Enqueue a turn (idempotent on idempotency_key — safe to re-enqueue the same
        email). Used by the deterministic inbox/slack triggers."""
        status, payload = self._call("POST", "/turns/", {
            "agent_slug": agent_slug, "origin": origin, "idempotency_key": idempotency_key,
            "prompt": prompt, "origin_ref": origin_ref or {}, "routing": routing,
        })
        # 201 = a NEW turn; 200 = idempotent hit on one we already enqueued. Callers log
        # the difference so a re-poll of the same unread mail reads as "nothing new".
        return {**(payload or {}), "_created": status == 201}

    def runner_mailboxes(self) -> list[dict]:
        """[{address, watch_topic}] — which mailboxes to arm, and on which topic.

        Served from each workspace's InboundPushConfig so a tenant sets its topic
        in the UI once, instead of someone hand-editing runner.json on every box.
        """
        _status, payload = self._call_api("/inbound/runner-mailboxes", method="GET")
        return (payload or {}).get("items", []) if isinstance(payload, dict) else []

    def report_watch(self, address: str, expires_at) -> dict:
        """Tell canopy-web when this mailbox's Gmail watch lapses.

        Lives under /api/inbound, not /api/harness — hence _call_api. Reporting
        is what turns the 7-day expiry from a silent cliff into a warn/error row
        (apps/inbound.services.note_watch_state).
        """
        _status, payload = self._call_api(
            "/inbound/watch/",
            method="POST",
            body={"address": address,
                  "expires_at": expires_at.isoformat() if expires_at else None},
        )
        return payload or {}

    def start(self, turn_id: str, session_id: str = "") -> None:
        self._call("POST", f"/turns/{turn_id}/start", {"session_id": session_id})

    def finish(self, turn_id: str, note: str = "", status: str = "done") -> None:
        self._call("POST", f"/turns/{turn_id}/finish", {"status": status, "result_note": note})

    def fail_turn(self, turn_id: str, note: str) -> None:
        self._call("POST", f"/turns/{turn_id}/finish", {"status": "failed", "result_note": note})

    def get_turn(self, turn_id: str) -> dict:
        _, payload = self._call("GET", f"/turns/{turn_id}")
        return payload or {}
