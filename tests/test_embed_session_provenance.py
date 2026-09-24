"""A session created through an embedded app is stamped with WHICH app, by the server.

The widget has to be able to ask "my previous conversations here" without being
able to ask "someone else's conversations there". `metadata.origin_key` already
scopes a session list, but it is supplied by the CALLER — ace-web derives it
server-side from a membership-checked path, which is safe for ace-web and is
not a property canopy can rely on from an arbitrary embedding host.

So `embed_app` is stamped from the DELEGATED TOKEN, which the caller cannot
choose, and any client-supplied value is discarded. `origin_key` is deliberately
left alone: it is a finer, host-chosen scope (ace-web uses one per ace
workspace) and overriding it would break that with no gain.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.tokens.models import AppCredential, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx(app_name="connect-labs"):
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="labs-helper", name="Labs Helper", workspace=ws)
    admin = User.objects.create_user(f"a-{app_name}", f"a-{app_name}@dimagi.com", "pw")
    app = AppCredential.create_credential(name=app_name, created_by=admin, workspace=ws)
    return user, ws, agent, app


def _bearer(app, user):
    raw, _tok = DelegatedToken.issue(app=app, user=user, ttl_seconds=3600)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _create(headers, body=None):
    return Client().post(
        "/api/canopy-sessions/",
        data=body if body is not None else {"agent_slug": "labs-helper"},
        content_type="application/json",
        **headers,
    )


def test_a_delegated_create_is_stamped_with_the_acting_app():
    user, _ws, _agent, app = _ctx()
    r = _create(_bearer(app, user))
    assert r.status_code == 200
    session = Session.objects.get(pk=r.json()["id"])
    assert session.metadata["embed_app"] == "connect-labs"


def test_a_client_cannot_claim_a_different_app():
    """The whole point. Supplying embed_app must not let one host's widget list
    or create under another host's name."""
    user, _ws, _agent, app = _ctx()
    r = _create(
        _bearer(app, user),
        {"agent_slug": "labs-helper", "metadata": {"embed_app": "ace-web"}},
    )
    session = Session.objects.get(pk=r.json()["id"])
    assert session.metadata["embed_app"] == "connect-labs"


def test_a_browser_session_is_not_stamped_at_all():
    """There is no app behind a cookie, and inventing one would put canopy's own
    chats into an embedder's list."""
    user, _ws, _agent, _app = _ctx()
    c = Client()
    c.force_login(user)
    r = c.post("/api/canopy-sessions/", data={"agent_slug": "labs-helper"},
               content_type="application/json")
    session = Session.objects.get(pk=r.json()["id"])
    assert "embed_app" not in session.metadata


def test_a_browser_session_cannot_stamp_itself_either():
    """Otherwise the gate is only as strong as the auth path a caller picks."""
    user, _ws, _agent, _app = _ctx()
    c = Client()
    c.force_login(user)
    r = c.post(
        "/api/canopy-sessions/",
        data={"agent_slug": "labs-helper", "metadata": {"embed_app": "connect-labs"}},
        content_type="application/json",
    )
    session = Session.objects.get(pk=r.json()["id"])
    assert "embed_app" not in session.metadata


def test_other_metadata_still_passes_through():
    """Only the one reserved key is server-owned — a host's own context
    (`workflow_id`, `opportunity_id`, …) is the reason metadata exists."""
    user, _ws, _agent, app = _ctx()
    r = _create(
        _bearer(app, user),
        {"agent_slug": "labs-helper", "metadata": {"workflow_id": 42, "embed_app": "spoof"}},
    )
    session = Session.objects.get(pk=r.json()["id"])
    assert session.metadata["workflow_id"] == 42
    assert session.metadata["embed_app"] == "connect-labs"


def test_origin_key_is_left_to_the_host():
    """ace-web derives a FINER scope (one per ace workspace) server-side from a
    membership-checked path. Overriding it would break that for no gain —
    embed_app answers a different question."""
    user, _ws, _agent, app = _ctx()
    r = _create(
        _bearer(app, user),
        {"agent_slug": "labs-helper", "metadata": {"origin_key": "ace-web:connect"}},
    )
    session = Session.objects.get(pk=r.json()["id"])
    assert session.metadata["origin_key"] == "ace-web:connect"


