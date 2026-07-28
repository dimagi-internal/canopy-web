"""Parsing a Claude Code permission dialog out of a rendered terminal.

Every fixture here was CAPTURED, not written: `claude` driven in a real PTY and
rendered through a terminal emulator, which is the same character grid xterm
produces for emdash — so what the runner reads over CDP is what these strings
contain. Verified by roundtrip on 2026-07-28: the dialog below was detected,
answered with a keystroke, and the file it asked about was actually deleted.
"""
from canopy_runner.menu import answer_keys, find_menu

# Verbatim, as a terminal renders it. Note the leading spaces, the box rule, and
# that option 2's text wraps concerns beyond the tool itself.
PERMISSION = """\
❯ Delete the file target.txt using the Bash tool (rm).
  Read 1 file, listed 1 directory (ctrl+o to expand)
⏺ Bash(rm /private/tmp/claude-501/-Users-jjackson-emdash-worktrees-canopy-web-em
      dash-acp-runner-730f0/be9ee2f2-4712-40ce-9f0d-17846ae7a65f/scratchpad/rt2/
      target.txt &&…)
  ⎿  Waiting…
────────────────────────────────────────────────────────────────────────────────
 Bash command
   rm /private/tmp/claude-501/-Users-jjackson-emdash-worktrees-canopy-web-emd
   ash-acp-runner-730f0/be9ee2f2-4712-40ce-9f0d-17846ae7a65f/scratchpad/rt2/t
   arget.txt && ls -la
   /private/tmp/claude-501/-Users-jjackson-emdash-worktrees-canopy-web-emdash
   -acp-runner-730f0/be9ee2f2-4712-40ce-9f0d-17846ae7a65f/scratchpad/rt2
   Delete target.txt and verify
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to rt2/ from this project
   3. No
 Esc to cancel · Tab to amend · ctrl+e to explain
"""

# The gate a fresh worktree hits first. A different dialog, same grammar — which
# is the point: one parser, or the runner grows a special case per dialog.
TRUST = """\
────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:
 /private/tmp/scratchpad/menu-work
 Quick safety check: Is this a project you created or one you trust?
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel
"""

# A working session. The single most important negative case: mistaking this for
# a menu would tell the phone an agent is blocked when it is busy, and every
# "needs you" after that is noise.
BUSY = """\
❯ Delete the file target.txt using the Bash tool (rm).
⏺ Bash(rm target.txt && ls -la)
  ⎿  Waiting…
✻ Sautéed for 6s
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
"""


def test_a_permission_dialog_is_found():
    menu = find_menu(PERMISSION)
    assert menu is not None
    assert menu.question == "Do you want to proceed?"


def test_every_option_is_extracted_in_order():
    menu = find_menu(PERMISSION)
    assert [(o.number, o.label) for o in menu.options] == [
        (1, "Yes"),
        (2, "Yes, and always allow access to rt2/ from this project"),
        (3, "No"),
    ]


def test_the_selected_option_is_known():
    """The ❯ marker is which choice is highlighted. Answering by NUMBER makes it
    cosmetic — but showing the wrong default on a phone is its own bug."""
    assert find_menu(PERMISSION).selected == 1


def test_the_subject_is_carried_so_the_phone_can_show_WHAT_it_is_asking():
    """A menu with no subject is unanswerable away from the keyboard: "Do you
    want to proceed?" tells you nothing about what you are approving."""
    menu = find_menu(PERMISSION)
    assert menu.title == "Bash command"
    assert "rm " in menu.body
    assert "target.txt" in menu.body


def test_a_wrapped_command_is_rejoined():
    """The TUI hard-wraps the command across grid lines mid-token
    ('…canopy-web-emd' / 'ash-acp-runner…'). Joining with a space or a newline
    corrupts the command being approved — which is the one string a human is
    reading to decide."""
    body = find_menu(PERMISSION).body
    assert "canopy-web-emdash-acp-runner-730f0" in body
    assert "canopy-web-emd ash" not in body


def test_the_trust_gate_parses_with_the_same_grammar():
    menu = find_menu(TRUST)
    assert menu is not None
    assert [(o.number, o.label) for o in menu.options] == [
        (1, "Yes, I trust this folder"),
        (2, "No, exit"),
    ]


def test_a_busy_session_is_not_a_menu():
    assert find_menu(BUSY) is None


def test_empty_and_garbage_are_not_menus():
    assert find_menu("") is None
    assert find_menu("just some output\nand more") is None


def test_numbered_prose_is_not_a_menu():
    """An agent writing a numbered list must not read as a dialog."""
    prose = """\
⏺ Here is the plan:
   1. Read the file
   2. Change the thing
   3. Run the tests
"""
    assert find_menu(prose) is None


def test_answering_is_the_number_then_enter():
    """Roundtrip-verified 2026-07-28: '1' + Enter on the dialog above ran the
    command and deleted the file."""
    assert answer_keys(1) == ["1", "\r"]
    assert answer_keys(3) == ["3", "\r"]


def test_refusing_is_escape_not_a_number():
    """Every dialog offers Esc, and it is the only answer that is safe when the
    option numbering is not what we think it is."""
    assert answer_keys(None) == ["\x1b"]


def test_an_option_outside_the_menu_is_refused():
    """A client could post any number; approving option 9 of a 3-option dialog
    by typing '9' would leave the prompt open and the turn wedged."""
    menu = find_menu(PERMISSION)
    assert menu.allows(1) and menu.allows(3)
    assert not menu.allows(4)
    assert not menu.allows(0)
