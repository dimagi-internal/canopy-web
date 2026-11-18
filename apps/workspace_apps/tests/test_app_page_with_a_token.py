"""An agent opens a workspace app page in a headless browser signed in with its own PAT.

Such a browser sends `Authorization: Bearer <PAT>` on every request, the page's own
and the sandbox frame's. The frame (`apps/dynamic_views/frame.py`) has an opaque
origin, so each of its module scripts became a preflighted cross-origin request that
WhiteNoise refused with a 405, and the page stayed blank. `config/static_middleware.py`
answers that preflight for the SPA's built files only, which are public anyway.

What the page shows still comes only from the app's REST routes, as the token's user:
these pin that a token reaches exactly what the same person's session reaches, and that
a token in the URL signs nobody in.
"""
from __future__ import annotations

import pytest

from apps.workspace_apps.models import AppRoleGrant
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.workspace_apps import BASE, _client, _user
from tests.token_helpers import bearer_client, pat_token

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _as_deployed(settings):
    # Production's CORS (base.py: only /api/, only for host sites) and its login gate;
    # the test settings allow every origin and switch the gate off.
    settings.CORS_ALLOW_ALL_ORIGINS = False
    settings.CORS_URLS_REGEX = r"^/api/.*$"
    settings.REQUIRE_AUTH = True

PAGE = "/w/dimagi/apps/hiring/"
PREFLIGHT = {"HTTP_ORIGIN": "null", "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "GET",
             "HTTP_ACCESS_CONTROL_REQUEST_HEADERS": "authorization"}


def _reads(client):
    """What a client gets from the routes an app page reads through."""
    return [(r.status_code, r.content) for r in (
        client.get(f"{BASE}/hiring"),
        client.get(f"{BASE}/hiring/query", {"collection": "job"}),
        client.get(f"{BASE}/hiring/query", {"collection": "offer"}),
    )]


def test_the_frames_scripts_may_be_fetched_with_a_bearer_header(client):
    r = client.options("/assets/dynamicView-abc123.js", **PREFLIGHT)
    assert r.status_code == 204
    assert r["Access-Control-Allow-Origin"] == "*"
    assert r["Access-Control-Allow-Methods"] == "GET, HEAD"
    assert r["Access-Control-Allow-Headers"] == "authorization"
    # Never with credentials: a cookie cannot ride along with the opaque frame's fetch.
    assert "Access-Control-Allow-Credentials" not in r


@pytest.mark.parametrize(("path", "method"), [
    ("/w/dimagi/apps/hiring/", "GET"),          # the page itself: no cross-origin read
    ("/dynamic-views/frame/", "GET"),           # the frame document
    ("/assets/dynamicView-abc123.js", "POST"),  # only reads of a built file
    ("/assets/dynamicView-abc123.js", "DELETE"),
])
def test_no_other_preflight_is_answered(client, path, method):
    r = client.options(path, **{**PREFLIGHT, "HTTP_ACCESS_CONTROL_REQUEST_METHOD": method})
    assert r.status_code != 204
    assert "Access-Control-Allow-Headers" not in r


def test_a_member_opens_the_page_with_their_token_and_reads_what_their_session_reads(world, hiring):
    token = bearer_client(pat_token(world["editor"]))
    page = token.get(PAGE)
    assert page.status_code != 302, "a token-signed request must not be sent to sign in"
    assert _reads(token) == _reads(_client(world["editor"]))
    assert _reads(token)[0][0] == 200


def test_a_token_with_no_role_gets_the_same_refusal_its_session_gets(world, hiring):
    # The viewer is a member with no Hiring role: offers are recruiter-only.
    as_token, as_session = _reads(bearer_client(pat_token(world["viewer"]))), _reads(_client(world["viewer"]))
    assert as_token == as_session
    assert as_token[2][0] == 403


def test_an_outsiders_token_gets_the_stranger_404(world, hiring):
    as_token = _reads(bearer_client(pat_token(world["outsider"])))
    assert as_token == _reads(_client(world["outsider"]))
    assert {status for status, _ in as_token} == {404}


def test_a_guests_token_reaches_their_app_role_and_nothing_more(world, hiring):
    guest = _user("guest")
    WorkspaceMembership.objects.create(user=guest, workspace=world["ws"], role=WorkspaceMembership.GUEST,
                                       scope={"apps": {"hiring": "interviewer"}})
    assert not AppRoleGrant.objects.filter(user=guest).exists()
    as_token = _reads(bearer_client(pat_token(guest)))
    assert as_token == _reads(_client(guest))
    # Never the app's drafts or setup, by token as by session.
    for path in (f"{BASE}/hiring/draft", f"{BASE}/hiring/setup"):
        assert bearer_client(pat_token(guest)).get(path).status_code == _client(guest).get(path).status_code


@pytest.mark.parametrize("param", ["token", "access_token", "pat", "auth", "bearer"])
def test_a_token_in_the_url_signs_nobody_in(world, hiring, client, param):
    raw = pat_token(world["editor"])
    page = client.get(PAGE, {param: raw})
    assert page.status_code == 302 and "/accounts/" in page["Location"]
    assert client.get(f"{BASE}/hiring", {param: raw}).status_code == 401
