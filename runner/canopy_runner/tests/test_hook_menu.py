"""The dialog a blocked agent is waiting on, captured from its own hook.

**The bug this exists to prevent.** The blocked-agent menu had two producers and
neither could see a live dialog. The transcript reader is blind by construction:
Claude Code writes the `AskUserQuestion` tool_use record only when the dialog is
ANSWERED — measured 2026-08-01 across 60 transcripts on a live box, 39 records,
every one already answered and not one pending, while two sessions sat visibly
blocked with nothing in their files. The screen reader can see it, but only by
driving CDP, which clicks the task and steals focus, so #510 stopped it running
on a signal and nothing has triggered it since.

`PreToolUse` closes the gap for free: it fires when the ask STARTS and carries
the same `tool_input` the transcript would eventually hold.
"""
from canopy_runner.hook_listener import HookListener

CWD = "/Users/x/emdash/worktrees/canopy-web/emdash/canopy-web-chat-1706-g225m"
KEY = ("canopy-web", "canopy-web-chat-1706")

ASK = {
    "hook_event_name": "PreToolUse",
    "cwd": CWD,
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [{
            "question": "How far do you want to take this?",
            "header": "Scope",
            "options": [
                {"label": "Both, bulk_create first", "description": "Fix the write path, then persist eagerly."},
                {"label": "Fast only", "description": "Chunked bulk_create."},
            ],
        }],
    },
}


def listener(**kw):
    return HookListener(
        port=0, nonce="n",
        resolve_session=lambda cwd: "sess" if cwd == CWD else "",
        forward=kw.pop("forward", lambda: True),
        resolve_task=lambda cwd: [KEY] if cwd == CWD else [],
        **kw,
    )


def test_the_ask_becomes_a_menu_the_report_can_serve():
    hl = listener()
    hl.handle_payload(ASK)
    menu = hl.pending_menu(*KEY)
    assert menu is not None
    assert menu["question"] == "How far do you want to take this?"
    assert menu["title"] == "Scope"
    assert [o["number"] for o in menu["options"]] == [1, 2]
    assert menu["options"][0]["label"] == "Both, bulk_create first"


def test_the_menu_says_which_half_found_it():
    """A client must never need to know, but an operator debugging a missing
    menu has to be able to tell the three producers apart."""
    hl = listener()
    hl.handle_payload(ASK)
    assert hl.pending_menu(*KEY)["source"] == "hook"


def test_a_menu_is_held_even_when_forwarding_is_off():
    """`forward_sessions` gates the POST of live events. A dialog is not an
    event — it is state the session report reads — so gating it there would make
    the phone's buttons depend on an unrelated switch."""
    hl = listener(forward=lambda: False)
    hl.handle_payload(ASK)
    assert hl.pending_menu(*KEY) is not None


def test_the_answer_retires_the_menu():
    hl = listener()
    hl.handle_payload(ASK)
    hl.handle_payload({"hook_event_name": "PostToolUse", "cwd": CWD,
                       "tool_name": "AskUserQuestion", "tool_response": {}})
    assert hl.pending_menu(*KEY) is None


def test_the_turn_ending_retires_the_menu():
    """Answered at the laptop with our PostToolUse missed, the menu would
    otherwise outlive its dialog — and its numbers get typed into an ordinary
    prompt, where the agent reads a bare "2" as an instruction."""
    hl = listener()
    hl.handle_payload(ASK)
    hl.handle_payload({"hook_event_name": "Stop", "cwd": CWD})
    assert hl.pending_menu(*KEY) is None


def test_another_tools_PostToolUse_does_not_retire_it():
    """An agent runs tools while a dialog is up (it is a tool call itself, and
    parallel calls are ordinary). Clearing on any completion would drop the menu
    a second after it appeared."""
    hl = listener()
    hl.handle_payload(ASK)
    hl.handle_payload({"hook_event_name": "PostToolUse", "cwd": CWD, "tool_name": "Bash"})
    assert hl.pending_menu(*KEY) is not None


def test_a_session_this_box_does_not_back_is_ignored():
    """Hooks are installed at USER level, so they fire for every Claude Code
    session on the machine — most of which are not ours."""
    hl = listener()
    hl.handle_payload({**ASK, "cwd": "/somewhere/else"})
    assert hl.pending_menu(*KEY) is None
    assert hl.pending_menu("", "") is None


def test_a_malformed_payload_never_raises():
    """A hook that errors is a hook that can cost an agent its turn."""
    hl = listener()
    for bad in ({}, {"hook_event_name": "PreToolUse", "cwd": CWD, "tool_name": "AskUserQuestion"},
                {"hook_event_name": "PreToolUse", "cwd": CWD,
                 "tool_name": "AskUserQuestion", "tool_input": {"questions": []}}):
        hl.handle_payload(bad)
    assert hl.pending_menu(*KEY) is None


