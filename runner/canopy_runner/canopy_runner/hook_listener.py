"""Loopback listener for Claude Code hook events.

Claude Code fires a hook per tool call; the hook `curl`s its JSON to this
listener, which maps it to a canopy Session and (when forwarding is on) ships it
as a live event. That gives the phone real-time tool activity, which transcript
tailing cannot: the Claude Code docs are explicit that the transcript "may lag
the in-memory conversation".

This is the same shape emdash already uses for its own hooks — a loopback POST
with a per-session nonce — so it is a pattern already proven on these machines
rather than a new one.

Three properties are load-bearing:

**It always answers 200, immediately.** A hook that errors or hangs is a hook
that can degrade the agent's own loop, so every failure path here still returns
success. Nothing observability does may cost an agent a turn.

**Forwarding is a switch, the listener is not.** `Config.forward_sessions` gates
only the POST to canopy-web. Off means events are accepted and dropped, so the
plumbing can be installed and watched before it carries anything.

**Live events are never persisted.** They carry `index = -1`, which the server
treats as view-only, so the durable record stays exactly one thing: the
transcript. That is what makes it safe for this path to drop events freely.
"""
from __future__ import annotations

import errno
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from canopy_transcript import (
    activity_for_hook,
    events_for_hook,
    hook_retires_menu,
    marker_from_hook,
    menu_from_hook,
)

# Mirrors `hooks.ANSWERED` — imported lazily there to keep this module free of
# the runner's CDP/emdash imports, which is what lets it unit-test standalone.
ANSWERED = "answered"
NO_DIALOG = "no_dialog"

logger = logging.getLogger("canopy_runner.hooks")

# A hook body is one tool call — the largest realistic one is a big tool result,
# and the payloads measured on a live session were ~800 bytes. This is a sanity
# bound against a runaway body, not a working limit.
MAX_BODY_BYTES = 4 * 1024 * 1024