def test_the_list_can_be_scoped_to_this_app():
    user, ws, agent, app = _ctx()
    other_app = AppCredential.create_credential(
        name="ace-web",
        created_by=User.objects.create_user("a2", "a2@dimagi.com", "pw"),
        workspace=ws,
    )
    mine = _create(_bearer(app, user)).json()["id"]
    theirs = _create(_bearer(other_app, user)).json()["id"]

    rows = Client().get("/api/canopy-sessions/?embed_app=connect-labs", **_bearer(app, user)).json()
    ids = {r["id"] for r in rows}
    assert mine in ids
    assert theirs not in ids


def test_an_unfiltered_list_still_returns_everything_the_user_may_see():
    """The filter is opt-in; adding it must not quietly narrow canopy's own UI."""
    user, _ws, _agent, app = _ctx()
    mine = _create(_bearer(app, user)).json()["id"]
    rows = Client().get("/api/canopy-sessions/", **_bearer(app, user)).json()
    assert mine in {r["id"] for r in rows}


# --- reading history back, scoped ------------------------------------------
#
# The stamping above exists so the widget can ask "my previous conversations
# HERE without being able to ask for someone else's conversations THERE". The
# second half was only enforced at CREATE: `embed_app` was a caller-supplied
# query filter on read, so a token issued to one host could name another and
# enumerate that user's conversations from it — same human, across the host
# boundary, titles included.


def _session_from(app_name, user, ws, agent, title="", page=None):
    session = Session.objects.create(
        workspace=ws, created_by=user, agent=agent, title=title,
        metadata={"embed_app": app_name},
    )
    if page:
        from apps.canopy_sessions import page_state

        page_state.set_page_state(session, page)
    return session


def _listed(headers, query=""):
    body = Client().get(f"/api/canopy-sessions/?state=all{query}", **headers).json()
    rows = body if isinstance(body, list) else (body.get("items") or body.get("sessions") or [])
    return {r["id"] for r in rows}


def test_a_host_sees_only_its_own_conversations_even_if_it_names_another():
    """THE fix. Naming someone else's app must not widen the list."""
    user, ws, agent, app = _ctx()          # app = "connect-labs"
    mine = _session_from("connect-labs", user, ws, agent, title="from connect-labs")
    theirs = _session_from("canopy-web", user, ws, agent, title="from canopy-web")

    ids = _listed(_bearer(app, user), "&embed_app=canopy-web")

    assert str(mine.id) in ids
    assert str(theirs.id) not in ids, "a host enumerated another host's conversations"


def test_a_plain_human_caller_is_not_narrowed():
    """A browser or PAT caller is the human themself, not an app acting for
    them — so canopy's own UI must not quietly start hiding sessions."""
    user, ws, agent, _app = _ctx()
    a = _session_from("connect-labs", user, ws, agent, title="a")
    b = _session_from("canopy-web", user, ws, agent, title="b")

    c = Client()
    c.force_login(user)
    body = c.get("/api/canopy-sessions/?state=all").json()
    rows = body if isinstance(body, list) else (body.get("items") or body.get("sessions") or [])

    assert {str(a.id), str(b.id)} <= {r["id"] for r in rows}


def test_history_filters_by_the_page_it_was_had_on():
    """"The conversations I had on THIS page." Matched on the session's own
    declared page state, not on anything canopy infers."""
    user, ws, agent, app = _ctx()
    on_insights = _session_from("connect-labs", user, ws, agent, "insights",
                                {"path": "/insights", "resource": "insight://"})
    _session_from("connect-labs", user, ws, agent, "stock",
                  {"path": "/stock", "resource": "stock://"})

    assert _listed(_bearer(app, user), "&resource=insight://") == {str(on_insights.id)}


def test_it_filters_by_exact_path_too():
    user, ws, agent, app = _ctx()
    a = _session_from("connect-labs", user, ws, agent, "a", {"path": "/insights"})
    _session_from("connect-labs", user, ws, agent, "b", {"path": "/supervisor"})

    assert _listed(_bearer(app, user), "&page_path=/insights") == {str(a.id)}


def test_a_session_that_declared_no_page_matches_no_page_filter():
    """Correct rather than unhelpful: it was not had on any page canopy knows
    of, and guessing from the title would be inventing provenance."""
    user, ws, agent, app = _ctx()
    _session_from("connect-labs", user, ws, agent, title="no page")

    assert _listed(_bearer(app, user), "&resource=insight://") == set()
