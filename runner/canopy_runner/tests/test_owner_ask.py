"""A caller's session's `For <Agent>'s owner:` line reaches its turn (canopy-web#1416)."""
from __future__ import annotations

import json

from canopy_runner import hooks
from canopy_runner.hook_listener import HookListener


class Client:
    def __init__(self):
        self.events = []

    def post_events(self, turn_id, events):
        self.events.append((turn_id, events))


def _stop(text):
    return {"hook_event_name": "Stop", "cwd": "/w/hal/cx-hal-vikram", "last_assistant_message": text}


def test_a_callers_stop_sends_its_owner_line_to_the_turn_its_profile_names(tmp_path):
    (tmp_path / "cx-hal-vikram.json").write_text(json.dumps({"turn_id": "turn-1"}))
    client = Client()
    hooks.send_owner_ask(client, _stop("Done.\nFor Hal's owner: add vkumar@ to callers"),
                         [("hal", "cx-hal-vikram")], profile_root=tmp_path, background=False)
    assert client.events == [("turn-1", [{"kind": "owner_ask",
                                          "payload": {"line": "For Hal's owner: add vkumar@ to callers"}}])]


def test_nothing_is_sent_for_an_owners_session_or_a_reply_with_no_line(tmp_path):
    (tmp_path / "cx-hal-vikram.json").write_text(json.dumps({"turn_id": "turn-1"}))
    client = Client()
    hooks.send_owner_ask(client, _stop("For Hal's owner: x"), [("hal", "hal-canopy-web-chat")],
                         profile_root=tmp_path, background=False)
    hooks.send_owner_ask(client, _stop("Just an answer."), [("hal", "cx-hal-vikram")],
                         profile_root=tmp_path, background=False)
    hooks.send_owner_ask(client, _stop("For Hal's owner: x"), [("hal", "cx-hal-unprofiled")],
                         profile_root=tmp_path, background=False)
    assert client.events == []


def test_the_listener_hands_a_stop_and_its_keys_over_and_nothing_else():
    seen = []
    lis = HookListener(port=0, nonce="n", resolve_session=lambda c: None, forward=lambda: False,
                       resolve_task=lambda cwd: [("hal", "cx-hal-vikram")],
                       on_stop=lambda payload, keys: seen.append((payload["hook_event_name"], keys)))
    lis.handle_payload({"hook_event_name": "UserPromptSubmit", "cwd": "/w"})
    lis.handle_payload({"hook_event_name": "Stop", "cwd": "/w"})
    assert seen == [("Stop", [("hal", "cx-hal-vikram")])]
    # A callback that raises never reaches the hook.
    boom = HookListener(port=0, nonce="n", resolve_session=lambda c: None, forward=lambda: False,
                        resolve_task=lambda cwd: [("hal", "cx-x")],
                        on_stop=lambda p, k: 1 / 0)
    assert boom.handle_payload({"hook_event_name": "Stop", "cwd": "/w"}) != "error"
