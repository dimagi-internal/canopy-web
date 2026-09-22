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
    app = AppCredential.create_credential(        name=name, created_by=admin,
    )[1]
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