def test_the_report_prefers_the_transcript_and_falls_back_to_the_hook():
    """Both are consulted, in that order — the durable file wins on a session
    whose hooks were never installed, and the hook covers the live dialog the
    file cannot contain."""
    from canopy_runner import transcript

    sessions = [{"project": "canopy-web", "emdash_task": "canopy-web-chat-1706"}]
    transcript.attach_pending_questions(
        sessions,
        claude_home=__import__("pathlib").Path("/nonexistent"),
        hook_menu_for=lambda p, t: {"question": "from the hook"} if (p, t) == KEY else None,
    )
    assert sessions[0]["question"] == {"question": "from the hook"}


def test_a_session_with_no_dialog_still_reports_None():
    """None is a real answer — it is what retires a menu somebody answered at
    the laptop."""
    from canopy_runner import transcript

    sessions = [{"project": "canopy-web", "emdash_task": "other-task"}]
    transcript.attach_pending_questions(
        sessions,
        claude_home=__import__("pathlib").Path("/nonexistent"),
        hook_menu_for=lambda p, t: None,
    )
    assert sessions[0]["question"] is None


def test_the_menu_does_not_depend_on_anybody_watching():
    """The regression that made the first cut of this fix nearly inert: the key
    was resolved through `_hook_sessions`, which is rebuilt wholesale from
    `sync_streams` — only the sessions a VIEWER is attached to. A menu captured
    only while somebody already has the chat open is captured only in the case
    that does not need it; you go and look BECAUSE the session stopped."""
    from canopy_runner import hooks

    hooks._hook_sessions.clear()          # nobody is watching anything
    keys = hooks.hook_project_task_keys(
        "/Users/x/emdash/worktrees/canopy-web/emdash/some-task-ab12x",
        home=__import__("pathlib").Path("/Users/x"),
    )
    assert keys, "a session with no viewer must still resolve to a report key"
    assert all(k[0] == "canopy-web" for k in keys)


def test_a_path_that_is_not_an_emdash_worktree_resolves_to_nothing():
    from canopy_runner import hooks

    home = __import__("pathlib").Path("/Users/x")
    assert hooks.hook_project_task_keys("/tmp/somewhere", home=home) == []
    assert hooks.hook_project_task_keys("", home=home) == []


def test_every_spelling_of_the_task_gets_the_menu():
    """The worktree dir may carry emdash's de-dupe suffix and the cwd cannot say
    which form the session report uses, so the menu is stored under each. They
    come from one path, so at most one is ever queried."""
    hl = HookListener(
        port=0, nonce="n", resolve_session=lambda cwd: "",
        forward=lambda: True,
        resolve_task=lambda cwd: [("p", "task-ab12x"), ("p", "task")],
    )
    hl.handle_payload({**ASK, "cwd": "/any"})
    assert hl.pending_menu("p", "task-ab12x") is not None
    assert hl.pending_menu("p", "task") is not None
    hl.handle_payload({"hook_event_name": "Stop", "cwd": "/any"})
    assert hl.pending_menu("p", "task-ab12x") is None
    assert hl.pending_menu("p", "task") is None


# --- what became of a tap -------------------------------------------------
#
# The original complaint, in its purest form: "clicking on a menu option doesn't
# fire." Every cause underneath it is now fixed, but a refusal was still silent —
# the API answers the phone `ok:true` the instant it relays the frame, so a
# correct refusal and a successful press look identical from a thumb.

# A real captured dialog (the trust gate, verbatim) — this file needs a screen to
# answer against, and `test_menu.py` owns the captures.
DIALOG = """\
────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:
 /private/tmp/scratchpad/menu-work
 Quick safety check: Is this a project you created or one you trust?
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel
"""


def test_a_successful_answer_drops_the_menu_at_once():
    """Not left for the agent's PostToolUse: the dialog is gone the moment the
    key lands, and a menu that outlives it invites a second tap at a dialog that
    has already moved on."""
    from canopy_runner import hooks

    hl = listener()
    hl.handle_payload(ASK)
    hl.note_answer([KEY], hooks.ANSWERED)
    assert hl.pending_menu(*KEY) is None


def test_a_refused_answer_keeps_the_menu_and_says_why():
    """The pairing is the point — the same buttons, plus the reason they did not
    work. Dropping the menu here would leave a phone with nothing to retry."""
    from canopy_runner import hooks

    hl = listener()
    hl.handle_payload(ASK)
    hl.note_answer([KEY], hooks.WRONG_PANE, hooks.ANSWER_NOTES[hooks.WRONG_PANE])
    menu = hl.pending_menu(*KEY)
    assert menu is not None
    assert menu["answer_error"] == hooks.WRONG_PANE
    assert "Claude tab" in menu["answer_note"]
    assert [o["number"] for o in menu["options"]] == [1, 2]


def test_every_refusal_has_something_to_show_a_human():
    """A reason code with no sentence behind it reaches the phone as a bare
    enum."""
    from canopy_runner import hooks

    for outcome in (hooks.NO_DIALOG, hooks.NOT_ON_MENU, hooks.WRONG_PANE,
                    hooks.UNREACHABLE):
        assert hooks.ANSWER_NOTES.get(outcome), outcome
    assert hooks.ANSWERED not in hooks.ANSWER_NOTES  # success is not an error


