"""The page the Canopy Runner desktop app opens on: GET/PATCH /api/me/runner-app/
and the `/runner-app/` redirect the app loads."""
import pytest
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(client):
    u = get_user_model().objects.create_user(username="r@x.com", email="r@x.com")
    client.force_login(u)
    return u


def _set(client, opens_on):
    return client.patch("/api/me/runner-app/", data={"opens_on": opens_on},
                        content_type="application/json")


def test_defaults_to_the_supervisor(client, user):
    assert client.get("/api/me/runner-app/").json() == {"opens_on": "supervisor"}
    res = client.get("/runner-app/")
    assert res.status_code == 302 and res["Location"] == "/supervisor"


def test_choosing_the_ide_opens_the_ide(client, user):
    assert _set(client, "ide").json() == {"opens_on": "ide"}
    assert client.get("/api/me/runner-app/").json() == {"opens_on": "ide"}
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
