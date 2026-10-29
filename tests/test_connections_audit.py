"""The connections audit trail and log: one vocabulary, and never a secret.

Every flow that writes a record runs here once — a person's and an agent's
connect (through the browser views), token handouts to a CLI and to a turn
(cached and refreshed, with rotation), a rejected grant, a misconfigured
client, a failed revoke, sharing and unsharing, disconnects. Then every log
record (any logger) and every Event is checked:

- each kind is in the documented vocabulary (`docs/architecture/vault.md`
  § Connection audit), and a connections log line parses back
  (`services.parse_log_line`);
- levels: INFO for normal, WARNING for reconnect_required / provider_error /
  revoke_failed, never ERROR from canopy's own code;
- no secret used anywhere in the run appears: access and refresh tokens (old
  and rotated), the authorization code, every OAuth `state` and PKCE verifier,
  the client secret, the PAT and the runner's bearer.
"""
from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.connections import services
from apps.connections.models import Connection
from apps.events.models import Event
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey, Turn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import SF, FakeSF, _resp, _token_body

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("salesforce_configured")]

VOCABULARY = {
    "connected": logging.INFO, "disconnected": logging.INFO, "granted": logging.INFO,
    "grant_revoked": logging.INFO, "token_issued": logging.INFO,
    "reconnect_required": logging.WARNING, "provider_error": logging.WARNING,
    "revoke_failed": logging.WARNING,
}
BASE_KEYS = ("connection", "provider", "owner", "account_label", "by")


def _connect_in_browser(client, secrets, **agent):
    start = reverse("connections_start", args=["salesforce"])
    resp = client.post(start, agent) if agent else client.get(start)
    state = parse_qs(urlparse(resp["Location"]).query)["state"][0]
    secrets += [state, client.session[services.SESSION_KEY][state]["verifier"]]
    with FakeSF():
        landed = client.get(reverse("connections_callback", args=["salesforce"]),
                            {"code": "c0de-secret", "state": state})
    assert "status=connected" in landed["Location"]


@pytest.fixture
def world():
    user = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.EDITOR)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    for a in (eva, echo):
        AgentAdmin.objects.get_or_create(agent=a, user=user)
    return user, eva, echo


