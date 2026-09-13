"""Calling into the page a user is looking at.

The failure this design exists to prevent is an agent believing it acted. A
control frame published to a group with nobody listening is silently discarded
(`RunnerBinding.pending_answer`'s docstring calls that "the purest form of
clicking does nothing"), and for a page action the consequence is worse: the
agent reports closing twelve insights that are all still there.

So most of these tests are about refusals — no page, unknown action, bad
arguments, a host that says no, and a tab that never answers. Each must reach
the caller as an error carrying a reason, never as a completed action.
"""

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import page_actions
from apps.canopy_sessions.models import PageAction, Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

DISMISS = {
    "name": "dismissInsights",
    "description": "Dismiss the given insights from the page the user is viewing",
    "parameters": {
        "type": "object",
        "properties": {"ids": {"type": "array"}, "reason": {"type": "string"}},
        "required": ["ids"],
    },
}


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    session = Session.objects.create(workspace=ws, created_by=user, title="chat")
    c = Client()
    c.force_login(user)
    return user, session, c


def _fast(monkeypatch, seconds=0.5):
    """Shrink the wait so a timeout test takes half a second, not twenty."""
    monkeypatch.setattr(page_actions, "DEFAULT_TIMEOUT_SECONDS", seconds)
    monkeypatch.setattr(page_actions, "POLL_INTERVAL_SECONDS", 0.02)


def _answer_during_the_wait(monkeypatch, *, result=None, error=""):
    """Resolve the pending action from inside the poll loop's own sleep.

    Threads would be truer to life but SQLite locks the table under concurrent
    writes, which fails the test for a reason that has nothing to do with the
    code. Hooking the sleep is deterministic, single-threaded and fast, and it
    exercises the same path: the row is still PENDING when the waiter starts,
    and resolved by the time it next reads.
    """
    state = {"done": False}

    def fake_sleep(_seconds):
        if state["done"]:
            return
        action = PageAction.objects.filter(status=PageAction.PENDING).first()
        if action:
            page_actions.resolve(action, result=result, error=error)
            state["done"] = True

    monkeypatch.setattr(page_actions.time, "sleep", fake_sleep)


# --- declaring what the page can do -----------------------------------------


def test_a_page_declares_its_actions_and_the_agent_can_discover_them():
    _user, session, c = _ctx()

    c.put(f"/api/canopy-sessions/{session.id}/page-actions",
          data={"actions": [DISMISS]}, content_type="application/json")

    listed = c.get(f"/api/canopy-sessions/{session.id}/page-actions").json()
    assert [a["name"] for a in listed] == ["dismissInsights"]
    # The SCHEMA is the point: without it an agent cannot know the call takes
    # `ids`, and would have to be told in prose.
    assert listed[0]["parameters"]["required"] == ["ids"]


def test_declaring_replaces_rather_than_merges():
    """A leftover action from the page the user navigated away from is one the
    agent would call into nothing."""
    _user, session, c = _ctx()
    c.put(f"/api/canopy-sessions/{session.id}/page-actions",
          data={"actions": [DISMISS]}, content_type="application/json")

    c.put(f"/api/canopy-sessions/{session.id}/page-actions",
          data={"actions": [{"name": "somethingElse"}]}, content_type="application/json")

    listed = c.get(f"/api/canopy-sessions/{session.id}/page-actions").json()
    assert [a["name"] for a in listed] == ["somethingElse"]


def test_a_session_with_no_page_declares_nothing():
    """Not an error — an agent working alone has no page, and that is normal."""
    _user, session, c = _ctx()
    assert c.get(f"/api/canopy-sessions/{session.id}/page-actions").json() == []


# --- the refusals -----------------------------------------------------------


def test_no_page_attached_refuses_and_says_so(monkeypatch):
    _fast(monkeypatch)
    user, session, _c = _ctx()
    with pytest.raises(page_actions.PageActionError) as exc:
        page_actions.request_action(session=session, name="dismissInsights",
                                    args={"ids": [1]}, user=user)
    assert exc.value.code == "no_page"
    assert "cannot be queued" in exc.value.message


def test_an_action_the_page_does_not_offer_is_refused_with_the_list(monkeypatch):
    _fast(monkeypatch)
    user, session, _c = _ctx()
    page_actions.set_declared_actions(session, [DISMISS])

    with pytest.raises(page_actions.PageActionError) as exc:
        page_actions.request_action(session=session, name="deleteEverything",
                                    args={}, user=user)

    assert exc.value.code == "unknown_action"
    # Naming what IS available turns a dead end into a correction.
    assert "dismissInsights" in exc.value.message


def test_a_missing_required_argument_is_caught_before_the_round_trip(monkeypatch):
    _fast(monkeypatch)
    user, session, _c = _ctx()
    page_actions.set_declared_actions(session, [DISMISS])

    with pytest.raises(page_actions.PageActionError) as exc:
        page_actions.request_action(session=session, name="dismissInsights",
                                    args={"reason": "stale"}, user=user)

    assert exc.value.code == "bad_arguments"
    assert "ids" in exc.value.message
    assert not PageAction.objects.exists(), "no row should be written for a call the page would reject"


def test_a_wrongly_typed_argument_is_caught():
    user, session, _c = _ctx()
    page_actions.set_declared_actions(session, [DISMISS])
    with pytest.raises(page_actions.PageActionError) as exc:
        page_actions.request_action(session=session, name="dismissInsights",
                                    args={"ids": "not-a-list"}, user=user)
    assert exc.value.code == "bad_arguments"


