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
