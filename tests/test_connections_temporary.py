"""Lending an account for an hour: an access token, nothing that can renew it, no passkey.

Pinned: Google is asked for no refresh token and the start needs no vault key; the token is
kept encrypted, is handed only to a turn its lender started, on the runner holding that turn;
one that ran out is handed to nobody and swept; ending it revokes it at Google and takes no passkey.
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.connections import keeper, services, temporary
from apps.connections.models import Connection, ConnectionApp, TemporaryAccess
from apps.connections.providers import google as _google
from apps.harness import initiator as who
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import FakeGoogle

pytestmark = pytest.mark.django_db

START = "/auth/connections/google/w/start/"
CALLBACK = "/auth/connections/google/w/callback/"


class ShortGoogle(FakeGoogle):
    """Google asked for `access_type=online`: no refresh token, unless told to misbehave."""

    refresh_anyway = False

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
        resp = super().post(url, data=data, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
        if url == _google.TOKEN_URL and not self.refresh_anyway:
            body = dict(resp.json.return_value)
            body.pop("refresh_token", None)
            resp.json.return_value = body
        return resp


@pytest.fixture
def world(db):
    users = get_user_model().objects
    lender = users.create_user("lender", email="someone@gmail.com")
    other = users.create_user("other", email="other@gmail.com")
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=lender)
    for u in (lender, other):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=lender)
    app = ConnectionApp.objects.create(workspace=ws, provider="google", client_id="cid", client_secret="sec",
                                       options={"allowed_domains": "gmail.com"})
    client = Client()
    client.force_login(lender)
    return mock.Mock(ws=ws, lender=lender, other=other, agent=agent, app=app, client=client)


@pytest.fixture
def fake():
    with ShortGoogle() as g:
        yield g


def _start(client, **params):
    return client.get(START, {"temporary": "1", "for_agent": "bailey", "return_to": "/w/w/views/2", **params})


def _connect(client):
    state = parse_qs(urlparse(_start(client)["Location"]).query)["state"][0]
    return client.get(CALLBACK, {"code": "c", "state": state})


def _row(world, *, minutes=30, user=None):
    return TemporaryAccess.objects.create(
        app=world.app, user=user or world.lender, agent=world.agent, provider="google", account_id="1098",
        account_label="someone@gmail.com", token=encrypt_secret("ya29.t"),
        expires_at=timezone.now() + timedelta(minutes=minutes))


def _turn(user):
    return mock.Mock(pk="t1", initiator_user_id=user.pk, initiator_kind=who.USER,
                     initiator_assurance=next(iter(services._OWNER_TURN_ASSURANCE)), initiator_via="")


def test_start_asks_google_for_no_refresh_token_and_needs_no_vault_key(world, fake):
    query = parse_qs(urlparse(_start(world.client)["Location"]).query)
    assert query["access_type"] == ["online"]
    assert query["prompt"] == ["consent"]


def test_callback_keeps_only_an_encrypted_token_and_lands_back_on_the_page(world, fake):
    resp = _connect(world.client)
    assert resp.status_code == 302
    assert resp["Location"].startswith("/w/w/views/2?") and "status=ok" in resp["Location"]
    row = TemporaryAccess.objects.get()
    assert "ya29" not in row.token and decrypt_secret(row.token).startswith("ya29.access-")
    assert row.expires_at <= timezone.now() + temporary.MAX_LIFETIME
    assert row.account_label == "someone@gmail.com"
    assert not Connection.objects.exists()


def test_a_refresh_token_is_refused_and_nothing_is_kept(world, fake):
    fake.refresh_anyway = True
    resp = _connect(world.client)
    assert "status=error" in resp["Location"] and "reason=provider_error" in resp["Location"]
    assert not TemporaryAccess.objects.exists()


def test_an_account_the_app_refuses_is_not_kept(world, fake):
    fake.user = {**fake.user, "email": "someone@elsewhere.example"}
    resp = _connect(world.client)
    assert "reason=wrong_account" in resp["Location"]
    assert not TemporaryAccess.objects.exists()


def test_a_new_sign_in_replaces_the_last_one(world, fake):
    _connect(world.client)
    first = TemporaryAccess.objects.get().token
    _connect(world.client)
    assert TemporaryAccess.objects.get().token != first


def test_an_agent_outside_the_workspace_is_refused_at_start(world, fake):
    resp = _start(world.client, for_agent="nobody")
    assert "reason=bad_agent" in resp["Location"]


def test_a_provider_that_cannot_be_asked_is_refused(world):
    ConnectionApp.objects.create(workspace=world.ws, provider="salesforce", client_id="c", client_secret="s")
    resp = world.client.get("/auth/connections/salesforce/w/start/",
                            {"temporary": "1", "for_agent": "bailey", "return_to": "/settings"})
    assert "reason=temporary_unsupported" in resp["Location"]


def test_only_a_turn_its_lender_started_gets_it(world):
    _row(world)
    assert temporary.for_turn(world.agent, "google", _turn(world.lender)) is not None
    assert temporary.providers_for_turn(world.agent, _turn(world.lender)) == ["google"]
    assert temporary.for_turn(world.agent, "google", _turn(world.other)) is None
    assert temporary.providers_for_turn(world.agent, _turn(world.other)) == []


def test_a_scheduled_turn_of_the_lender_does_not_get_it(world):
    _row(world)
    turn = _turn(world.lender)
    turn.initiator_kind, turn.initiator_via = who.SYSTEM, "schedule"
    assert temporary.for_turn(world.agent, "google", turn) is None


def test_someone_who_left_the_workspace_lends_nothing(world):
    _row(world)
    WorkspaceMembership.objects.filter(user=world.lender).delete()
    assert temporary.for_turn(world.agent, "google", _turn(world.lender)) is None


def test_one_that_ran_out_is_handed_to_nobody_listed_nowhere_and_swept(world):
    _row(world, minutes=-1)
    assert temporary.for_turn(world.agent, "google", _turn(world.lender)) is None
    assert temporary.mine(world.lender) == []
    keeper.sweep()
    assert not TemporaryAccess.objects.exists()


def test_my_connections_lists_mine_only_and_never_the_token(world):
    _row(world)
    _row(world, user=world.other)
    body = world.client.get("/api/connections/").json()
    assert [(t["agent"], t["account_label"]) for t in body["temporary"]] == [("bailey", "someone@gmail.com")]
    assert "token" not in body["temporary"][0] and "access_token" not in body["temporary"][0]


def test_ending_it_revokes_at_google_and_takes_no_passkey(world, fake):
    row = _row(world)
    resp = world.client.delete(f"/api/connections/temporary/{row.pk}")
    assert resp.status_code == 200 and resp.json() == {"outcome": "revoked"}
    assert not TemporaryAccess.objects.exists()
    assert [c for c in fake.calls if c[1] == _google.REVOKE_URL][0][2]["token"] == "ya29.t"


def test_it_ends_even_when_google_cannot_be_reached(world, fake):
    fake.revoke_status = 503
    row = _row(world)
    resp = world.client.delete(f"/api/connections/temporary/{row.pk}")
    assert resp.status_code == 200 and resp.json() == {"outcome": "ended"}
    assert not TemporaryAccess.objects.exists()


def test_someone_elses_is_a_404(world):
    row = _row(world, user=world.other)
    assert world.client.delete(f"/api/connections/temporary/{row.pk}").status_code == 404
    assert TemporaryAccess.objects.exists()
