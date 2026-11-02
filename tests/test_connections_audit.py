"""The connections audit trail and log: one vocabulary, and never a secret.

Every flow that writes a record runs here once — connects through the browser
views (refused at authorize, refused for a missing refresh token, a person's and
an agent's sealed to their owner), sharing and unsharing, disconnects. Then
every log record (any logger) and every Event is checked:

- each kind is in the documented vocabulary (`docs/architecture/vault.md`
  § Connection audit), and a connections log line parses back
  (`services.parse_log_line`);
- levels: INFO for normal, WARNING for reconnect_required / provider_error /
  revoke_failed, never ERROR from canopy's own code;
- no secret used anywhere in the run appears: the access and refresh tokens,
  the authorization code, every OAuth `state` and PKCE verifier, and the
  client secret — not in a log, an Event, or a stored connection row.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.agents.models import Agent, AgentAdmin
from apps.connections import services
from apps.connections.models import Connection
from apps.events.models import Event
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import CLIENT_SECRET, FakeSF, _resp, _token_body, sf_app, vault_key

pytestmark = pytest.mark.django_db

VOCABULARY = {
    "connected": logging.INFO, "disconnected": logging.INFO, "granted": logging.INFO,
    "grant_revoked": logging.INFO, "token_issued": logging.INFO,
    "reconnect_required": logging.WARNING, "provider_error": logging.WARNING,
    "revoke_failed": logging.WARNING, "app_configured": logging.INFO, "app_removed": logging.INFO,
}
BASE_KEYS = ("connection", "provider", "owner", "account_label", "by")


def _connect_in_browser(client, secrets, **agent):
    start = reverse("connections_start", args=["salesforce", "w"])
    resp = client.post(start, agent) if agent else client.get(start)
    state = parse_qs(urlparse(resp["Location"]).query)["state"][0]
    secrets += [state, client.session[services.SESSION_KEY][state]["verifier"]]
    with FakeSF():
        landed = client.get(reverse("connections_callback", args=["salesforce", "w"]),
                            {"code": "c0de-secret", "state": state})
    assert "/share?" in landed["Location"]                       # sealed to its owner


@pytest.fixture
def world():
    user = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    vault_key(user)  # a connection is sealed to it
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.EDITOR)
    sf_app(ws)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    for a in (eva, echo):
        AgentAdmin.objects.get_or_create(agent=a, user=user)
    return user, eva, echo


def test_every_record_is_in_the_vocabulary_and_carries_no_secret(world, caplog):
    user, eva, echo = world
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="apps")      # the settings quiet `apps` below WARNING
    secrets = ["00D!access_1", "5Aep_refresh_1", "c0de-secret", CLIENT_SECRET]
    browser = Client()
    browser.force_login(user)

    # Connects that fail: the provider refused at authorize; then no refresh token.
    start = reverse("connections_start", args=["salesforce", "w"])
    state = parse_qs(urlparse(browser.get(start)["Location"]).query)["state"][0]
    secrets.append(state)
    browser.get(reverse("connections_callback", args=["salesforce", "w"]), {"error": "server_error", "state": state})
    state = parse_qs(urlparse(browser.get(start)["Location"]).query)["state"][0]
    secrets += [state, browser.session[services.SESSION_KEY][state]["verifier"]]
    with FakeSF(token=[_resp(body=_token_body(refresh_token=None))]):
        browser.get(reverse("connections_callback", args=["salesforce", "w"]), {"code": "c0de-secret", "state": state})

    # Connects that pass every check: sealed to the owner, nothing readable kept.
    _connect_in_browser(browser, secrets)
    _connect_in_browser(browser, secrets, agent="eva")
    rows = json.dumps(list(Connection.objects.values()), default=str)
    assert not [s for s in secrets if s in rows], "a secret in a stored connection row"

    # A person's connection, disconnected; an agent's, shared, unshared, disconnected.
    mine = Connection.objects.get(user=user)
    assert browser.delete(f"/api/connections/{mine.pk}").status_code == 204
    conn = Connection.objects.get(agent=eva)
    services.grant(conn, echo, by=user)
    services.revoke_grant(conn.grants.get(), by=user)
    services.disconnect(conn, by=user)

    # ---- the vocabulary -------------------------------------------------------------
    events = list(Event.objects.filter(source=services.EVENT_SOURCE).order_by("id"))
    assert [e.kind for e in events] == [
        "connections.connected", "connections.granted", "connections.grant_revoked", "connections.disconnected"]
    for e in events:
        assert e.level == "info" and e.payload["owner"] == "agent:eva"
        assert all(k in e.payload for k in BASE_KEYS)

    lines = [r for r in caplog.records if r.getMessage().startswith("connections.")]
    kinds = [r.getMessage().split(" ", 1)[0].removeprefix("connections.") for r in lines]
    assert kinds == ["provider_error", "provider_error", "connected", "disconnected"]
    parsed = [services.parse_log_line(r.getMessage()) for r in lines]
    assert [p[1].get("during") for p in parsed[:2]] == ["authorize", "connect"]
    assert [p[1].get("error") for p in parsed[:2]] == ["server_error", "no_refresh_token"]
    for r, (kind, pairs) in zip(lines, parsed, strict=True):
        assert all(k in pairs for k in BASE_KEYS), r.getMessage()
        assert r.levelno == VOCABULARY[kind], r.getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("apps.")]

    # ---- never a secret --------------------------------------------------------------
    tracebacks = [logging.Formatter().formatException(r.exc_info) for r in caplog.records if r.exc_info]
    blob = "\n".join([r.getMessage() for r in caplog.records] + tracebacks
                     + [str(getattr(r, "exc_text", "") or "") for r in caplog.records]
                     + [json.dumps(e.payload) + e.summary for e in Event.objects.all()])
    leaked = [s for s in secrets if s in blob]
    assert not leaked, f"secrets in logs or events: {leaked}"
