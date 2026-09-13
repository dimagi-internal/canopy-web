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
    _raw, app = AppCredential.create_credential(
        name=app_name, domains=["dimagi.com"], created_by=admin,
    )
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
    _raw2, other_app = AppCredential.create_credential(
        name="ace-web", domains=["dimagi.com"],
        created_by=User.objects.create_user("a2", "a2@dimagi.com", "pw"),
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
