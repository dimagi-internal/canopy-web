"""The AG-UI cross-language fixture: the frames the Python projection and its TypeScript
inverse both read, and how it is built (`uv run python -m tests.regen_agui_fixture`).
"""
from __future__ import annotations

from pathlib import Path

from apps.canopy_sessions import agui

FIXTURE = Path(__file__).resolve().parents[2] / "frontend" / "packages" / \
    "canopy-ui" / "src" / "chat" / "agui.fixture.json"


ROUND_TRIP_FRAMES = [
    {"event": "chat.stream_start", "data": {"message_id": "m1", "turn_index": 4}},
    {"event": "chat.delta", "data": {"message_id": "m1", "text": "hello"}},
    {"event": "chat.stream_complete", "data": {"message_id": "m1", "plaintext": "hello"}},
    # Claude Code's own error notice: the flag is what keeps it from rendering
    # as the agent's reply, so it must survive the round trip.
    {"event": "chat.stream_complete",
     "data": {"message_id": "m2", "plaintext": "Not logged in · Please run /login",
              "api_error": "api_error", "signed_out": True}},
    {"event": "chat.user_message",
     "data": {"message_id": "u1", "turn_index": 3, "plaintext": "hi there",
              "author": {"name": "Jon", "user_id": 7}}},
    {"event": "chat.tool_use",
     "data": {"tool_message_id": "t9", "parent_message_id": "m1", "turn_index": 5,
              "block": {"name": "list_tasks", "input": {"limit": 5}}}},
    {"event": "chat.tool_result",
     "data": {"tool_message_id": "t9", "parent_message_id": "m1", "turn_index": 6,
              "block": {"type": "tool_result", "tool_use_id": "toolu_01ABC",
                        "content": "[]"}}},
    {"event": "session.title_updated", "data": {"title": "Inbox triage"}},
    {"event": "draft.updated", "data": {"id": "d1", "body": "x", "version": 2}},
    {"event": "draft.typing",
     "data": {"author": {"id": 7, "name": "Beth"}, "body": "wip", "at": "2026-09-26T12:00:00+00:00"}},
    {"event": "presence.joined", "data": {"user_id": 7}},
    {"event": "presence.left", "data": {"user_id": 7}},
    # --- every other frame a canopy client can receive ------------------------
    # Added 2026-09-18, when `test_every_frame_the_consumer_emits_is_in_the_
    # round_trip_fixture` arrived and found twelve frames that had never been
    # proven to survive the trip. Shapes copied from the producers
    # (`consumers.py`, `stream_map.py`, `page_actions.py`), not invented, so a
    # producer changing its payload shows up here as a stale example.
    {"event": "session.state",
     "data": {"messages": [
                  {"id": "41", "turn_index": 0, "role": "user", "content": {},
                   "plaintext": "what is stale?", "status": "complete",
                   "error_detail": None, "started_at": None, "completed_at": None,
                   "created_at": "2026-09-18T12:00:00+00:00"},
                  {"id": "42", "turn_index": 1, "role": "assistant", "content": {},
                   "plaintext": "Three items are.", "status": "complete",
                   "error_detail": None, "started_at": None, "completed_at": None,
                   "created_at": "2026-09-18T12:00:05+00:00"}],
              "active_draft": None,
              "participants": [{"user_id": 7, "role": "editor"}],
              "presence_user_ids": [7],
              "current_user_id": 7,
              "menu": None,
              "turn_status": {"state": "working", "agent_slug": "hal",
                              "runners": ["jj-mbp"], "claimed_by": "jj-mbp",
                              "pinned": False, "cloud_runner": None,
                              "cloud_runner_id": None, "last_seen_at": None,
                              "menu_pending": False, "settled": False,
                              "stuck": False}}},
    {"event": "session.page_action",
     "data": {"id": "9f1c", "name": "scrollToRow", "args": {"id": 4471}}},
    {"event": "session.turn_status",
     "data": {"status": {"state": "waiting_runner", "agent_slug": "hal",
                         "runners": ["jj-mbp"], "claimed_by": None,
                         "pinned": False, "cloud_runner": "cloud-1",
                         "cloud_runner_id": "8f2e", "last_seen_at": None,
                         "menu_pending": False, "settled": False,
                         "stuck": True}}},
    {"event": "session.queued",
     "data": {"queued": [{"turn_id": "8f2e", "client_id": "c9",
                          "author": {"name": "Jon", "user_id": 7},
                          "text": "from jj", "sent_at": "2026-09-26T12:00:00+00:00",
                          "state": "queued"}]}},
    {"event": "session.stop", "data": {"state": "failed"}},
    {"event": "session.activity", "data": {"state": "working"}},
    {"event": "session.menu",
     "data": {"menu": {"source": "hook", "question": "Proceed?", "observed_at": 1758196800,
                       "options": [{"label": "Yes"}, {"label": "No"}]}}},
    {"event": "session.error", "data": {"code": "draft_conflict", "message": "stale version"}},
    {"event": "chat.stream_cancelled", "data": {"message_id": "m1", "partial_len": 12}},
    {"event": "chat.stream_error", "data": {"message_id": "m1", "detail": "runner went away"}},
    {"event": "draft.committed", "data": {"draft_id": "d1", "user_message_id": "u2"}},
    {"event": "draft.discarded", "data": {"draft_id": "d1"}},
    {"event": "page.invalidate", "data": {"uri": "task://"}},
]


def _build_fixture() -> list[dict]:
    return [
        {"canopy": frame,
         # NO run id, because the live socket passes none
         # (`SessionConsumer.send_json`). This passed `run_id="r1"` until
         # 2026-09-18, so the round trip proved a path production never takes —
         # and missed that the path it does take renamed a cancelled stream to
         # an event the reducer does not know.
         "agui": [agui.encode(e) for e in agui.project(frame, thread_id="t1")]}
        for frame in ROUND_TRIP_FRAMES
    ]
