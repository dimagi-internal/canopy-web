"""canopy-web offering the widget on its own pages.

The odd host: the reason to embed an agent is to talk about what is on the
page, and the pages where that is most useful are canopy's own — an agent
inbox that has gone stale, a feature set worth deprecating that you notice
while looking at it. Canopy already has first-class chat, but not *there*.

Two properties matter here. It is **off unless configured**, because this
mounts a chat panel on every authenticated page and no deployment should grow
one by surprise. And a misconfiguration leaves it **absent rather than
broken** — a setting naming a credential that does not exist must not 500 the
page that asks about it.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.tokens.models import AppCredential, DelegatedToken
from tests.site_tenant import host_workspace

pytestmark = pytest.mark.django_db


def _user():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    c = Client()
    c.force_login(user)
    return user, c


def _app(name="canopy-web", *, shown=True):
    """A connected site, optionally the one canopy shows on its own pages.

    `shown` is a column now, not a name matched against `EMBED_SELF_APP`. The
    setting is gone: it made canopy a special case, and a name that did not
    match produced no widget and no error on either side.
    """
    admin = User.objects.create_user(f"a-{name}", f"a-{name}@dimagi.com", "pw")
    app = AppCredential.create_credential(name=name, created_by=admin,
                                         workspace=host_workspace())
    if shown:
        app.show_on_canopy_pages = True
        app.save(update_fields=["show_on_canopy_pages"])
    return app


def test_off_by_default():
    _user_, c = _user()
    assert c.get("/api/embed/self").json() == {"enabled": False, "app": "", "agent": ""}


def test_the_token_endpoint_404s_when_off():
    _user_, c = _user()
    assert c.post("/api/embed/token").status_code == 404


def test_enabled_when_the_named_app_exists():
    _app()
    _user_, c = _user()
    assert c.get("/api/embed/self").json() == {
        "enabled": True,
        "app": "canopy-web",
        "agent": "",
    }


def test_a_setting_naming_a_missing_app_is_absent_not_broken():
    """A typo in a deployment setting should leave the widget off, not 500 the
    status call every page makes."""
    _user_, c = _user()
    assert c.get("/api/embed/self").json()["enabled"] is False
    assert c.post("/api/embed/token").status_code == 404


def test_a_revoked_app_turns_it_off():
    app = _app()
    AppCredential.objects.filter(pk=app.pk).update(revoked_at=timezone.now())
    _user_, c = _user()
    assert c.get("/api/embed/self").json()["enabled"] is False
    assert c.post("/api/embed/token").status_code == 404


def test_the_token_is_minted_for_the_caller_and_actually_works():
    app = _app()
    user, c = _user()

    body = c.post("/api/embed/token").json()

    assert body["token"]
    resolved = DelegatedToken.lookup(body["token"])
    assert resolved is not None, "the minted token does not authenticate"
    assert resolved.user == user, "minted for the wrong user"
    assert resolved.app == app


def test_the_minted_token_authenticates_the_embed_surface():
    """End to end: what the widget will actually do with it."""
    _app()
    _user_, c = _user()
    raw = c.post("/api/embed/token").json()["token"]

    response = Client().get("/api/embed/agents", HTTP_AUTHORIZATION=f"Bearer {raw}")

    assert response.status_code == 200
    # Empty because nothing is allowlisted for this app — the point is that the
    # token authenticated rather than 403'd.
    assert response.json() == []


def test_anonymous_callers_get_nothing():
    """Session-authed: the endpoint mints for `request.user`, so there must be
    one."""
    _app()
    anon = Client()
    assert anon.post("/api/embed/token").status_code in (302, 401, 403)


def test_it_does_not_mint_for_an_email_the_caller_supplies():
    """Unlike a third-party host there is no `acting_as_email` here at all —
    the caller IS the user. A body must not be able to redirect it."""
    _app()
    user, c = _user()
    User.objects.create_user("victim", "victim@dimagi.com", "pw")

    raw = c.post(
        "/api/embed/token",
        data={"acting_as_email": "victim@dimagi.com"},
        content_type="application/json",
    ).json()["token"]

    assert DelegatedToken.lookup(raw).user == user


# --- the workspace's agents, on canopy's own panel ----------------------------------
#
# canopy's panel is registered as a site in ONE workspace, and a site offers only its
# own workspace's agents. On a page of another workspace that left the panel with
# nothing to offer (Bailey on marshellis's Gmail clean-up page). There, nothing
# narrower is set, so the workspace's agents are on offer: ones the person can already
# chat with on canopy. A third-party site never widens this way.


def _workspace_with_agent(user, slug, agent_slug):
    from apps.agents.models import Agent
    from apps.workspaces.models import Workspace, WorkspaceMembership

    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.EDITOR)
    return Agent.objects.create(slug=agent_slug, name=agent_slug.title(), workspace=ws)


def _agents(raw):
    r = Client().get("/api/embed/agents", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200, r.content
    return [a["slug"] for a in r.json()]


def test_on_another_workspaces_page_the_panel_lists_that_workspaces_agents():
    from apps.agents.models import Agent
    from apps.tokens.models import AppCredentialAgent

    app = _app()
    host_agent = Agent.objects.create(slug="ada", name="Ada", workspace=host_workspace())
    AppCredentialAgent.objects.create(app=app, agent=host_agent)
    user, c = _user()
    from apps.workspaces.models import WorkspaceMembership
    WorkspaceMembership.objects.create(user=user, workspace=host_workspace(), role=WorkspaceMembership.OWNER)
    _workspace_with_agent(user, "family", "bailey")

    family = c.post("/api/embed/token?workspace=family").json()["token"]
    assert _agents(family) == ["bailey"]
    # The site's own workspace keeps the site's list; no workspace (or one you are not
    # in) is as before.
    assert _agents(c.post("/api/embed/token?workspace=site-host").json()["token"]) == ["ada"]
    assert _agents(c.post("/api/embed/token").json()["token"]) == ["ada"]
    assert _agents(c.post("/api/embed/token?workspace=nowhere").json()["token"]) == ["ada"]


def test_the_panel_may_reach_a_workspace_agent_but_a_sites_token_may_not():
    from apps.embedding import delegation

    app = _app()
    user, c = _user()
    bailey = _workspace_with_agent(user, "family", "bailey")
    c.post("/api/embed/token?workspace=family")
    own = DelegatedToken.objects.filter(user=user).latest("created_at")
    assert own.own_panel and own.page_workspace == "family"
    assert bailey.pk in delegation.offered_agent_ids(app, user=user, own_panel=True)

    # The same app's token minted any other way (a site's exchange) does not widen.
    _raw, site_tok = DelegatedToken.issue(app=app, user=user, ttl_seconds=600)
    assert not site_tok.own_panel
    assert bailey.pk not in delegation.offered_agent_ids(app, user=user, own_panel=site_tok.own_panel)
