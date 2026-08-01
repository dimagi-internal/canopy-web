"""The dialog a blocked agent is waiting on, read from its transcript.

The payloads here are the REAL ones. `SPARK_ASK` is copied verbatim from
`ace`'s `spark` session on 2026-07-31, the run that motivated this: an
`AskUserQuestion` at 03:53:46Z that nobody could answer from a phone, and a
session that consequently sat dead for 52 minutes.
"""
import canopy_transcript as ct

# Verbatim from the live transcript (descriptions truncated only for width).
SPARK_ASK = {
    "questions": [
        {
            "question": "Phase 3 landed `verdict: partial` with 2 BLOCKER concerns. How should the run proceed?",
            "header": "Phase 3→4",
            "multiSelect": False,
            "options": [
                {"label": "Proceed to Phase 4 (Recommended)",
                 "description": "Accept the 6 recorded residuals and continue into Connect setup."},
                {"label": "Fix the duplicate guard first",
                 "description": "Pause Phase 4 and build the registration duplicate guard."},
                {"label": "Stop the run here",
                 "description": "End at the Phase 3 boundary with apps built, released and QA-passed."},
            ],
        }
    ]
}


_DEFAULT = object()   # so `payload=None` can be tested as a real malformed input


def _ask(tool_use_id="toolu_01", payload=_DEFAULT, **over):
    rec = {
        "type": "assistant",
        "timestamp": "2026-07-31T03:53:46.217Z",
        "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": "AskUserQuestion",
             "input": SPARK_ASK if payload is _DEFAULT else payload},
        ]},
    }
    rec.update(over)
    return rec


def _answer(tool_use_id="toolu_01"):
    return {
        "type": "user",
        "timestamp": "2026-07-31T04:45:35.470Z",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": 'Your questions have been answered.'},
        ]},
    }


def _text(role="assistant", text="working on it"):
    return {"type": role, "message": {"content": [{"type": "text", "text": text}]}}


# -- the case that motivated this ------------------------------------------


def test_an_unanswered_question_is_the_pending_menu():
    menu = ct.pending_question([_text(), _ask()])
    assert menu is not None
    assert menu["question"] == SPARK_ASK["questions"][0]["question"]
    assert menu["title"] == "Phase 3→4"
    assert [(o["number"], o["label"]) for o in menu["options"]] == [
        (1, "Proceed to Phase 4 (Recommended)"),
        (2, "Fix the duplicate guard first"),
        (3, "Stop the run here"),
    ]


def test_the_options_keep_their_descriptions():
    """The description is most of what a choice MEANS — on spark, the labels
    alone ("Proceed to Phase 4") could not tell you that Phase 4 is test-gated.
    A phone that shows only labels is asking someone to decide blind."""
    menu = ct.pending_question([_ask()])
    assert menu["options"][0]["description"].startswith("Accept the 6 recorded residuals")


def test_an_answered_question_is_not_pending():
    """52 minutes later the human answered at the laptop. The menu must vanish
    on its own — a stale dialog with live buttons presses something real."""
    assert ct.pending_question([_ask(), _answer(), _text()]) is None


def test_the_answer_must_match_the_question_it_answers():
    """A tool_result for a DIFFERENT call says nothing about this one. Matching
    on 'some result arrived' would clear a dialog that is still up."""
    assert ct.pending_question([_ask("toolu_A"), _answer("toolu_B")]) is not None


def test_the_newest_unanswered_question_wins():
    """Two asks in one session: the second is what is on screen. Answering the
    first one's options would press the wrong keys against the second."""
    other = {"questions": [{"question": "Second?", "header": "B", "multiSelect": False,
                            "options": [{"label": "x"}, {"label": "y"}]}]}
    menu = ct.pending_question([_ask("toolu_A"), _answer("toolu_A"), _ask("toolu_B", other)])
    assert menu["question"] == "Second?"


