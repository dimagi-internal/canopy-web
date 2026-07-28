"""ACP permission requests -> the menu payload the phone already renders.

The fixture is the VERBATIM `session/request_permission` params captured from
claude-agent-acp 0.63.0 on 2026-07-28 (spike4), so this pins the real contract
rather than a guess at it.

The point of these tests is convergence: what comes out here must be
interchangeable with what the terminal parser produces on the laptop, or the
client needs two renderers and the two paths drift.
"""
import pytest

from canopy_acp.menus import menu_from_permission_request, option_id_for

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
