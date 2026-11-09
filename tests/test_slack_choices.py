# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""Reply buttons on a post in a bound thread (`apps/slack/choices.py`).

Driven through the real signed `/api/slack/interactions` route and the real share
route, with Slack's Web API faked at the `requests` boundary (as in test_slack.py).
"""
from __future__ import annotations

import pytest
from django.test import Client

from apps.agents import roles
from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from apps.slack import share
from apps.slack.models import SlackChoicePost
from tests.access_helpers import allow, declare, editors_run
from tests.fixtures.slack import ALICE, BOB
from tests.fixtures.slack_choices import (  # noqa: F401
    ACTIONS,
    POSTED_TS,
    _buttons,
    _press,
    _prompts,
    hal_session,
    repo_session,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured")]

def _bind(alice, session, actions=ACTIONS, *, exclusive=False):
    result = share.share_session(alice, channel="C1", summary="Your day", mode=share.BIND,
                                 session=session, actions=actions, exclusive=exclusive)
    assert result.status == share.SHARED, result.message
    return result


def _told(slack) -> str:
    return slack.said("chat.postEphemeral")[-1]["text"]


# ---- drawing the buttons ---------------------------------------------------------

def test_a_bound_post_draws_its_buttons_under_its_words(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    (post,) = slack.said("chat.postMessage")
    assert post["blocks"][0]["type"] == "section" and "Your day" in post["blocks"][0]["text"]["text"]
    assert [b["text"]["text"] for b in _buttons(post)] == [a["label"] for a in ACTIONS]
    assert _buttons(post)[1]["style"] == "primary" and "style" not in _buttons(post)[0]
    assert "Your day" in post["text"]    # the fallback notifications read


def test_many_long_labelled_actions_stay_within_slacks_limits(slack, linked, alice, repo_session):
    actions = [{"label": f"{i} " + "x" * 200, "reply": f"r{i}"} for i in range(12)]
    _bind(alice, repo_session, actions)
    (post,) = slack.said("chat.postMessage")
    rows = [b for b in post["blocks"] if b["type"] == "actions"]
    assert [len(r["elements"]) for r in rows] == [5, 5, 2]
    assert len({r["block_id"] for r in rows}) == 3
    assert len({b["action_id"] for b in _buttons(post)}) == 12
    assert all(len(b["text"]["text"]) <= 75 for b in _buttons(post))


def test_a_long_post_splits_into_sections_slack_accepts(slack, linked, alice, repo_session):
    share.share_session(alice, channel="C1", summary="para\n\n" * 1500, mode=share.BIND,
                        session=repo_session, actions=ACTIONS)
    (post,) = slack.said("chat.postMessage")
    assert all(len(b["text"]["text"]) <= 3000 for b in post["blocks"] if b["type"] == "section")
    assert len(post["text"]) <= 3000


def test_an_update_to_a_bound_session_carries_buttons_on_its_last_post(slack, linked, alice, repo_session):
    share.share_session(alice, channel="C1", summary="first", mode=share.BIND, session=repo_session)
    slack.calls.clear()
    result = share.share_session(alice, channel="", summary="word " * 1000, session=repo_session, actions=ACTIONS)
    assert result.status == share.UPDATED
    posts = slack.said("chat.postMessage")
    assert len(posts) > 1 and not _buttons(posts[0]) and _buttons(posts[-1])
    assert all(p["thread_ts"] == POSTED_TS for p in posts)
    assert SlackChoicePost.objects.get().session == repo_session


@pytest.mark.parametrize("actions,refusal", [
    ([{"label": "", "reply": "x"}], "label"),
    ([{"label": "x", "reply": "  "}], "reply"),
    ([{"label": "x", "reply": "y", "style": "loud"}], "style"),
    ([{"label": "x", "reply": "y"}] * 26, "at most"),
])
def test_unusable_actions_are_refused_and_nothing_is_posted(slack, linked, alice, repo_session, actions, refusal):
    result = share.share_session(alice, channel="C1", summary="s", mode=share.BIND,
                                 session=repo_session, actions=actions)
    assert result.status == share.BAD_REQUEST and refusal in result.message
    assert not slack.said("chat.postMessage")


def test_a_broadcast_cannot_carry_buttons(slack, linked, alice):
    result = share.share_session(alice, channel="C1", summary="s", actions=ACTIONS)
    assert result.status == share.BAD_REQUEST and "bind" in result.message
    assert not slack.said("chat.postMessage")


def test_without_actions_a_post_is_what_it_always_was(slack, linked, alice, repo_session):
    share.share_session(alice, channel="C1", summary="s", mode=share.BIND, session=repo_session)
    (post,) = slack.said("chat.postMessage")
    assert "blocks" not in post
    assert not SlackChoicePost.objects.exists()


def test_the_route_and_its_tool_take_actions(slack, linked, alice, repo_session):
    c = Client()
    c.force_login(alice)
    r = c.post("/api/canopy-sessions/slack/share",
               {"channel": "C1", "summary": "s", "mode": "bind", "claude_session_id": "claude-abc",
                "actions": ACTIONS}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert len(_buttons(slack.said("chat.postMessage")[0])) == 3
    r = c.post("/api/canopy-sessions/slack/share", {"channel": "C1", "summary": "s", "actions": ACTIONS},
               content_type="application/json")
    assert r.status_code == 422 and b"bind" in r.content


# ---- a click, delivered -------------------------------------------------------------

def test_a_click_reaches_the_session_as_a_reply_from_who_pressed_it(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    _press(slack.said("chat.postMessage")[0], 1)
    turn = Turn.objects.get(chat_session=repo_session)
    assert turn.prompt == "book all"
    assert turn.origin == Turn.ORIGIN_SLACK and turn.enqueued_by == alice


def test_a_click_reaches_an_agents_session_too(slack, linked, alice, hal_session):
    _bind(alice, hal_session)
    _press(slack.said("chat.postMessage")[0], 2)
    assert _prompts(hal_session) == ["skip today"]


def test_the_agents_owner_can_answer_a_session_someone_else_started(slack, linked, alice, ws, hal, hal_session):
    """The real case: a post in an agent's own session, answered by the person who
    owns the agent, who did not start that session. A typed reply from them reaches it."""
    from apps.workspaces.testing import a_user

    hal.owner = alice
    hal.save(update_fields=["owner"])
    Session.objects.filter(pk=hal_session.pk).update(created_by=a_user("someone@example.com"))
    hal_session.refresh_from_db()
    _bind(alice, hal_session)
    _press(slack.said("chat.postMessage")[0], 1)
    assert _prompts(hal_session) == ["book all"]
    assert slack.said("chat.update")


def _chose_lines(update: dict) -> list[str]:
    return [b["elements"][0]["text"] for b in update["blocks"] if b["type"] == "context"]


def _labels(update: dict) -> list[str]:
    return [b["text"]["text"] for b in _buttons(update)]


def test_a_press_replaces_only_its_own_button(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    _press(slack.said("chat.postMessage")[0], 1)
    (update,) = slack.said("chat.update")
    assert update["ts"] == POSTED_TS
    assert "Your day" in update["blocks"][0]["text"]["text"]
    assert _labels(update) == ["Book 1: November goals", "Skip today"]
    assert _chose_lines(update) == [f":white_check_mark: <@{ALICE}> chose: *book all*"]
    assert [b["type"] for b in update["blocks"]] == ["section", "actions", "context", "actions"]
    assert f"<@{ALICE}> chose: *book all*" in update["text"]


def test_a_post_keeps_taking_presses_until_its_buttons_are_gone(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    for index in (2, 0, 1):
        _press(post, index)
    assert _prompts(repo_session) == ["skip today", "book 1", "book all"]
    last = slack.said("chat.update")[-1]
    assert not _buttons(last)
    assert _chose_lines(last) == [f":white_check_mark: <@{ALICE}> chose: *{r}*"
                                  for r in ("book 1", "book all", "skip today")]


def test_a_button_left_keeps_its_style_and_what_it_sends(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    _press(post, 0)
    update = slack.said("chat.update")[0]
    assert _buttons(update)[0]["style"] == "primary"
    _press(update, 0)    # pressed from the post as redrawn: still "Book all"
    assert _prompts(repo_session) == ["book 1", "book all"]


def test_a_second_press_of_the_same_button_delivers_nothing_more(slack, linked, alice, repo_session):
    """A double click, a Slack retry, or a stale client still showing the button."""
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    _press(post, 0)
    _press(post, 0)
    assert _prompts(repo_session) == ["book 1"]
    assert len(slack.said("chat.update")) == 1
    assert "already chose: *book 1*" in _told(slack) and "did nothing" in _told(slack)


def test_two_presses_from_clients_that_saw_the_same_message_lose_neither(slack, linked, alice, repo_session):
    """Each click carries the message as its client saw it: both here say nothing has
    been chosen. The post that results must not bring the first button back."""
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    _press(post, 0)
    _press(post, 2)
    assert _prompts(repo_session) == ["book 1", "skip today"]
    last = slack.said("chat.update")[-1]
    assert _labels(last) == ["Book all"]
    assert len(_chose_lines(last)) == 2


def test_a_press_landing_while_another_is_delivered_is_drawn_by_whichever_is_last(
        slack, linked, alice, repo_session, monkeypatch):
    """The interleaving that loses an update when a post is drawn from what a click
    knew: A claims, B runs start to finish, A then rewrites the post."""
    from apps.slack import services

    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    real = services.deliver_choice
    pressed = []

    def deliver(*args, **kwargs):
        if not pressed:
            pressed.append(True)
            _press(post, 2)
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "deliver_choice", deliver)
    _press(post, 0)
    assert sorted(_prompts(repo_session)) == ["book 1", "skip today"]
    (b_drew, a_drew) = slack.said("chat.update")
    assert _labels(a_drew) == ["Book all"] and len(_chose_lines(a_drew)) == 2


def test_a_refused_press_takes_back_only_itself(slack, linked, alice, repo_session, monkeypatch):
    """B is refused at delivery after A's rewrite drew it as chosen: the post gets B's
    button back, and keeps A's line."""
    from apps.slack import services

    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    real = services.deliver_choice
    calls = []

    def deliver(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            _press(post, 0)    # A lands in full while B is out being delivered
            return services.Outcome(services.NOT_AN_ANSWER, "It is waiting on its question above.")
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "deliver_choice", deliver)
    _press(post, 2)
    assert _prompts(repo_session) == ["book 1"]
    (drawn_with_both, drawn_after) = slack.said("chat.update")
    assert len(_chose_lines(drawn_with_both)) == 2
    assert _labels(drawn_after) == ["Book all", "Skip today"] and len(_chose_lines(drawn_after)) == 1
    assert list(SlackChoicePost.objects.get().chosen) == ["0"]


