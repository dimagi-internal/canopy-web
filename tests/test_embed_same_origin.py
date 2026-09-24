"""canopy embedding its own widget: the frame carries a session cookie too.

The first thing a real browser found. Every other host is cross-origin, so its
frame sends canopy nothing but the `Authorization: Bearer` header — but canopy
embedding ITSELF is same-origin, and the browser attaches canopy's own session
cookie to the frame's XHRs.

`BearerTokenAuthMiddleware` returned early whenever `request.user` was already
authenticated, so on exactly that path the header was never read,
`request.delegated_app` was never stamped, and `/api/embed/agents` answered
`403` — reported from a live page as
`canopy request failed (403): /api/embed/agents`.

Nothing in-process caught it because every existing test presents the token
WITHOUT a session, which is the cross-origin shape.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _setup():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    _raw, app = AppCredential.create_credential(name="canopy-web", created_by=user, workspace=ws)
    AppCredentialAgent.objects.create(app=app, agent=agent)
    token, _row = DelegatedToken.issue(app=app, user=user, ttl_seconds=3600)
    return user, app, token


def test_the_frame_can_list_agents_while_a_session_cookie_rides_along():
    """THE regression. Same-origin means both credentials arrive together."""
    user, _app, token = _setup()
    c = Client()
    c.force_login(user)  # the session cookie the browser attaches anyway

    r = c.get("/api/embed/agents", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert r.status_code == 200, r.content
    assert [a["slug"] for a in r.json()] == ["echo"]


def test_the_cross_origin_shape_still_works():
    """No session, header only — every other host, and what the old tests covered."""
    _user, _app, token = _setup()

    r = Client().get("/api/embed/agents", HTTP_AUTHORIZATION=f"Bearer {token}")

    assert r.status_code == 200
    assert [a["slug"] for a in r.json()] == ["echo"]


def test_a_session_alone_is_still_refused():
    """The app must come from the token. A browser with no token behind it has
    no acting app, and answering "every agent you can see" would quietly turn
    this into an unscoped agent index."""
    user, _app, _token = _setup()
    c = Client()
    c.force_login(user)

    assert c.get("/api/embed/agents").status_code == 403


def test_the_frame_can_write_without_a_csrf_cookie():
    """It authenticates by header and holds no CSRF token for canopy, so an
    unsafe method would 403 on CSRF rather than on auth — the same symptom from
    a different cause, one step later in the same flow."""
    user, _app, token = _setup()
    c = Client(enforce_csrf_checks=True)
    c.force_login(user)

    r = c.post(
        "/api/canopy-sessions/",
        data={"agent_slug": "echo", "title": "", "metadata": {}},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    assert r.status_code != 403, r.content


def test_the_session_user_wins_when_the_two_disagree():
    """They agree in practice — `/api/embed/token` mints for the session user.
    Where they somehow did not, the person at the browser is the safer answer,
    and it is the identity every other view on this request already saw.
    """
    _user, app, _token = _setup()
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="w2", display_name="W2", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=ws2, role=WorkspaceMembership.OWNER)
    theirs, _row = DelegatedToken.issue(app=app, user=other, ttl_seconds=3600)

    c = Client()
    c.force_login(_user)
    r = c.get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {theirs}")

    if r.status_code == 200:
        assert r.json()["email"] == "jj@dimagi.com"


def test_a_bad_bearer_alongside_a_session_changes_nothing():
    """A garbage header must not strip the session or grant an app."""
    user, _app, _token = _setup()
    c = Client()
    c.force_login(user)

    r = c.get("/api/embed/agents", HTTP_AUTHORIZATION="Bearer nonsense")

    # Still signed in (so not a 401), still no acting app (so a 403).
    assert r.status_code == 403