def test_a_transcript_with_no_question_has_no_menu():
    assert ct.pending_question([_text(), _text("user", "go")]) is None


# -- numbering is the load-bearing part ------------------------------------


def test_numbering_starts_at_one_in_declared_order():
    """The answer is sent as a KEYSTROKE. Claude Code renders the declared
    options first, numbered from 1 in order (verified against the captured live
    screen in runner test_menu.py::ASK_USER_QUESTION, where two declared options
    rendered as 1 and 2 before the two the TUI appends itself). Any other
    numbering here presses the wrong option — silently, because a number always
    selects something."""
    payload = {"questions": [{"question": "Pick a colour", "header": "Colour",
                              "multiSelect": False,
                              "options": [{"label": "Red"}, {"label": "Blue"}]}]}
    menu = ct.pending_question([_ask(payload=payload)])
    assert [(o["number"], o["label"]) for o in menu["options"]] == [(1, "Red"), (2, "Blue")]


def test_the_options_the_tui_appends_are_not_invented_here():
    """Claude Code adds its own trailing options ("Type something", "Chat about
    this"). We do NOT synthesize them: the runner re-reads the real screen and
    refuses any option not on it, so a fabricated number would simply be
    dropped — and offering a phone a button that never works is worse than not
    offering it."""
    payload = {"questions": [{"question": "Pick a colour", "header": "",
                              "options": [{"label": "Red"}, {"label": "Blue"}]}]}
    assert len(ct.pending_question([_ask(payload=payload)])["options"]) == 2


# -- robustness: this runs against every session on the box, every 10s ------


def test_a_subagent_question_is_not_the_session_s_question():
    """A sidechain (Task/Agent) turn has its own conversation. Its dialog is not
    on the screen the human would answer, and canopy has no way to route a key
    into it."""
    assert ct.pending_question([_ask(**{"isSidechain": True})]) is None


def test_a_question_with_no_options_is_not_a_menu():
    """Fails closed, like find_menu: nothing to press means nothing to show."""
    payload = {"questions": [{"question": "Anything?", "options": []}]}
    assert ct.pending_question([_ask(payload=payload)]) is None


def test_malformed_payloads_never_raise():
    """This is computed for every open session on the box on a 10s cadence. A
    crash here would take out the liveness report with it."""
    for bad in ({}, {"questions": []}, {"questions": "no"}, {"questions": [None]},
                {"questions": [{"options": [{"label": None}]}]}, None):
        assert ct.pending_question([_ask(payload=bad)]) is None
    assert ct.pending_question([]) is None
    assert ct.pending_question([{"type": "assistant"}, {"message": None}, None]) is None


def test_remaining_questions_are_stated_not_dropped():
    """AskUserQuestion can carry several questions; the TUI shows them one at a
    time. We render the first and SAY there are more, rather than silently
    presenting a 1-of-3 dialog as the whole ask."""
    payload = {"questions": [
        {"question": "First?", "options": [{"label": "a"}, {"label": "b"}]},
        {"question": "Second?", "options": [{"label": "c"}]},
    ]}
    menu = ct.pending_question([_ask(payload=payload)])
    assert menu["question"] == "First?"
    assert "1 more" in menu["body"]


def test_the_menu_says_where_it_came_from():
    """Two producers reach the same client shape (this, and the CDP screen
    read). `source` is what lets an operator tell which path answered — and the
    client must be able to ignore it."""
    assert ct.pending_question([_ask()])["source"] == "transcript"


def test_the_shape_matches_the_screen_reader_s():
    """One payload shape, whichever half produced it — the client must never
    grow two readers (the rule execute.py's _blocking_dialog_note already
    follows)."""
    menu = ct.pending_question([_ask()])
    assert set(menu) == {"question", "title", "body", "selected", "options", "source"}
    assert menu["selected"] is None      # a transcript cannot see the cursor