class HookListener:
    """Owns the loopback server and the cwd -> session mapping.

    `resolve_session(cwd)` is injected rather than imported so this module stays
    testable without the runner's client, binding cache, or a live server.
    """

    def __init__(self, *, port: int, nonce: str, resolve_session, forward,
                 read_menu=None, resolve_task=None, menu_store=None):
        self.port = port
        self.nonce = nonce
        self._resolve_session = resolve_session
        self._forward = forward
        # Injected: cwd -> the (project, emdash task) keys the session report is
        # built on. Injected for the same reason `resolve_session` is — this
        # module stays testable with no emdash and no worktree on disk.
        #
        # Note it is NOT `resolve_session`: that one answers with a canopy session
        # id and can only do so for a session a viewer is attached to, which is
        # precisely the case a blocked-agent menu does not need.
        self._resolve_task = resolve_task
        # The live dialog per (project, task), captured from `PreToolUse`.
        # Written from listener threads and read from the report thread; dict
        # get/set on a str key is atomic under the GIL, and a menu arriving
        # one tick late is a non-event, so no lock.
        self._pending_menus: dict[tuple[str, str], dict] = {}
        # Where pending menus survive a restart. The hook that raised a dialog
        # fires exactly once, so without this a restart does not merely FORGET a
        # live menu — the next report ships `question: null` and actively RETIRES
        # it server-side, and nothing can ever rediscover it: no hook re-fires,
        # and the transcript will not carry the ask until it is answered. The
        # runner auto-updates itself, so restarts are routine, not rare.
        self._menu_store = menu_store
        # Sessions with a turn in flight: added on `UserPromptSubmit`, dropped on
        # `Stop`. This is what tells a real dialog apart from an idle prompt —
        # see `_track_menu`. Deliberately NOT persisted: after a restart we
        # genuinely do not know, and the safe answer there is silence.
        self._in_turn: set[tuple[str, str]] = set()
        if menu_store is not None:
            try:
                self._pending_menus.update(menu_store.load())
            except Exception:  # noqa: BLE001 — a bad store must not stop the listener
                logger.debug("could not restore pending menus", exc_info=True)
        # Injected: given a cwd, return the dialog on that session's screen (or
        # None). Injected rather than imported so this module stays testable
        # without CDP, emdash, or a live terminal — and so a runner with no CDP
        # simply reports "blocked" with no menu instead of failing.
        self._read_menu = read_menu
        self._server: ThreadingHTTPServer | None = None
        self.received = 0
        self.forwarded = 0
        self.dropped_unknown_cwd = 0

    # -- request handling (pure enough to unit-test directly) ----------------

    def handle_payload(self, payload: dict) -> str:
        """Process one hook payload. Returns a short outcome string, for logs
        and tests. Never raises — the caller must answer 200 regardless."""
        self.received += 1
        try:
            # BEFORE every gate below. A dialog is state this box holds for the
            # session report to read, not an event to ship — so it must survive
            # `forward_sessions` being off, and it must be recorded even for a
            # hook kind that forwards nothing.
            self._track_menu(payload)
            activity = activity_for_hook(payload)
            if activity is not None:
                cwd = payload.get("cwd") or ""
                session_id = self._resolve_session(cwd)
                if not session_id:
                    self.dropped_unknown_cwd += 1
                    return "unknown-cwd"
                if not self._forward():
                    return "not-forwarding"
                # A state transition, not a chat row: index -1 so it is never
                # persisted, and a dedicated kind the server maps to a status
                # frame rather than a message.
                #
                # "blocked" alone is not actionable away from the keyboard — it
                # says somebody is wanted, not what is being asked. Only on
                # `blocked` do we go and LOOK at the screen, because that read
                # costs a CDP round trip and this is the one state where the
                # agent has stopped and is waiting.
                payload_out: dict = {}
                if activity == "blocked" and self._read_menu is not None:
                    menu = self._safe_read_menu(cwd)
                    if menu is not None:
                        payload_out["menu"] = menu
                self._send(session_id, [{"kind": f"activity:{activity}",
                                         "seq": -1, "index": -1,
                                         "payload": payload_out}])
                self.forwarded += 1
                return f"activity:{activity}"
            events = events_for_hook(payload)
            if not events:
                return "ignored"          # not a forwarded event kind
            cwd = payload.get("cwd") or ""
            session_id = self._resolve_session(cwd)
            if not session_id:
                # Hooks are installed at USER level, so they fire for every
                # Claude Code session on this machine — including ones canopy
                # knows nothing about. Dropping them here is the expected path,
                # not an error.
                self.dropped_unknown_cwd += 1
                return "unknown-cwd"
            if not self._forward():
                return "not-forwarding"   # accepted and dropped, by configuration
            self._send(session_id, events)
            self.forwarded += 1
            return "forwarded"
        except Exception:  # noqa: BLE001 — a hook must never see a failure
            logger.debug("hook handling failed (non-fatal)", exc_info=True)
            return "error"

    def _track_menu(self, payload: dict) -> None:
        """Hold, or drop, the dialog this session is waiting on.

        This is the ONLY producer that sees a menu while it is still up. The
        transcript cannot: Claude Code writes the `AskUserQuestion` tool_use
        record when the dialog is ANSWERED, so `pending_question` is structurally
        blind to a live one (39 records across 60 transcripts on a live box, all
        already answered, zero pending — 2026-08-01). The screen reader can, but
        only by driving CDP, which clicks the task and steals focus, so it can
        never run on a cadence.
        """
        if self._resolve_task is None:
            return
        try:
            keys = self._resolve_task(payload.get("cwd") or "")
            if not keys:
                return
            menu = menu_from_hook(payload)
            retire = menu is None and hook_retires_menu(payload)
            # A `Notification` says a human is wanted but cannot say what is being
            # asked — no options, no command. It is recorded ONLY where nothing
            # better is already held: a real menu is strictly more useful, and
            # letting a notification overwrite one would replace buttons that work
            # with words that do not.
            # Turn state first — a Notification's meaning depends on it.
            event = payload.get("hook_event_name")
            if event == "UserPromptSubmit":
                self._in_turn.update(keys)
            elif event == "Stop":
                self._in_turn.difference_update(keys)

            # A `Notification` fires for two different things: an agent stopping
            # MID-TURN to ask a human, and a prompt simply sitting idle after a
            # turn ends. Only the first is somebody waiting on you. Treating both
            # as blocked put four merely-idle sessions on the fleet's "waiting on
            # you" list within minutes of shipping it (labs, 2026-08-01) — and a
            # badge that counts every idle session is a badge nobody reads.
            #
            # The discriminator is turn STATE, not the message text: an idle
            # notification arrives after `Stop`, a real one between
            # `UserPromptSubmit` and `Stop`. Parsing the wording would be
            # guessing at strings Claude Code is free to change; this is observed.
            #
            # Fails closed. If we never saw the turn start (a runner restarted
            # mid-turn), a real permission prompt is missed rather than a false
            # "blocked" raised — silence costs a menu, noise costs the signal.
            marker = None
            if menu is None and not retire and any(k in self._in_turn for k in keys):
                marker = marker_from_hook(payload)
            if menu is None and marker is None and not retire:
                return
            for key in keys:
                if menu is not None:
                    self._pending_menus[key] = menu
                elif retire:
                    self._pending_menus.pop(key, None)
                elif key not in self._pending_menus:
                    self._pending_menus[key] = marker
            self._persist()
        except Exception:  # noqa: BLE001 — a hook must never see a failure
            logger.debug("menu tracking failed (non-fatal)", exc_info=True)

    def pending_menu(self, project: str, task: str) -> dict | None:
        """The dialog this session is waiting on, or None."""
        return self._pending_menus.get((project or "", task or ""))

    def note_answer(self, keys, outcome: str, note: str = "", screen=None) -> None:
        """Reconcile the cached menu against what the terminal actually showed.

        **The rule, and it is the whole state model in one line:** a tap always
        reconciles the cache to the authority. The dialog lives on a terminal;
        everything else — this dict, the store, `RunnerBinding.pending_question`,
        the phone — is a copy. The tap is the one moment both are in hand, so it
        is where they are made to agree.

            could we read the screen?
              yes -> the screen wins. Clear if it showed nothing, REPLACE if it
                     showed a different dialog. Never keep a cache the authority
                     has just contradicted.
              no  -> keep the cache and say why we could not check.

        That is what makes a refusal useful rather than merely honest: tapping a
        dialog somebody already answered removes it from your phone instead of
        leaving the buttons sitting there, and tapping one that CHANGED shows you
        what it changed to, so the next tap is against truth.

        `answer_error` therefore only ever rides a menu we are keeping — the
        outcomes where we could not consult the screen. A cleared menu carries no
        note by construction: there is no object left to hang it on, and the
        client knows it just tapped.
        """
        for key in keys or ():
            if outcome in (ANSWERED, NO_DIALOG):
                # ANSWERED: the key landed, the dialog is gone. NO_DIALOG: the
                # screen says there is nothing there — which is authoritative,
                # so the cache is wrong and goes.
                self._pending_menus.pop(key, None)
            elif screen is not None:
                # The screen showed a DIFFERENT dialog. Show that one.
                self._pending_menus[key] = {**screen, "answer_note": note}
            else:
                menu = self._pending_menus.get(key)
                if menu is not None:
                    self._pending_menus[key] = {**menu, "answer_error": outcome,
                                                "answer_note": note}
        self._persist()

    def _persist(self) -> None:
        """Best-effort. A store that cannot be written costs a restart's menus,
        which is exactly the status quo — never a hook's 200."""
        if self._menu_store is None:
            return
        try:
            self._menu_store.save(self._pending_menus)
        except Exception:  # noqa: BLE001
            logger.debug("could not persist pending menus", exc_info=True)

    def _safe_read_menu(self, cwd: str):
        """The dialog on this session's screen, or None.

        Never raises and never blocks the report: reading the screen means
        driving emdash over CDP, which can be slow, wedged, or looking at
        another task. A menu we fail to read costs the phone its buttons — the
        "blocked" state still ships, and the terminal can still answer.
        """
        try:
            return self._read_menu(cwd)
        except Exception:  # noqa: BLE001 — observability may never cost a turn
            logger.debug("could not read the menu for %s", cwd, exc_info=True)
            return None

    def _send(self, session_id: str, events: list[dict]) -> None:
        raise NotImplementedError  # set by `bind_sender`

    def bind_sender(self, send) -> None:
        """Inject the transport (the runner's client) after construction."""
        self._send = send

    # -- server lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self.port <= 0 or self._server is not None:
            return
        listener = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                # Answer FIRST, work second: the hook's curl should never wait on
                # canopy-web, and its timeout should never become the agent's.
                token = self.headers.get("X-Canopy-Token", "")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(length, MAX_BODY_BYTES)) if length else b""
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                if token != listener.nonce:
                    logger.debug("hook rejected: bad nonce")
                    return
                try:
                    payload = json.loads(body or b"{}")
                except ValueError:
                    return
                if isinstance(payload, dict):
                    listener.handle_payload(payload)

            def log_message(self, *_args):  # silence BaseHTTPRequestHandler
                pass

        # Bound to 127.0.0.1 explicitly — never 0.0.0.0. This machine holds fleet
        # credentials; the listener must not be reachable off-box.
        #
        # The configured port is a PREFERENCE, not a contract: nothing outside
        # this process has to predict the number, because we WRITE it into
        # ~/.claude/settings.json ourselves. What it cannot be is machine-global.
        # Two accounts on one Mac each install their own settings.json pointing
        # at the same default 8787, so whichever runner starts first receives
        # BOTH accounts' hooks — and can only resolve its own emdash sessions,
        # so the other account's hooks are not merely lost but actively eaten as
        # "unknown-cwd". The loser used to log a warning and run with live events
        # off forever (2026-07-28 → 07-30: every restart of the second runner,
        # zero activity events forwarded, and the chat UI's "running" indicator
        # dead for every session that runner drove). Taking a free port instead
        # retires the whole collision class.
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            taken, self.port = self.port, self._server.server_address[1]
            logger.warning(
                "hook port %d is held by another process; listening on :%d instead "
                "(the hook config is rewritten to match)", taken, self.port)
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="hook-listener").start()
        logger.info("hook listener on 127.0.0.1:%d (forwarding=%s)",
                    self.port, self._forward())

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
