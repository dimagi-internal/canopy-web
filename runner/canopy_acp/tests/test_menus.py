"""ACP permission requests -> the menu payload the phone already renders.

The fixture is the VERBATIM `session/request_permission` params captured from
claude-agent-acp 0.63.0 on 2026-07-28 (spike4), so this pins the real contract
rather than a guess at it.

The point of these tests is convergence: what comes out here must be
interchangeable with what the terminal parser produces on the laptop, or the
client needs two renderers and the two paths drift.
"""
import pytest

from canopy_acp.menus import (
    elicitation_answer,
    menu_from_elicitation,
    menu_from_permission_request,
    option_id_for,
)
from canopy_transcript.questions import menu_from_hook

LIVE_REQUEST = {
    "options": [
        {"kind": "allow_always",
         "name": "Always Allow Bash(rm -f ./scratch-target.txt) and access to /tmp/work2",
         "optionId": "allow_always"},
        {"kind": "allow_once", "name": "Allow", "optionId": "allow"},
        {"kind": "reject_once", "name": "Reject", "optionId": "reject"},
    ],
    "sessionId": "e07102c3-f554-4717-8ddb-3d16534ce207",
    "toolCall": {
        "toolCallId": "toolu_01FWwqHtv3X8jgmhjvqKbdVy",
        "rawInput": {"command": "rm -f ./scratch-target.txt && ls -la ./scratch-target.txt 2>&1",
                     "description": "Remove scratch-target.txt"},
        "title": "rm -f ./scratch-target.txt && ls -la ./scratch-target.txt 2>&1",
        "kind": "execute",
    },
}


def test_the_live_request_becomes_a_menu():
    menu = menu_from_permission_request(LIVE_REQUEST)
    assert menu is not None
    assert [o["label"] for o in menu["options"]] == [
        "Always Allow Bash(rm -f ./scratch-target.txt) and access to /tmp/work2",
        "Allow",
        "Reject",
    ]


def test_it_carries_the_command_being_approved():
    """The command is the whole decision — a menu without it is unanswerable
    away from the keyboard. The terminal parser goes to real trouble to rejoin a
    wrapped one; here it arrives intact."""
    menu = menu_from_permission_request(LIVE_REQUEST)
    assert menu["body"].startswith("rm -f ./scratch-target.txt")
    assert menu["title"] == "Command"


def test_the_payload_matches_what_the_terminal_parser_emits():
    """Convergence, asserted rather than hoped for: same keys, same types, so
    one client renders both paths and neither can quietly grow a field."""
    menu = menu_from_permission_request(LIVE_REQUEST)
    for key in ("question", "title", "body", "selected", "options"):
        assert key in menu, key
    for option in menu["options"]:
        assert set(option) >= {"number", "label"}
        assert isinstance(option["number"], int)
        assert isinstance(option["label"], str)


def test_numbers_are_for_rendering_and_ids_are_for_answering():
    """ACP answers by optionId, the TUI by keystroke. Numbering exists only so
    one client can drive both; the ID is what actually goes back."""
    menu = menu_from_permission_request(LIVE_REQUEST)
    assert option_id_for(menu, 1) == "allow_always"
    assert option_id_for(menu, 3) == "reject"
    assert option_id_for(menu, None) is None


def test_an_option_the_agent_never_offered_is_refused():
    """Same guard the keystroke path applies: an answer the agent did not offer
    is not an answer, and the request stays open."""
    menu = menu_from_permission_request(LIVE_REQUEST)
    with pytest.raises(ValueError):
        option_id_for(menu, 9)


def test_the_open_request_is_identified_so_it_can_be_answered():
    """ACP replies to a request that is still open — the runner must know which."""
    assert menu_from_permission_request(LIVE_REQUEST)["tool_call_id"].startswith("toolu_")


def test_a_request_with_no_options_is_not_a_menu():
    assert menu_from_permission_request({"options": []}) is None
    assert menu_from_permission_request({}) is None
    assert menu_from_permission_request(None) is None


def test_a_file_operation_reads_sensibly():
    menu = menu_from_permission_request({
        "options": [{"kind": "allow_once", "name": "Allow", "optionId": "allow"},
                    {"kind": "reject_once", "name": "Reject", "optionId": "reject"}],
        "toolCall": {"toolCallId": "toolu_2", "kind": "edit",
                     "rawInput": {"file_path": "/repo/app/settings.py"},
                     "title": "Edit settings.py"},
    })
    assert menu["title"] == "File edit"
    assert menu["body"] == "/repo/app/settings.py"


# -- AskUserQuestion, which ACP carries as a form elicitation ----------------
#
# Shapes are what claude-agent-acp 0.93.0's `askUserQuestionsToCreateRequest`
# builds (dist/elicitation.js): one `question_<n>` field per question — a titled
# `oneOf` enum, or an array of `anyOf` for a multi-select — each followed by an
# optional free-text `question_<n>_custom`. A single question's text rides
# `message`; with several, each field's `description` carries its own.