# --- the hook path -------------------------------------------------------
#
# `pending_question` above can only ever report a dialog that is OVER. Claude
# Code writes the AskUserQuestion tool_use record when the ask is answered, not
# when it is made: measured 2026-08-01 across 60 transcripts on a live box, 39
# such records, all already answered, zero pending — while two sessions sat
# blocked on visible dialogs with nothing in their files at all. `PreToolUse`
# fires at the other end, when the ask starts, carrying the same tool_input.

PRE_TOOL_USE = {
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_input": SPARK_ASK,
}


def test_a_hook_produces_the_same_menu_the_transcript_would_have():
    """One dict from either half — a client must never learn which found it."""
    from_hook = ct.menu_from_hook(PRE_TOOL_USE)
    from_transcript = ct.pending_question([
        {"message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "AskUserQuestion",
             "input": SPARK_ASK},
        ]}},
    ])
    assert from_hook is not None and from_transcript is not None
    assert {k: v for k, v in from_hook.items() if k != "source"} == \
           {k: v for k, v in from_transcript.items() if k != "source"}


def test_the_hook_menu_names_its_source():
    assert ct.menu_from_hook(PRE_TOOL_USE)["source"] == "hook"


def test_only_the_ask_starting_is_a_menu():
    """PostToolUse for the same tool carries the same input — reading it as a
    menu would re-raise the dialog the human just dismissed."""
    assert ct.menu_from_hook({**PRE_TOOL_USE, "hook_event_name": "PostToolUse"}) is None
    assert ct.menu_from_hook({**PRE_TOOL_USE, "tool_name": "Bash"}) is None
    assert ct.menu_from_hook(None) is None
    assert ct.menu_from_hook({}) is None


def test_what_retires_a_menu():
    assert ct.hook_retires_menu({"hook_event_name": "PostToolUse",
                                 "tool_name": "AskUserQuestion"})
    assert ct.hook_retires_menu({"hook_event_name": "Stop"})
    assert ct.hook_retires_menu({"hook_event_name": "UserPromptSubmit"})
    # A tool finishing while the dialog is up must not clear it — the ask is
    # itself a tool call, and parallel calls are ordinary.
    assert not ct.hook_retires_menu({"hook_event_name": "PostToolUse", "tool_name": "Bash"})
    assert not ct.hook_retires_menu(PRE_TOOL_USE)
    assert not ct.hook_retires_menu(None)


# --- the dialogs that carry no menu ---------------------------------------
#
# A permission prompt or trust gate is drawn with no tool call behind it, so it
# exists only on the rendered terminal — and reading that means CDP, which steals
# focus. `Notification` is what a hook CAN see: a human is wanted, and roughly
# why. Less than a menu, and honest about it.

NOTIFY = {"hook_event_name": "Notification",
          "message": "Claude needs your permission to use Bash"}


def test_a_notification_becomes_an_option_less_record():
    m = ct.marker_from_hook(NOTIFY)
    assert m is not None
    assert m["question"] == "Claude needs your permission to use Bash"
    assert m["options"] == []
    assert m["source"] == "notification"


def test_the_message_is_passed_through_not_interpreted():
    """Working out WHICH dialog it is by parsing the wording is how a signal
    starts lying. The words go through as written; the human can tell."""
    m = ct.marker_from_hook({"hook_event_name": "Notification",
                             "message": "Claude is waiting for your input"})
    assert m["question"] == "Claude is waiting for your input"


def test_a_message_less_notification_still_says_something():
    for payload in ({"hook_event_name": "Notification"},
                    {"hook_event_name": "Notification", "message": "   "}):
        assert ct.marker_from_hook(payload)["question"] == "This session is waiting on you."


def test_only_a_notification_makes_a_marker():
    assert ct.marker_from_hook(PRE_TOOL_USE) is None
    assert ct.marker_from_hook({"hook_event_name": "Stop"}) is None
    assert ct.marker_from_hook(None) is None