def test_every_record_is_in_the_vocabulary_and_carries_no_secret(world, caplog):
    user, eva, echo = world
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="apps")      # the settings quiet `apps` below WARNING
    secrets = ["00D!access_1", "5Aep_refresh_1", "00D!rotated", "5Aep_rotated", "c0de-secret",
               SF["SALESFORCE_CLIENT_SECRET"]]
    browser = Client()
    browser.force_login(user)

    # Connects that fail: the provider refused at authorize; then no refresh token.
    start = reverse("connections_start", args=["salesforce"])
    state = parse_qs(urlparse(browser.get(start)["Location"]).query)["state"][0]
    secrets.append(state)
    browser.get(reverse("connections_callback", args=["salesforce"]), {"error": "server_error", "state": state})
    state = parse_qs(urlparse(browser.get(start)["Location"]).query)["state"][0]
    secrets += [state, browser.session[services.SESSION_KEY][state]["verifier"]]
    with FakeSF(token=[_resp(body=_token_body(refresh_token=None))]):
        browser.get(reverse("connections_callback", args=["salesforce"]), {"code": "c0de-secret", "state": state})

    # A person: connect, a cached and a refreshed (rotating) CLI token, disconnect.
    _connect_in_browser(browser, secrets)
    pat, _ = PersonalToken.create_for_user(user=user, label="cli")
    secrets.append(pat)
    cli = Client(HTTP_AUTHORIZATION=f"Bearer {pat}")
    assert cli.post("/api/me/connections/salesforce/token").status_code == 200
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!rotated", refresh_token="5Aep_rotated"))]):
        assert cli.post("/api/me/connections/salesforce/token?refresh=true").status_code == 200

    # A misconfigured client (not the grant's fault), then a revoked grant.
    with FakeSF(token=[_resp(400, {"error": "invalid_client", "error_description": "bad secret"})]):
        assert cli.post("/api/me/connections/salesforce/token?refresh=true").status_code == 502
    with FakeSF(token=[_resp(400, {"error": "invalid_grant", "error_description": "expired"})]):
        assert cli.post("/api/me/connections/salesforce/token?refresh=true").status_code == 409
    mine = Connection.objects.get(user=user)
    with FakeSF(revoke=_resp(400, {"error": "unsupported_token_type"})):
        assert browser.delete(f"/api/connections/{mine.pk}").status_code == 204

    # An agent: connect, share, a turn's token (first stage and a heartbeat re-stage), unshare, disconnect.
    _connect_in_browser(browser, secrets, agent="eva")
    conn = Connection.objects.get(agent=eva)
    services.grant(conn, echo, by=user)
    runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=user, workspace=eva.workspace,
                                   last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=echo, runner=runner, rank=0)
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=uuid.uuid4().hex, status=RunnerDeviceKey.ACTIVE)
    bearer, _ = RunnerAccessToken.mint(key)
    secrets.append(bearer)
    turn = Turn.objects.create(agent=echo, origin=Turn.ORIGIN_EMAIL, prompt="x", idempotency_key="k",
                               status=Turn.CLAIMED, claimed_by=runner)
    box = Client(HTTP_AUTHORIZATION=f"Bearer {bearer}")
    for _ in range(2):
        got = box.post(f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/connection-token",
                       {"provider": "salesforce"}, content_type="application/json")
        assert got.status_code == 200, got.content
    g = conn.grants.get()
    services.revoke_grant(g, by=user)
    with FakeSF():
        services.disconnect(conn, by=user)

    # ---- the vocabulary -------------------------------------------------------------
    events = list(Event.objects.filter(source=services.EVENT_SOURCE).order_by("id"))
    assert [e.kind for e in events] == [
        "connections.connected", "connections.granted", "connections.token_issued",
        "connections.grant_revoked", "connections.disconnected"]
    issued = events[2]
    assert issued.count == 2                                       # one turn's re-stage coalesces
    assert issued.payload["to"] == "turn" and issued.payload["agent"] == "echo"
    assert issued.payload["turn"] == str(turn.pk) and issued.payload["runner"] == str(runner.pk)
    for e in events:
        assert e.level == "info" and e.payload["owner"] == "agent:eva"
        assert all(k in e.payload for k in BASE_KEYS)

    lines = [r for r in caplog.records if r.getMessage().startswith("connections.")]
    kinds = [r.getMessage().split(" ", 1)[0].removeprefix("connections.") for r in lines]
    assert kinds == ["provider_error", "provider_error", "connected", "token_issued", "token_issued",
                     "provider_error", "reconnect_required", "revoke_failed", "disconnected"]
    parsed = [services.parse_log_line(r.getMessage()) for r in lines]
    assert [p[1].get("during") for p in parsed[:2]] == ["authorize", "connect"]
    assert [p[1].get("error") for p in parsed[:2]] == ["server_error", "no_refresh_token"]
    for r, (kind, pairs) in zip(lines, parsed, strict=True):
        assert all(k in pairs for k in BASE_KEYS), r.getMessage()
        assert r.levelno == VOCABULARY[kind], r.getMessage()
    # (Django's own `django.request` logs the 502 at ERROR, as it does every 5xx; ours never do.)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("apps.")]

    # ---- never a secret --------------------------------------------------------------
    tracebacks = [logging.Formatter().formatException(r.exc_info) for r in caplog.records if r.exc_info]
    blob = "\n".join([r.getMessage() for r in caplog.records] + tracebacks
                     + [str(getattr(r, "exc_text", "") or "") for r in caplog.records]
                     + [json.dumps(e.payload) + e.summary for e in Event.objects.all()])
    leaked = [s for s in secrets if s in blob]
    assert not leaked, f"secrets in logs or events: {leaked}"
