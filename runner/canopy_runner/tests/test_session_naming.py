"""The emdash session-name format, exercised as the one thing it is: a format.

Every assertion here is a name a human reads in the emdash sidebar. The old shape
(`<agent>-<subject>-<disc>-<MMDD>-<HHMM>`) spent its budget on two things emdash
already shows — the project group and the created-at column — and fell back to the
bare origin word (`ace-api-4a4e-0905-0920`) whenever no email subject existed, which
is most turns. See docs/superpowers/specs for the ladder's rationale.
"""
import pytest

from canopy_runner import session_naming as sn


def _turn(**kw):
    t = {"id": "t-abc123", "origin": "api", "origin_ref": {}, "prompt": ""}
    t.update(kw)
    return t


# --- the marker -------------------------------------------------------------

def test_every_name_declares_canopy_launched_it():
    """The prefix is the whole point: a glance at the sidebar separates work the
    fleet started from work a human typed."""
    name = sn.build_task_name("ace", _turn())
    assert name.startswith("c-")
    assert sn.is_canopy_task(name)


def test_a_human_typed_name_is_not_a_canopy_task():
    for human in ("improve-session-names", "close-sessions", "bednet", "cron-stuff"):
        assert not sn.is_canopy_task(human)


def test_legacy_stamped_names_are_still_recognised():
    """Names created before the prefix existed keep working — the fleet has live
    sessions carrying them, and reuse resolves by name."""
    assert sn.is_canopy_task("ace-api-4a4e-0905-0920")
    assert sn.is_canopy_task("hal-security-alert-6355-0714-1514")
    assert sn.is_canopy_task("emdash-echo-api-611f-0901-1248")


# --- the ladder -------------------------------------------------------------

def test_board_item_title_wins():
    t = _turn(origin_ref={"item_title": "Retry the backoff on 429", "thread_id": "abcd1234"})
    assert sn.build_task_name("ace", t) == "c-retry-the-backoff-on-429-1234"


def test_schedule_name_beats_its_own_slash_command():
    """A cron turn carries both. "Weekly manager report" is what the human named
    it; `/echo:manager-report` is how it is spelled to the agent."""
    t = _turn(origin="canopy_scheduler", prompt="/echo:manager-report",
              origin_ref={"schedule_name": "Weekly manager report", "thread_id": "aaaa9f21"})
    assert sn.build_task_name("echo", t) == "c-weekly-manager-report-9f21"


def test_email_subject_is_used_and_reply_noise_dropped():
    t = _turn(origin="email", origin_ref={"subject": "Re: Bednet demo!!", "thread_id": "19f4c06eeb986355"})
    assert sn.build_task_name("hal", t) == "c-bednet-demo-6355"


def test_a_slash_command_names_the_turn_that_had_no_subject():
    """The fix for `ace-api-4a4e-0905-0920`. The namespace is dropped when it just
    repeats the agent, which emdash already shows as the group."""
    t = _turn(prompt="/ace:issue-triage", origin_ref={"thread_id": "zz4a4e"})
    assert sn.build_task_name("ace", t) == "c-issue-triage-4a4e"


def test_a_foreign_namespace_is_kept_because_it_carries_information():
    t = _turn(prompt="/canopy:issue-triage", origin_ref={"thread_id": "zz4a4e"})
    assert sn.build_task_name("ace", t) == "c-canopy-issue-triage-4a4e"


def test_falls_back_to_the_first_real_line_of_the_brief():
    t = _turn(prompt="Re-validate the stall classifier against current reality.\n\nDetails follow.",
              origin_ref={"thread_id": "wwwwe4a7"})
    assert sn.build_task_name("connect-labs", t) == "c-re-validate-the-stall-classifier-e4a7"


def test_the_dispatch_stamp_never_becomes_the_name():
    """A dispatched brief ends in the marker + provenance. Naming a session after
    boilerplate every dispatched turn shares would make them indistinguishable."""
    t = _turn(prompt=(
        "Fix the flaky close-out test.\n\n"
        "— Dispatched by ada, a canopy agent, not typed by a human. Treat it as a hypothesis.\n"
        "<!-- canopy:dispatched-prompt -->\n<!-- canopy:dispatched-by=ada -->"
    ), origin_ref={"thread_id": "qqqq7f21"})
    assert sn.build_task_name("hal", t) == "c-fix-the-flaky-close-out-test-7f21"


def test_marker_only_prompt_falls_through_to_the_origin():
    t = _turn(origin="api", prompt="<!-- canopy:dispatched-prompt -->",
              origin_ref={"thread_id": "qqqq7f21"})
    assert sn.build_task_name("hal", t) == "c-api-7f21"


def test_origin_is_the_last_resort_not_the_common_case():
    assert sn.build_task_name("ace", _turn(origin="email", origin_ref={"thread_id": "aaaa0001"})) == "c-email-0001"


# --- uniqueness -------------------------------------------------------------