ONE_QUESTION = {
    "mode": "form", "sessionId": "s", "toolCallId": "toolu_01Ask",
    "message": "Pick a colour",
    "requestedSchema": {"type": "object", "properties": {
        "question_0": {"type": "string", "title": "Colour", "oneOf": [
            {"const": "Aubergine", "title": "Aubergine", "description": "Deep purple"},
            {"const": "Beige", "title": "Beige"}]},
        "question_0_custom": {"type": "string", "title": "Other",
                              "description": "Type your own answer (optional)."},
    }},
}

TWO_QUESTIONS = {
    "mode": "form", "sessionId": "s", "toolCallId": "toolu_02Ask",
    "message": "Please answer the following questions.",
    "requestedSchema": {"type": "object", "properties": {
        "question_0": {"type": "string", "title": "Size", "description": "Which size?",
                       "oneOf": [{"const": "Small", "title": "Small"},
                                 {"const": "Large", "title": "Large"}]},
        "question_0_custom": {"type": "string", "title": "Other"},
        "question_1": {"type": "array", "title": "Toppings", "description": "Which toppings?",
                       "items": {"anyOf": [{"const": "Cheese", "title": "Cheese"},
                                           {"const": "Olives", "title": "Olives"},
                                           {"const": "Basil", "title": "Basil"}]}},
        "question_1_custom": {"type": "string", "title": "Other"},
    }},
}


def test_an_ask_reads_exactly_like_the_same_ask_from_the_hook():
    """Convergence: the laptop's hook sees the TOOL INPUT, the cloud sees the form
    the adapter built from it. One client renders both, so both must be one shape."""
    from_cloud = menu_from_elicitation(ONE_QUESTION)
    from_laptop = menu_from_hook({
        "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{
            "question": "Pick a colour", "header": "Colour", "multiSelect": False,
            "options": [{"label": "Aubergine", "description": "Deep purple"},
                        {"label": "Beige"}]}]}})
    for key in ("question", "title", "body", "selected", "options", "questions"):
        assert from_cloud[key] == from_laptop[key], key
    assert from_cloud["source"] == "acp"
    assert from_cloud["observed_at"]


def test_every_question_is_carried_with_its_own_text_and_kind():
    menu = menu_from_elicitation(TWO_QUESTIONS)
    assert [q["question"] for q in menu["questions"]] == ["Which size?", "Which toppings?"]
    assert [q["multi_select"] for q in menu["questions"]] == [False, True]
    assert [o["label"] for o in menu["questions"][1]["options"]] == ["Cheese", "Olives", "Basil"]
    assert menu["question"] == "Which size?"
    assert menu["body"] == "1 more question after this one."


def test_a_form_that_is_not_an_ask_is_not_a_menu():
    """An MCP server's own elicitation has its own fields; guessing at buttons for
    it is how a tap answers the wrong thing. The runner declines those instead."""
    assert menu_from_elicitation({"mode": "form", "message": "Log in",
                                  "requestedSchema": {"properties": {
                                      "username": {"type": "string"}}}}) is None
    assert menu_from_elicitation({"mode": "url", "url": "https://x"}) is None
    assert menu_from_elicitation(None) is None


def test_a_tapped_option_becomes_the_label_the_tool_records():
    menu = menu_from_elicitation(ONE_QUESTION)
    assert elicitation_answer(menu, option=1) == {
        "action": "accept", "content": {"question_0": "Aubergine"}}


def test_a_multi_question_answer_fills_every_field():
    menu = menu_from_elicitation(TWO_QUESTIONS)
    answer = elicitation_answer(menu, option=2, selections=[[2], [1, 3]],
                                texts=[None, "Extra garlic"])
    assert answer == {"action": "accept", "content": {
        "question_0": "Large", "question_1": ["Cheese", "Basil"],
        "question_1_custom": "Extra garlic"}}


def test_a_typed_answer_alone_is_an_answer():
    menu = menu_from_elicitation(ONE_QUESTION)
    assert elicitation_answer(menu, option=None, selections=[[]], texts=["Teal"]) == {
        "action": "accept", "content": {"question_0_custom": "Teal"}}


def test_a_refusal_declines_rather_than_aborting_the_turn():
    """Escape at the TUI. `decline` tells the model the person skipped and the turn
    goes on; `cancel` would abort the tool call mid-turn."""
    menu = menu_from_elicitation(ONE_QUESTION)
    assert elicitation_answer(menu, option=None) == {"action": "decline"}


def test_an_answer_the_ask_never_offered_is_refused():
    menu = menu_from_elicitation(TWO_QUESTIONS)
    with pytest.raises(ValueError):
        elicitation_answer(menu, option=7)
    with pytest.raises(ValueError):
        elicitation_answer(menu, option=1, selections=[[1], [9]])
