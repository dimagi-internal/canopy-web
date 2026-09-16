"""AG-UI's `RunAgentInput` as the one way to declare a page.

canopy has two endpoints for this — `page-state` and `page-actions` — which is
fine for canopy's own widget and a poor answer for anyone else: a host
integrating from outside would have to learn two canopy-specific shapes to say
what every AG-UI client already knows how to say. This accepts the protocol's
own object.

The test that matters most is the LAST one: a payload built by the real
`ag_ui.core.RunAgentInput` must be accepted as-is. Asserting against a dict this
repo wrote would prove only that canopy agrees with itself.
"""

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    session = Session.objects.create(workspace=ws, created_by=user, title="chat")
    c = Client()
    c.force_login(user)
    return session, c


def _put(c, session, body):
    return c.put(
        f"/api/canopy-sessions/{session.id}/run-input",
        data=body, content_type="application/json",
    )


def test_one_call_declares_both_the_view_and_the_actions():
    session, c = _ctx()

    resp = _put(c, session, {
        "state": {"path": "/insights", "resource": "insight://", "visible_ids": [1, 2]},
        "tools": [{"name": "scrollToInsight", "description": "Scroll to a row"}],
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"]["visible_ids"] == [1, 2]
    assert [t["name"] for t in body["tools"]] == ["scrollToInsight"]


def test_it_reports_what_was_honoured_rather_than_echoing_the_input():
    """The server assigns the version, so a client can tell its declaration
    landed instead of assuming."""
    session, c = _ctx()

    first = _put(c, session, {"state": {"path": "/a"}, "tools": []}).json()
    second = _put(c, session, {"state": {"path": "/b"}, "tools": []}).json()

    assert second["version"] > first["version"]


def test_fields_canopy_cannot_honour_are_ignored_not_rejected():
    """A conforming client sends the WHOLE object. Refusing it because canopy has
    no use for `resume` would make the protocol's own payload invalid here —
    which defeats the point of speaking it."""
    session, c = _ctx()

    resp = _put(c, session, {
        "state": {"path": "/insights"},
        "tools": [],
        "threadId": "t1", "runId": "r1", "parentRunId": None,
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "context": [{"description": "tz", "value": "UTC"}],
        "forwardedProps": {"anything": True},
        "resume": [{"interruptId": "i1", "status": "resolved"}],
    })

    assert resp.status_code == 200


def test_a_page_sending_rows_instead_of_a_selection_is_still_refused():
    """The bound does not weaken just because the payload arrived in AG-UI's
    shape — the point is that the agent re-reads rows itself."""
    session, c = _ctx()
    fat = {"rows": [{"id": n, "text": "x" * 400} for n in range(60)]}

    resp = _put(c, session, {"state": fat, "tools": []})

    assert resp.status_code == 422
    assert "too_large" in resp.json()["detail"]


def test_an_empty_state_does_not_blank_a_page_that_already_declared_one():
    """`tools` and `state` are independent: re-declaring tools must not be read
    as "my screen is now empty"."""
    session, c = _ctx()
    _put(c, session, {"state": {"path": "/insights", "resource": "insight://"}, "tools": []})

    resp = _put(c, session, {"tools": [{"name": "scrollTo"}]})

    assert resp.json()["state"]["path"] == "/insights"


def test_another_users_session_is_not_writable():
    session, _c = _ctx()
    stranger = User.objects.create_user("nope", "nope@dimagi.com", "pw")
    other = Client()
    other.force_login(stranger)

    assert _put(other, session, {"state": {"path": "/x"}}).status_code == 404


def test_a_payload_built_by_the_real_sdk_is_accepted():
    """THE test. Anything else proves only that canopy agrees with itself.

    Built with `ag_ui.core.RunAgentInput` and serialised `by_alias` — the
    camelCase every non-Python AG-UI client sends.
    """
    from ag_ui.core.types import RunAgentInput, Tool

    session, c = _ctx()
    payload = RunAgentInput(
        thread_id=str(session.id),
        run_id="run-1",
        state={"path": "/insights", "resource": "insight://", "visible_ids": [7]},
        messages=[],
        tools=[Tool(name="scrollToInsight", description="Scroll to a row",
                    parameters={"type": "object", "properties": {}})],
        context=[],
        forwarded_props=None,
    )

    resp = _put(c, session, json.loads(payload.model_dump_json(by_alias=True)))

    assert resp.status_code == 200, resp.content[:400]
    assert resp.json()["state"]["visible_ids"] == [7]
    assert [t["name"] for t in resp.json()["tools"]] == ["scrollToInsight"]
