"""The page the Canopy Runner desktop app opens on: GET/PATCH /api/me/runner-app/
and the `/runner-app/` redirect the app loads."""
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(client):
    u = get_user_model().objects.create_user(username="r@x.com", email="r@x.com")
    client.force_login(u)
    return u


def _set(client, opens_on):
    return client.patch("/api/me/runner-app/", data={"opens_on": opens_on},
                        content_type="application/json")


CHOICES = [{"value": "supervisor", "label": "Supervisor"}, {"value": "ide", "label": "IDE"}]


def test_defaults_to_the_supervisor(client, user):
    assert client.get("/api/me/runner-app/").json() == {"opens_on": "supervisor", "choices": CHOICES}
    res = client.get("/runner-app/")
    assert res.status_code == 302 and res["Location"] == "/supervisor"


def test_choosing_the_ide_opens_the_ide(client, user):
    assert _set(client, "ide").json() == {"opens_on": "ide", "choices": CHOICES}
    assert client.get("/api/me/runner-app/").json()["opens_on"] == "ide"
    assert client.get("/runner-app/")["Location"] == "/ide"


def test_choosing_back_persists(client, user):
    _set(client, "ide")
    _set(client, "supervisor")
    assert client.get("/runner-app/")["Location"] == "/supervisor"


def test_an_unknown_page_is_refused(client, user):
    assert _set(client, "elsewhere").status_code == 422


def test_signed_out_goes_to_the_supervisor(client):
    res = client.get("/runner-app/")
    assert res.status_code == 302 and res["Location"].endswith("/supervisor")
    assert client.get("/api/me/runner-app/").status_code in (401, 403)


def test_the_menubar_can_set_it_with_the_windows_session():
    # What the app sends on a box with no PAT: the window's cookies, the CSRF token
    # from its csrftoken cookie as X-CSRFToken, and an Origin of canopy itself.
    c = Client(enforce_csrf_checks=True)
    c.force_login(get_user_model().objects.create_user(username="m@x.com", email="m@x.com"))
    c.get("/api/csrf/")
    token = c.cookies[settings.CSRF_COOKIE_NAME].value
    res = c.patch("/api/me/runner-app/", data={"opens_on": "ide"}, content_type="application/json",
                  HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="http://testserver")
    assert res.status_code == 200 and res.json()["opens_on"] == "ide"