def test_a_boolean_is_not_accepted_as_a_number():
    """`bool` subclasses `int` in Python, so an integer field silently accepts
    True unless that is excluded on purpose."""
    user, session, _c = _ctx()
    page_actions.set_declared_actions(session, [{
        "name": "setLimit",
        "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}},
    }])
    with pytest.raises(page_actions.PageActionError) as exc:
        page_actions.request_action(session=session, name="setLimit",
                                    args={"n": True}, user=user)
    assert exc.value.code == "bad_arguments"


def test_a_page_that_never_answers_times_out_rather_than_hanging(monkeypatch):
    """THE case. A closed tab must read as "the page is not open", never as a
    completed action."""
    _fast(monkeypatch, seconds=0.4)
    user, session, _c = _ctx()
    page_actions.set_declared_actions(session, [DISMISS])

    with pytest.raises(page_actions.PageActionError) as exc:
        page_actions.request_action(session=session, name="dismissInsights",
                                    args={"ids": [1]}, user=user)

    assert exc.value.code == "timeout"
    assert "closed" in exc.value.message
    # And the row records it, so an unanswered action is visible afterwards
    # rather than sitting pending forever.
    assert PageAction.objects.get().status == PageAction.EXPIRED


def test_a_host_refusal_reaches_the_agent_as_an_error_not_a_success(monkeypatch):
    """A host throws to refuse. If that arrived as a completed action with a
    falsy result, the agent would carry on as though the page had changed."""
    _fast(monkeypatch, seconds=2)
    user, session, _c = _ctx()
    page_actions.set_declared_actions(session, [DISMISS])
    _answer_during_the_wait(monkeypatch, error="this insight is already closed")

    with pytest.raises(page_actions.PageActionError) as exc:
        page_actions.request_action(session=session, name="dismissInsights",
                                    args={"ids": [1]}, user=user)

    assert exc.value.code == "refused"
    assert "already closed" in exc.value.message


# --- the happy path ---------------------------------------------------------


def test_a_page_that_answers_returns_its_result(monkeypatch):
    _fast(monkeypatch, seconds=2)
    user, session, _c = _ctx()
    page_actions.set_declared_actions(session, [DISMISS])
    _answer_during_the_wait(monkeypatch, result={"dismissed": 3})

    action = page_actions.request_action(session=session, name="dismissInsights",
                                         args={"ids": [1, 2, 3]}, user=user)

    assert action.status == PageAction.DONE
    assert action.result == {"dismissed": 3}
    # Who it ran as matters: the page executes in this user's browser session,
    # so this is the identity whose permissions actually applied.
    assert action.requested_for == user


def test_a_late_answer_cannot_resolve_an_action_already_expired():
    """The caller has already been told it timed out. A retried POST must not
    flip that to success afterwards."""
    user, session, _c = _ctx()
    action = PageAction.objects.create(
        session=session, name="dismissInsights", args={"ids": [1]},
        requested_for=user, status=PageAction.EXPIRED, resolved_at=timezone.now(),
    )
    page_actions.resolve(action, result={"dismissed": 3})
    action.refresh_from_db()
    assert action.status == PageAction.EXPIRED
    assert action.result is None


def test_first_answer_wins():
    user, session, _c = _ctx()
    action = PageAction.objects.create(session=session, name="x", requested_for=user)
    page_actions.resolve(action, result={"first": True})
    page_actions.resolve(action, result={"second": True})
    action.refresh_from_db()
    assert action.result == {"first": True}


# --- the HTTP surface -------------------------------------------------------


def test_invoke_refuses_over_http_with_a_readable_code(monkeypatch):
    _fast(monkeypatch)
    _user, session, c = _ctx()
    r = c.post(f"/api/canopy-sessions/{session.id}/page-actions/invoke",
               data={"name": "whatever", "args": {}}, content_type="application/json")
    assert r.status_code == 409
    assert "no_page" in r.content.decode()


def test_bad_arguments_are_a_422_not_a_409(monkeypatch):
    """A malformed call is the caller's fault; an absent page is not."""
    _fast(monkeypatch)
    _user, session, c = _ctx()
    page_actions.set_declared_actions(session, [DISMISS])
    r = c.post(f"/api/canopy-sessions/{session.id}/page-actions/invoke",
               data={"name": "dismissInsights", "args": {}}, content_type="application/json")
    assert r.status_code == 422


def test_a_page_cannot_resolve_an_action_on_another_session():
    user, session, c = _ctx()
    other = Session.objects.create(workspace=session.workspace, created_by=user, title="other")
    action = PageAction.objects.create(session=other, name="x", requested_for=user)

    r = c.post(f"/api/canopy-sessions/{session.id}/page-actions/{action.id}/result",
               data={"result": {"done": True}}, content_type="application/json")

    assert r.status_code == 404
    action.refresh_from_db()
    assert action.status == PageAction.PENDING


def test_a_non_member_cannot_declare_or_invoke():
    _user, session, _c = _ctx()
    outsider = User.objects.create_user("out", "out@dimagi.com", "pw")
    c = Client()
    c.force_login(outsider)
    assert c.get(f"/api/canopy-sessions/{session.id}/page-actions").status_code == 404
    assert c.put(f"/api/canopy-sessions/{session.id}/page-actions",
                 data={"actions": []}, content_type="application/json").status_code == 404


def test_an_unknown_action_id_is_404_not_a_silent_ok():
    _user, session, c = _ctx()
    r = c.post(f"/api/canopy-sessions/{session.id}/page-actions/{uuid.uuid4()}/result",
               data={"result": {}}, content_type="application/json")
    assert r.status_code == 404
