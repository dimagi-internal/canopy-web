"""An unconfirmed send is RECOVERED when a look at the session proves how — and
never typed into a session that might already hold it.

The incident (2026-10-07): a Slack message to ACE was typed into an existing emdash
session while Jonathan was using the machine. Nothing reached the transcript within
the confirm window and the turn failed with "Couldn't confirm…". The likely causes
— a click stealing focus between the sidecar's focus and its insert, or a swallowed
Enter — leave the message recoverable and leave evidence in the session: an empty
composer, or our text still sitting in it.

Retyping is the dangerous move (turn 22662f53 typed one message four times), so
these pin each branch of the ladder, and above all the ones that must NOT type:
a late transcript record, a human's text in the composer, our text already on
screen, an unreadable frame.
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import cdp_control, chat_bridge, delivery, emdash, execute


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(delivery, "CONFIRM_TIMEOUT", 0.2)
    monkeypatch.setattr(delivery, "CONFIRM_POLL", 0.02)
    monkeypatch.setattr(delivery, "UNMATCHED_GRACE", 0.1)
    monkeypatch.setattr(execute, "RECOVERY_CONFIRM_TIMEOUT", 0.2)
    monkeypatch.setattr(emdash, "task_state", lambda *a, **k: "live")
    chat_bridge.IN_FLIGHT.clear()
    yield
    chat_bridge.IN_FLIGHT.clear()


PROMPT = "can you pull the latest Kenya numbers into the deck before Thursday?"
MULTI = "here is the screenshot\n\nThe user attached the following file:\n- /tmp/x.png"


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}, "origin": {"kind": "human"}}


def _state(found=True, typed="", screen="", running=False):
    return {"found": found, "text": typed, "typed": typed, "screen": screen, "running": running}


# ── the decision, one look at a time ───────────────────────────────────────

def test_our_text_unsent_in_the_composer_is_submitted():
    assert delivery.recovery_action(_state(typed=PROMPT), PROMPT)[0] == delivery.SUBMIT


def test_a_wrapped_slice_of_our_text_is_still_ours():
    # The composer wraps over rows; the sidecar joins them with spaces.
    typed = "can you pull the latest Kenya   numbers into the deck"
    assert delivery.recovery_action(_state(typed=typed), PROMPT)[0] == delivery.SUBMIT


def test_a_collapsed_paste_of_a_multiline_message_is_ours():
    state = _state(typed="[Pasted text #1 +3 lines]")
    assert delivery.recovery_action(state, MULTI)[0] == delivery.SUBMIT


def test_a_paste_placeholder_is_not_ours_when_our_message_is_one_line():
    state = _state(typed="[Pasted text #1 +3 lines]")
    assert delivery.recovery_action(state, PROMPT) == (delivery.LEAVE, "composer_holds_other_text")


def test_a_humans_text_in_the_composer_is_left_alone():
    state = _state(typed="wait, before that, check the")
    assert delivery.recovery_action(state, PROMPT) == (delivery.LEAVE, "composer_holds_other_text")


def test_a_short_word_of_ours_is_not_enough_to_claim_the_composer():
    # "the" is in our message, and in everything a human types.
    assert delivery.recovery_action(_state(typed="the"), PROMPT)[0] == delivery.LEAVE


def test_our_text_with_a_humans_words_after_it_is_left_alone():
    # Enter would send their half-thought along with ours.
    state = _state(typed=PROMPT + " also ignore that")
    assert delivery.recovery_action(state, PROMPT)[0] == delivery.LEAVE


def test_an_unreadable_frame_is_left_alone():
    state = _state(found=False)
    assert delivery.recovery_action(state, PROMPT) == (delivery.LEAVE, "composer_not_visible")


def test_our_text_already_on_screen_counts_as_delivered():
    # Rendered in scrollback, wrapped and indented by the TUI.
    screen = ("⏺ earlier answer\n\n> can you pull the latest Kenya numbers into the\n"
              "  deck before Thursday?\n\n✻ Thinking…\n" + "─" * 40 + "\n❯ \n")
    assert delivery.recovery_action(_state(screen=screen), PROMPT)[0] == delivery.SEEN


def test_an_empty_composer_in_a_busy_session_is_left_alone():
    state = _state(screen="⏺ working\n esc to interrupt", running=True)
    assert delivery.recovery_action(state, PROMPT) == (delivery.LEAVE, "session_busy")


def test_a_paste_on_screen_might_be_ours_so_it_is_left_alone():
    state = _state(screen="> [Pasted text #1 +3 lines]\n⏺ ok")
    assert delivery.recovery_action(state, MULTI) == (delivery.LEAVE, "pasted_text_on_screen")


def test_an_empty_idle_composer_with_no_trace_is_retyped():
    state = _state(typed="Try \"fix lint errors\"", screen="⏺ earlier answer\n❯ ")
    assert delivery.recovery_action(state, PROMPT) == (delivery.RETYPE, "no_trace")


# ── the ladder, wired through the chat reuse path (the incident's path) ─────

class _Client:
    def __init__(self):
        self.events, self.failed, self.finished = [], None, None
        self.finished_status, self.finished_task = None, None

    def resolve_session(self, *a, **k):
        return {"reuse": True, "emdash_task_id": "c-you-were-just-brought-into-an-8202",
                "summary": ""}

    def start(self, *a, **k):
        pass

    def record_session(self, *a, **k):
        pass

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done", emdash_task_id=""):
        self.finished_status, self.finished_task = status, emdash_task_id
        if status == "failed":
            self.failed = note
        else:
            self.finished = note

    def fail_turn(self, turn_id, note):
        self.failed = note

    def statuses(self):
        return [e["payload"].get("status") for e in self.events]

    def retries(self):
        return [(e["payload"]["action"], e["payload"]["reason"]) for e in self.events
                if e["payload"].get("status") == "delivery_retry"]


def _cfg():
    tmp = Path(tempfile.mkdtemp(prefix="canopy-runner-test-"))
    return SimpleNamespace(cdp_port=9222, runner_id="r-1", emdash_db="/fake/emdash.db",
                           state_path=str(tmp / "runner-state.json"))


def _chat_turn(prompt=PROMPT):
    return {"id": "t1", "agent_slug": "ace", "project": "", "workspace_slug": "connect",
            "prompt": prompt, "origin_ref": {"chat_session_id": "s1", "thread_key": "s1"}}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    path = tmp_path / "sess.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"content": "earlier reply",
                                                                 "stop_reason": "end_turn"}}) + "\n")
    monkeypatch.setattr(execute, "_resolve_transcript_path", lambda *a, **k: path)
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: path)
    return path


def _land(path, text=PROMPT):
    with open(path, "a") as f:
        f.write(json.dumps(_user(text)) + "\n")


class _Session:
    """The emdash session, faked at the cdp_control seam. The FIRST send is lost
    (the incident); what later calls do is set per test."""

    def __init__(self, monkeypatch, transcript, *, looks, retype_lands=False, enter_lands=False):
        self.sends, self.keys, self.reads = [], [], 0
        self.transcript = transcript
        self.looks = list(looks)

        def open_and_send(task, text, clear_first=False, port=9222, project=""):
            self.sends.append((task, project))
            if len(self.sends) > 1 and retype_lands:
                _land(transcript, text)
            return {"ok": True, "action": "sent", "task": task}

        def send_keys(task, keys, port=9222, project=""):
            self.keys.append((task, tuple(keys), project))
            if enter_lands:
                _land(transcript)
            return {"ok": True, "sent": len(keys)}

        def read_composer(task, port=9222, project=""):
            self.reads += 1
            look = self.looks[min(self.reads, len(self.looks)) - 1]
            return look(self) if callable(look) else look

        monkeypatch.setattr(cdp_control, "open_and_send", open_and_send)
        monkeypatch.setattr(cdp_control, "send_keys", send_keys)
        monkeypatch.setattr(cdp_control, "read_composer", read_composer)


def test_a_swallowed_enter_is_pressed_again_and_the_turn_goes_on(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state(typed=PROMPT)], enter_lands=True)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out.startswith("chat:t1")
    assert client.failed is None
    assert len(s.sends) == 1, "Enter only — never a second copy"
    assert s.keys == [("c-you-were-just-brought-into-an-8202", ("Enter",), "ace")]
    assert client.retries() == [("submit", "composer_holds_message")]
    assert "delivery_recovered" in client.statuses()
    assert "undelivered" not in client.statuses()


def test_a_message_on_screen_is_believed_and_never_retyped(monkeypatch, transcript):
    screen = "> " + PROMPT + "\n⏺ Looking at the deck now\n" + "─" * 40 + "\n❯ \n"
    s = _Session(monkeypatch, transcript, looks=[_state(screen=screen)])
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out.startswith("chat:t1")
    assert client.failed is None
    assert len(s.sends) == 1 and s.keys == []
    assert "delivery_seen_on_screen" in client.statuses()


def test_a_diverted_message_is_retyped_into_the_empty_composer(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state(screen="⏺ earlier\n❯ ")],
                 retype_lands=True)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out.startswith("chat:t1")
    assert client.failed is None
    assert len(s.sends) == 2
    assert s.sends[1] == ("c-you-were-just-brought-into-an-8202", "ace"), "aimed by project"
    assert client.retries() == [("retype", "no_trace")]
    assert "delivery_recovered" in client.statuses()


def test_a_late_original_found_before_the_retype_is_not_retyped(monkeypatch, transcript):
    """The original lands while we are looking at the screen. The transcript is read
    once more right before typing, and that record stops the retype."""
    def look_while_it_lands(session):
        _land(session.transcript)
        return _state(screen="⏺ earlier\n❯ ")
    s = _Session(monkeypatch, transcript, looks=[look_while_it_lands], retype_lands=True)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out.startswith("chat:t1")
    assert client.failed is None
    assert len(s.sends) == 1, "the original arrived — a retype would duplicate it"
    assert "delivery_late" in client.statuses()


def test_a_late_original_found_before_the_first_look_needs_no_look(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state()])
    real_confirm = delivery.confirm

    def confirm_then_land(reader, prompt, **k):
        verdict = real_confirm(reader, prompt, **k)
        _land(transcript)                       # lands just after the window closed
        return verdict
    monkeypatch.setattr(delivery, "confirm", confirm_then_land)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out.startswith("chat:t1")
    assert s.reads == 0 and len(s.sends) == 1
    assert "delivery_late" in client.statuses()


def test_a_humans_text_in_the_composer_is_never_touched(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state(typed="hold on, I'm typing")],
                 retype_lands=True, enter_lands=True)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out == "failed:t1"
    assert len(s.sends) == 1 and s.keys == []
    assert client.retries() == [("leave", "composer_holds_other_text")]
    assert "Couldn't confirm" in client.failed


def test_an_unreadable_composer_fails_as_before(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state(found=False)], retype_lands=True)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out == "failed:t1"
    assert len(s.sends) == 1 and s.keys == []
    assert "Couldn't confirm" in client.failed


def test_a_human_typing_between_the_look_and_the_retype_is_not_clobbered(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state()])

    def send(task, text, clear_first=False, port=9222, project=""):
        s.sends.append((task, project))
        if len(s.sends) == 1:
            return {"ok": True, "action": "sent", "task": task}
        return {"ok": True, "action": "collision", "task": task, "line": "hey"}
    monkeypatch.setattr(cdp_control, "open_and_send", send)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out == "failed:t1"
    assert ("leave", "collision_on_retype") in client.retries()


def test_exhausted_retypes_fail_with_the_existing_note(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state()])     # never lands
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out == "failed:t1"
    assert len(s.sends) == 1 + execute.RECOVERY_ATTEMPTS
    assert client.retries() == [("retype", "no_trace")] * execute.RECOVERY_ATTEMPTS
    assert client.failed == execute._not_received_note("c-you-were-just-brought-into-an-8202")
    # Failed WITH its session, so the server does not requeue it as a non-attempt.
    assert client.finished_status == "failed"
    assert client.finished_task == "c-you-were-just-brought-into-an-8202"
    assert "undelivered" in client.statuses()
    assert "t1" not in chat_bridge.IN_FLIGHT


def test_exhausted_enters_fail_without_ever_retyping(monkeypatch, transcript):
    s = _Session(monkeypatch, transcript, looks=[_state(typed=PROMPT)])
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out == "failed:t1"
    assert len(s.sends) == 1
    assert len(s.keys) == execute.RECOVERY_ATTEMPTS
    assert "Couldn't confirm" in client.failed


def test_a_reclaimed_turn_is_still_never_typed_again(monkeypatch, transcript):
    """Recovery lives inside ONE claim. If the turn comes back (a requeue, a lease
    reclaim), _TYPED_TURNS still refuses it — no send, no look, no Enter."""
    s = _Session(monkeypatch, transcript, looks=[_state()])
    first, again = _Client(), _Client()

    execute.execute_chat_turn(_cfg(), first, "r-1", _chat_turn())
    typed_once = len(s.sends)
    reads_once = s.reads
    out = execute.execute_chat_turn(_cfg(), again, "r-1", _chat_turn())

    assert out == "failed:t1"
    assert len(s.sends) == typed_once and s.reads == reads_once and s.keys == []
    assert "not_retyped" in again.statuses()


# ── the agent-turn reuse path gets the same ladder ──────────────────────────

def test_an_agent_turn_recovers_a_swallowed_enter(monkeypatch, transcript):
    prompt = "/ace:turn --thread x"
    s = _Session(monkeypatch, transcript, looks=[_state(typed=prompt)])

    def send_keys(task, keys, port=9222, project=""):
        s.keys.append((task, tuple(keys), project))
        _land(transcript, prompt)
        return {"ok": True}
    monkeypatch.setattr(cdp_control, "send_keys", send_keys)
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: pytest.fail("never duplicate"))
    client = _Client()

    out = execute.execute_turn(_cfg(), client, "r-1",
                               {"id": "t2", "agent_slug": "ace", "origin_ref": {"thread_id": "x"},
                                "prompt": prompt})

    assert out == "reused:t2"
    assert len(s.sends) == 1 and len(s.keys) == 1
    assert client.failed is None