def test_answering_reports_the_outcome_instead_of_raising():
    """`answer_menu_with` returns; the CDP-bound wrapper classifies transport
    failures. Both must be data, because the outcome has to ride the session
    report back to the phone."""
    from canopy_runner import hooks

    class NoDialog:
        def read_terminal(self, *a, **k): return "just output\nnothing here"
    assert hooks.answer_menu_with(NoDialog(), "t", 1) == hooks.NO_DIALOG

    class Real:
        sent = None
        def read_terminal(self, *a, **k): return DIALOG
        def send_keys(self, task, keys, **k): Real.sent = keys
    cdp = Real()
    assert hooks.answer_menu_with(cdp, "t", 9) == hooks.NOT_ON_MENU   # not on the menu
    assert Real.sent is None, "a rejected option must never reach the terminal"
    assert hooks.answer_menu_with(cdp, "t", 1) == hooks.ANSWERED
    assert Real.sent == ["1", "\r"]


def test_a_wrong_pane_is_told_apart_from_a_dead_runner():
    """They want opposite things from a human: switch your emdash tab, versus go
    find out why the box is unreachable."""
    from canopy_runner import cdp_control, hooks

    class Boom:
        def read_terminal(self, *a, **k):
            raise cdp_control.CDPError("NOT_A_CLAUDE_PANE: a shell tab is selected")
    class Dead:
        def read_terminal(self, *a, **k):
            raise cdp_control.CDPError("ECONNREFUSED")

    orig = hooks.cdp_control
    try:
        hooks.cdp_control = Boom()
        assert hooks.answer_menu("t", 1) == hooks.WRONG_PANE
        hooks.cdp_control = Dead()
        assert hooks.answer_menu("t", 1) == hooks.UNREACHABLE
    finally:
        hooks.cdp_control = orig


# --- surviving a restart --------------------------------------------------

def test_a_menu_survives_the_runner_restarting(tmp_path):
    """The hook fires once. Without a store a restart does not merely forget a
    live menu — the next report ships `question: null` and RETIRES it, and
    nothing can rediscover it. The runner auto-updates every 30 minutes, so this
    is routine rather than rare."""
    from canopy_runner.menu_store import MenuStore

    store = MenuStore(tmp_path / "pending-menus.json")
    hl = listener(menu_store=store)
    hl.handle_payload(ASK)

    revived = listener(menu_store=MenuStore(tmp_path / "pending-menus.json"))
    menu = revived.pending_menu(*KEY)
    assert menu is not None
    assert menu["question"] == "How far do you want to take this?"
    assert menu["restored"] is True


def test_an_answered_menu_does_not_come_back_from_the_store(tmp_path):
    from canopy_runner import hooks
    from canopy_runner.menu_store import MenuStore

    path = tmp_path / "pending-menus.json"
    hl = listener(menu_store=MenuStore(path))
    hl.handle_payload(ASK)
    hl.note_answer([KEY], hooks.ANSWERED)
    assert listener(menu_store=MenuStore(path)).pending_menu(*KEY) is None


def test_a_corrupt_store_is_an_empty_one(tmp_path):
    """Never a crash on a path that runs at startup and inside a hook."""
    from canopy_runner.menu_store import MenuStore

    path = tmp_path / "pending-menus.json"
    path.write_text("{not json at all")
    assert MenuStore(path).load() == {}
    assert MenuStore(tmp_path / "nope.json").load() == {}


# --- dialogs with no readable menu ----------------------------------------

NOTIFY = {"hook_event_name": "Notification", "cwd": CWD,
          "message": "Claude needs your permission to use Bash"}


def test_a_notification_reaches_the_phone_with_no_options():
    """A permission prompt or trust gate has no tool call behind it, so it exists
    only on the terminal — and reading that steals focus. Before this, those
    dialogs reached a phone only if somebody happened to be watching at the
    instant they appeared."""
    hl = listener()
    hl.handle_payload(NOTIFY)
    menu = hl.pending_menu(*KEY)
    assert menu is not None
    assert menu["options"] == []
    assert menu["question"] == "Claude needs your permission to use Bash"


def test_a_notification_never_downgrades_a_real_menu():
    """Buttons that work must not be replaced by words that do not. An agent can
    emit a Notification while an AskUserQuestion is already up."""
    hl = listener()
    hl.handle_payload(ASK)
    hl.handle_payload(NOTIFY)
    menu = hl.pending_menu(*KEY)
    assert [o["number"] for o in menu["options"]] == [1, 2]
    assert menu["source"] == "hook"


def test_a_real_menu_replaces_a_notification():
    """The other direction is an upgrade and must go through."""
    hl = listener()
    hl.handle_payload(NOTIFY)
    hl.handle_payload(ASK)
    assert len(hl.pending_menu(*KEY)["options"]) == 2


def test_the_turn_ending_retires_a_notification_too():
    hl = listener()
    hl.handle_payload(NOTIFY)
    hl.handle_payload({"hook_event_name": "Stop", "cwd": CWD})
    assert hl.pending_menu(*KEY) is None