def test_two_threads_with_the_same_subject_stay_distinct():
    """The bug the discriminator exists for. Dropping the timestamp removes the
    accidental tiebreak, so this now rests entirely on the discriminator."""
    a = _turn(origin="email", origin_ref={"subject": "Security alert", "thread_id": "19f4c06eeb986355"})
    b = _turn(origin="email", origin_ref={"subject": "Security alert", "thread_id": "19f425675a9855a4"})
    assert sn.build_task_name("hal", a) == "c-security-alert-6355"
    assert sn.build_task_name("hal", b) == "c-security-alert-55a4"


def test_a_keyless_turn_discriminates_on_its_own_id():
    """No thread_key means fresh-per-turn, so the turn id is the discriminator —
    two cron fires of one schedule must not collide onto a single name."""
    one = sn.build_task_name("echo", _turn(id="t-0001", origin_ref={"schedule_name": "Nightly sweep"}))
    two = sn.build_task_name("echo", _turn(id="t-0002", origin_ref={"schedule_name": "Nightly sweep"}))
    assert one != two
    assert one.startswith("c-nightly-sweep-")


# --- shape ------------------------------------------------------------------

def test_long_subjects_truncate_on_a_word_boundary():
    """`hal-alarm-labs-jj-web-cpu-high-i-...` cut mid-word. Names are read, not parsed."""
    t = _turn(origin="email", origin_ref={
        "subject": "Alarm: labs-jj-web CPU utilization is critically high right now",
        "thread_id": "aaaaea9d"})
    name = sn.build_task_name("hal", t)
    assert name == "c-alarm-labs-jj-web-cpu-utilization-ea9d"
    assert not name.replace("-ea9d", "").endswith("-")


def test_names_are_lowercase_slug_safe():
    t = _turn(origin_ref={"item_title": "Ship  the  “Fix”: réponse (v2)!", "thread_id": "aaaa0001"})
    name = sn.build_task_name("ace", t)
    assert name == "c-ship-the-fix-r-ponse-v2-0001"


@pytest.mark.parametrize("subject", ["", "   ", "!!!", "---"])
def test_an_empty_or_punctuation_only_subject_does_not_produce_a_dangling_name(subject):
    t = _turn(origin="email", origin_ref={"subject": subject, "thread_id": "aaaa0001"})
    assert sn.build_task_name("hal", t) == "c-email-0001"


def test_the_agent_prefix_is_gone_because_emdash_groups_by_project():
    name = sn.build_task_name("ace", _turn(prompt="/ace:issue-triage"))
    assert not name.startswith("c-ace-")


def test_no_timestamp_in_the_name():
    """emdash shows created-at in its own column; ten characters of MMDD-HHMM were
    the other half of why subjects got truncated."""
    import re
    name = sn.build_task_name("hal", _turn(origin="email", origin_ref={"subject": "Security alert"}))
    assert not re.search(r"-\d{4}-\d{4}$", name)


# --- parse ------------------------------------------------------------------

def test_parse_round_trips_what_build_produced():
    t = _turn(origin="email", origin_ref={"subject": "Security alert", "thread_id": "19f4c06eeb986355"})
    name = sn.build_task_name("hal", t)
    assert sn.parse_task_name(name) == {"subject": "security-alert", "disc": "6355"}


def test_parse_declines_a_human_name():
    assert sn.parse_task_name("improve-session-names") is None


# --- the discriminator is a fixed-width field, not just entropy ---------------

def test_the_discriminator_is_always_exactly_four_characters():
    """Load-bearing outside this module: canopy recovers the task id from the
    worktree directory by telling this 4-char tail from emdash's 5-char de-dupe
    suffix. A 3-char disc would make `c-foo-abc-1me7x` unsplittable."""
    for turn in (
        _turn(origin_ref={"thread_id": "a"}),
        _turn(origin_ref={"thread_key": "x:y"}),
        _turn(id="1", origin_ref={}),
        _turn(origin_ref={"thread_id": "19f4c06eeb986355"}),
    ):
        name = sn.build_task_name("hal", turn)
        assert len(name.rsplit("-", 1)[1]) == sn.DISC_LEN, name


def test_a_short_thread_key_still_agrees_with_itself():
    """Reuse re-derives the name, so a padded disc must be deterministic and stay
    tied to the thread — not random."""
    t = _turn(origin_ref={"thread_key": "x:y"})
    assert sn.build_task_name("hal", t) == sn.build_task_name("hal", dict(t))


def test_a_worktree_suffix_can_be_told_from_the_discriminator():
    """The property canopy's emdash_task_from_cwd depends on."""
    name = sn.build_task_name("ace", _turn(prompt="/ace:issue-triage",
                                           origin_ref={"thread_id": "zz4a4e"}))
    worktree = f"{name}-1me7x"          # emdash's 5-char de-dupe suffix
    assert worktree.rsplit("-", 1)[0] == name