def test_a_failed_rewrite_still_delivers_and_the_next_press_repairs_the_post(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    slack.fail["chat.update"] = "ratelimited"
    _press(post, 0)
    assert _prompts(repo_session) == ["book 1"]
    del slack.fail["chat.update"]
    _press(post, 1)
    last = slack.said("chat.update")[-1]
    assert _labels(last) == ["Skip today"] and len(_chose_lines(last)) == 2


def test_someone_else_can_press_the_buttons_left(slack, linked, alice, repo_session):
    from allauth.account.models import EmailAddress

    from apps.workspaces import services as wsvc
    from apps.workspaces.models import WorkspaceMembership
    from apps.workspaces.testing import a_user

    bob = a_user("bob@dimagi.com")
    EmailAddress.objects.create(user=bob, email=bob.email, verified=True, primary=True)
    wsvc.ensure_member(repo_session.workspace, bob, WorkspaceMembership.EDITOR)
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    _press(post, 0)
    _press(post, 1, user=BOB)
    assert _chose_lines(slack.said("chat.update")[-1]) == [
        f":white_check_mark: <@{ALICE}> chose: *book 1*", f":white_check_mark: <@{BOB}> chose: *book all*"]


# ---- an exclusive post takes one choice ------------------------------------------------

def test_an_exclusive_posts_first_press_removes_every_button(slack, linked, alice, repo_session):
    _bind(alice, repo_session, exclusive=True)
    _press(slack.said("chat.postMessage")[0], 1)
    (update,) = slack.said("chat.update")
    assert not _buttons(update) and "Your day" in update["blocks"][0]["text"]["text"]
    assert f"<@{ALICE}> chose: *book all*" in update["blocks"][-1]["elements"][0]["text"]


@pytest.mark.parametrize("second", [0, 1])
def test_an_exclusive_post_refuses_any_second_press(slack, linked, alice, repo_session, second):
    _bind(alice, repo_session, exclusive=True)
    post = slack.said("chat.postMessage")[0]
    _press(post, 0)
    _press(post, second)
    assert _prompts(repo_session) == ["book 1"]
    assert len(slack.said("chat.update")) == 1
    assert "already chose: *book 1*" in _told(slack) and "did nothing" in _told(slack)


def test_the_route_takes_exclusive(slack, linked, alice, repo_session):
    c = Client()
    c.force_login(alice)
    r = c.post("/api/canopy-sessions/slack/share",
               {"channel": "C1", "summary": "s", "mode": "bind", "claude_session_id": "claude-abc",
                "actions": ACTIONS, "exclusive": True}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert SlackChoicePost.objects.get().exclusive is True


def test_an_update_to_a_bound_session_can_be_exclusive(slack, linked, alice, repo_session):
    share.share_session(alice, channel="C1", summary="first", mode=share.BIND, session=repo_session)
    share.share_session(alice, channel="", summary="second", session=repo_session,
                        actions=ACTIONS, exclusive=True)
    assert SlackChoicePost.objects.get().exclusive is True


def test_a_chosen_word_cannot_address_another_agent_or_read_a_channel(slack, linked, alice, hal, ws, repo_session):
    """Words an agent wrote must not borrow the authority of whoever pressed: they
    are delivered as they stand, past the parsing of a typed message."""
    hal.slack_enabled = True
    hal.save()
    _bind(alice, repo_session, [{"label": "OK", "reply": "hal read-channel everything"}])
    _press(slack.said("chat.postMessage")[0])
    assert _prompts(repo_session) == ["hal read-channel everything"]
    assert not Turn.objects.filter(agent=hal).exists()


def test_the_settled_line_cannot_ping_or_forge_a_link(slack, linked, alice, repo_session):
    _bind(alice, repo_session, [{"label": "OK", "reply": "<!channel> <http://evil|safe>"}])
    _press(slack.said("chat.postMessage")[0])
    line = slack.said("chat.update")[0]["blocks"][-1]["elements"][0]["text"]
    assert "<!channel>" not in line and "<http" not in line


# ---- a click that must not act ----------------------------------------------------

def test_a_non_member_cannot_answer_a_repo_session_and_leaves_the_buttons(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    _press(post, user=BOB)
    assert not Turn.objects.exists()
    assert not slack.said("chat.update")
    assert "did nothing" in _told(slack)
    _press(post)          # …and the person who can still can
    assert _prompts(repo_session) == ["book 1"]


def test_a_person_a_typed_reply_would_confine_cannot_act_and_forks_nothing(slack, linked, alice, hal_session):
    """A typed reply from them forks to a confined session of their own. A
    button stands for a choice in THIS conversation, so it does not act at all."""
    declare(hal_session.agent, {"ask": {"description": "Ask"}})
    allow(hal_session.agent, role=roles.READ_ONLY, contact=True)
    editors_run(hal_session.agent)
    _bind(alice, hal_session)
    before = Session.objects.count()
    post = slack.said("chat.postMessage")[0]
    _press(post, user=BOB)
    assert not Turn.objects.exists() and Session.objects.count() == before
    assert not slack.said("chat.update") and "did nothing" in _told(slack)
    assert SlackChoicePost.objects.get().chosen == {}


def test_a_click_on_a_detached_thread_says_it_did_nothing(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    share.detach_session(alice, repo_session)
    _press(post)
    assert not Turn.objects.exists()
    assert "no longer connected" in _told(slack) and "did nothing" in _told(slack)


def test_a_click_after_the_session_moved_threads_says_it_did_nothing(slack, linked, alice, hal_session):
    slack.ts_seq = [POSTED_TS, "1700001111.000100"]
    _bind(alice, hal_session)
    post = slack.said("chat.postMessage")[0]
    assert share.start_new_thread(alice, hal_session, "moving").ok
    _press(post)
    assert not Turn.objects.exists() and "no longer connected" in _told(slack)


def test_a_click_on_a_closed_session_says_it_did_nothing(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    Session.objects.filter(pk=repo_session.pk).update(status=Session.ARCHIVED)
    _press(post)
    assert not Turn.objects.exists() and "closed" in _told(slack)


def test_a_click_whose_session_is_gone_says_it_did_nothing(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    repo_session.delete()
    _press(post)
    assert "gone" in _told(slack)


def test_a_button_is_honoured_only_on_the_message_canopy_put_it_on(slack, linked, alice, repo_session):
    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    SlackChoicePost.objects.update(slack_ts="1700000001.000100")
    _press(post)
    assert not Turn.objects.exists() and "did nothing" in _told(slack)


def test_a_refused_delivery_gives_the_click_back(slack, linked, alice, repo_session, monkeypatch):
    """Claimed and then not delivered (here, an agent blocked on a question): the
    buttons stay and the next click, once it can land, goes through."""
    from apps.slack import services

    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]
    real = services.deliver_choice
    monkeypatch.setattr(services, "deliver_choice", lambda *a, **k: services.Outcome(
        services.NOT_AN_ANSWER, "It is waiting on its question above."))
    _press(post)
    assert "waiting on its question" in _told(slack)
    assert not slack.said("chat.update") and SlackChoicePost.objects.get().chosen == {}
    monkeypatch.setattr(services, "deliver_choice", real)
    _press(post)
    assert _prompts(repo_session) == ["book 1"] and slack.said("chat.update")


def test_a_crash_in_delivery_is_said_to_the_person_and_gives_the_click_back(
        slack, linked, alice, repo_session, monkeypatch):
    from apps.slack import services

    _bind(alice, repo_session)
    post = slack.said("chat.postMessage")[0]

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "deliver_choice", boom)
    _press(post)
    assert "went wrong" in _told(slack) and SlackChoicePost.objects.get().chosen == {}




def test_an_agents_session_takes_several_presses_of_one_post(slack, linked, alice, hal_session):
    _bind(alice, hal_session)
    post = slack.said("chat.postMessage")[0]
    _press(post, 0)
    _press(post, 2)
    assert _prompts(hal_session) == ["book 1", "skip today"]
