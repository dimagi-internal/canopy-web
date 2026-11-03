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
from apps.connections import services, sharing
from apps.connections.models import Connection, ConnectionApp
from apps.events.models import Event
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import (
    CLIENT_SECRET,
    FakeSF,
    _resp,
    _token_body,
    browser_share,
    sf_app,
    vault_key,
)

pytestmark = pytest.mark.django_db

VOCABULARY = {
    "connected": logging.INFO, "disconnected": logging.INFO, "shared": logging.INFO, "granted": logging.INFO,
    "grant_revoked": logging.INFO, "token_issued": logging.INFO,
    "reconnect_required": logging.WARNING, "provider_error": logging.WARNING,
    "revoke_failed": logging.WARNING, "app_configured": logging.INFO, "app_removed": logging.INFO,
    "resealed": logging.INFO, "revoked": logging.INFO, "revoke_unconfirmed": logging.INFO,
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
    priv = vault_key(user)  # a connection is sealed to it
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.EDITOR)
    sf_app(ws)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    for a in (eva, echo):
        AgentAdmin.objects.get_or_create(agent=a, user=user)
    return user, eva, echo, priv


def test_every_record_is_in_the_vocabulary_and_carries_no_secret(world, caplog):
    user, eva, echo, priv = world
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
    assert browser.delete(f"/api/connections/{mine.pk}").status_code == 200
    conn = Connection.objects.get(agent=eva)
    sharing.share(conn, user=user, **browser_share(conn, user, priv, agents=[eva, echo], to=[]))
    sharing.share(conn, user=user, **browser_share(conn, user, priv, agents=[eva], to=[]))
    services.disconnect(conn, by=user)

    # ---- the vocabulary -------------------------------------------------------------
    events = list(Event.objects.filter(source=services.EVENT_SOURCE).order_by("id"))
    assert [e.kind for e in events] == [
        "connections.connected", "connections.granted", "connections.shared", "connections.grant_revoked",
        "connections.shared", "connections.disconnected", "connections.revoke_unconfirmed"]
    for e in events:
        assert e.level == "info" and e.payload["owner"] == "agent:eva"
        assert all(k in e.payload for k in BASE_KEYS)

    lines = [r for r in caplog.records if r.getMessage().startswith("connections.")]
    kinds = [r.getMessage().split(" ", 1)[0].removeprefix("connections.") for r in lines]
    assert kinds == ["provider_error", "provider_error", "connected", "disconnected", "revoke_unconfirmed"]
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


# ---- the machines' side: every path, scanned ------------------------------------------

LEAK = "SENTINEL-LEAK-7f3a"           # planted in request bodies a server must never echo
APP_SECRET = "APP-SECRET-SENTINEL-91c"
ACCESS_SENTINEL, REFRESH_SENTINEL, CODE_SENTINEL = "00D!ACCESS-SENTINEL-1", "5Aep_REFRESH-SENTINEL-1", "CODE-SENTINEL-1"


def _records(caplog):
    """Every record of the test, including those a fixture logged (`caplog.records` is only the call phase)."""
    return [*caplog.get_records("setup"), *caplog.get_records("call")]


def _scan_blob(caplog, responses=()):
    """Every log record (any logger, DEBUG), traceback, Event and response body, plus a
    dump of every Connection row."""
    tracebacks = [logging.Formatter().formatException(r.exc_info) for r in _records(caplog) if r.exc_info]
    return "\n".join([r.getMessage() for r in _records(caplog)] + tracebacks
                     + [json.dumps(e.payload) + e.summary for e in Event.objects.all()]
                     + [r.content.decode() for r in responses]
                     + [json.dumps(list(Connection.objects.values()), default=str)])


def _debug(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="apps")


@pytest.fixture
def full_run(caplog):
    """A connection used end to end: material, a keeper re-seal, a rotated write-back, a
    crashed lease, every refused write-back (with sentinels in the bodies), a person's
    connection disconnected with no cloud holder, and eva's disconnect confirmed by its
    holder. Returns the world, responses by kind, secrets, caplog."""
    from datetime import timedelta
    from uuid import uuid4

    from django.utils import timezone

    from tests.fixtures.connection_calls import (
        TokensWorld,
        _runner_of,
        _turn,
        lease,
        machine_write,
        material_for,
        reseal_body,
        runner_call,
        write_back,
    )

    _debug(caplog)
    w = TokensWorld()
    box_a_key = w.machines[w.box_a][0]
    conn = w.conn = w.shared(w.evas(), agents=[w.eva],
                             to=[("device", box_a_key.ecdh_jkt, box_a_key.ecdh_public_jwk)])
    out: dict[str, list] = {"material": [], "other": [], "errors": []}
    turn = _turn(w.eva, _runner_of(w, w.box_a))
    out["material"] += [material_for(w, w.box_a, turn=turn), material_for(w, w.box_a, turn=turn)]
    # box_b waits for a copy; box_a re-seals for it (route 14): `resealed`.
    out["other"].append(runner_call(w, w.box_b, "GET", "/vault-keeper/connections"))
    out["other"].append(runner_call(w, w.box_a, "PUT", f"/vault-keeper/connections/{conn.pk}",
                                    reseal_body(w, conn, w.box_a)))
    lid = lease(w, w.box_a)
    out["other"].append(lid)
    lease_id = lid.json()["lease_id"]
    # Refused write-backs, each with a sentinel planted in a field of the body.
    stranger = w.machines["ada-computer"][0]
    foreign = machine_write(w, conn, w.box_a, lease_id,
                            to=[*rcpt_list(conn), ("device", stranger.ecdh_jkt, stranger.ecdh_public_jwk)])
    tampered = machine_write(w, conn, w.box_a, lease_id)
    tampered["access_sig"]["macs"][0]["mac"] = "A" * 43
    stale = machine_write(w, conn, w.box_a, lease_id, expect="0" * 64)
    lost = machine_write(w, conn, w.box_a, str(uuid4()))
    for body, code in ((foreign, "foreign_reader"), (tampered, "bad_writer_sig"), (stale, "zk_value_changed"),
                       (lost, "lease_lost")):
        body["access_sig"]["leak"] = LEAK
        r = write_back(w, w.box_a, body)
        assert r.status_code in (409, 422) and r.json()["error"] == code, (code, r.content)
        out["errors"].append(r)
    r = runner_call(w, w.box_a, "POST", f"/connections/{conn.pk}/reconnect-required",
                    {"lease_id": str(uuid4()), "error": LEAK})
    assert r.status_code == 409
    out["errors"].append(r)
    body = machine_write(w, conn, w.box_a, lease_id, rotated=True)
    out["other"].append(write_back(w, w.box_a, body))
    # A lease taken and never written, expired, then taken by the other box.
    for name in (w.box_a, w.box_b):
        type(conn).objects.filter(pk=conn.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=5))
        out["other"].append(lease(w, name))
        if name == w.box_a:
            type(conn).objects.filter(pk=conn.pk).update(lease_written=False)
    # A person's connection that no cloud box holds: deleted at once, `revoke_unconfirmed`.
    mine = w.shared(w.persons(), my_computers=[w.computer.ecdh_jkt])
    services.disconnect(mine, by=w.person)
    services.disconnect(conn, by=w.person)
    out["other"].append(runner_call(w, w.box_a, "POST", f"/connections/{conn.pk}/revoked",
                                    {"revoked": True, "error": ""}))
    from tests.fixtures.connections import FAKE_REFRESH
    return w, out, ["00D!fresh", "5Aep_rotated", "same", FAKE_REFRESH, LEAK], caplog


def rcpt_list(conn):
    from apps.connections import recipients as rcpt

    return rcpt.recipients(conn)


def _kinds():
    return {e.kind.removeprefix("connections.") for e in Event.objects.filter(source=services.EVENT_SOURCE)}


def test_full_connection_lifecycle_writes_no_plaintext_token(full_run):
    w, out, secrets, caplog = full_run
    log_kinds = {r.getMessage().split(" ", 1)[0].removeprefix("connections.")
                 for r in _records(caplog) if r.getMessage().startswith("connections.")}
    # Every verb the machines' side can emit ran: resealed, refreshed, revoked, lease_expired_unwritten as
    # Events; the person's revoke_unconfirmed as a log line.
    assert {"token_issued", "resealed", "refreshed", "lease_expired_unwritten", "disconnected",
            "revoked"} <= _kinds()
    assert "revoke_unconfirmed" in log_kinds
    blob = _scan_blob(caplog, [r for rs in out.values() for r in rs])
    assert not [s for s in secrets if s in blob], "a plaintext token or planted value leaked"
    for e in Event.objects.filter(source=services.EVENT_SOURCE):
        assert e.kind.removeprefix("connections.") in services.AUDITED
    [issued] = Event.objects.filter(kind="connections.token_issued")
    assert issued.count == 2 and {"to", "runner", "machine", "turn"} <= set(issued.payload)


def test_refused_write_backs_echo_nothing_they_were_sent(full_run):
    _, out, _, _ = full_run
    assert len(out["errors"]) == 5
    assert not [r for r in out["errors"] if LEAK in r.content.decode()]


def test_connect_exchange_leaves_no_token_in_logs_events_responses_or_rows(world, caplog):
    user, eva, echo, priv = world
    _debug(caplog)
    browser = Client()
    browser.force_login(user)
    seen = []
    for agent in ({}, {"agent": "eva"}):
        start = reverse("connections_start", args=["salesforce", "w"])
        resp = browser.post(start, agent) if agent else browser.get(start)
        seen.append(resp)
        state = parse_qs(urlparse(resp["Location"]).query)["state"][0]
        with FakeSF(token=[_resp(body=_token_body(access_token=ACCESS_SENTINEL, refresh_token=REFRESH_SENTINEL))]):
            landed = browser.get(reverse("connections_callback", args=["salesforce", "w"]),
                                 {"code": CODE_SENTINEL, "state": state})
        seen.append(landed)
        assert "/share?" in landed["Location"]
    for c in Connection.objects.all():
        seen.append(browser.get(f"/api/connections/{c.pk}/share"))
        assert seen[-1].status_code == 200
    seen.append(browser.get("/api/connections/"))
    seen.append(browser.get("/api/agents/eva/connections"))
    # A redirect carries its target in a header, not a body: scan both.
    blob = _scan_blob(caplog, seen) + "\n".join(r.get("Location", "") for r in seen)
    assert [s for s in (ACCESS_SENTINEL, REFRESH_SENTINEL, CODE_SENTINEL) if s in blob] == []
    assert Connection.objects.count() == 2


def test_refreshes_coalesce_and_a_rotation_is_its_own_event(world_tokens):
    world = world_tokens
    from tests.fixtures.connection_calls import lease, machine_write, runner_call, write_back

    conn = world.conn = world.shared(world.evas(), agents=[world.eva])

    def refresh(rotated):
        lid = lease(world, world.box_a).json()["lease_id"]
        r = write_back(world, world.box_a, machine_write(world, conn, world.box_a, lid, rotated=rotated))
        assert r.status_code == 200, r.content
        runner_call(world, world.box_a, "DELETE", f"/connections/{conn.pk}/refresh-lease/{lid}")

    def refreshed():
        return {e.payload["rotated"]: e for e in Event.objects.filter(kind="connections.refreshed")}

    refresh(False)
    refresh(False)
    [plain] = Event.objects.filter(kind="connections.refreshed")
    assert plain.count == 2 and plain.payload["rotated"] is False      # same-day plains: one Event
    refresh(True)
    assert Event.objects.filter(kind="connections.refreshed").count() == 2
    assert refreshed()[True].count == 1                                  # the rotation is its own
    refresh(False)
    ev = refreshed()
    assert Event.objects.filter(kind="connections.refreshed").count() == 2
    assert ev[False].count == 3 and ev[True].count == 1                  # not folded into the rotation
    assert ev[True].payload["rotated"] is True


def test_app_secret_appears_only_in_material_responses(world_tokens, caplog):
    from tests.fixtures.connection_calls import _runner_of, _turn, material_for, person_call

    w = world_tokens
    _debug(caplog)
    WorkspaceMembership.objects.filter(workspace=w.ws, user=w.person).update(role=WorkspaceMembership.ADMIN)
    admin = Client()
    admin.force_login(w.person)
    grant_step_up(admin)
    apps_url = "/api/workspaces/w/connection-apps"
    others = [admin.put(f"{apps_url}/salesforce",
                        {"client_id": w.app.client_id, "client_secret": APP_SECRET,
                         "options": {"login_url": "https://login.salesforce.com"}},
                        content_type="application/json"),
              admin.get(apps_url)]
    assert others[0].status_code == 200, others[0].content
    eva_conn = w.shared(w.evas(), agents=[w.eva])
    mine = w.shared(w.persons(), my_computers=[w.computer.ecdh_jkt])
    turn = _turn(w.eva, _runner_of(w, w.box_a))
    material = [material_for(w, w.box_a, turn=turn), person_call(w, "GET", "/salesforce/material")]
    assert [m.status_code for m in material] == [200, 200]
    assert all(APP_SECRET in m.content.decode() for m in material)
    others += [admin.get(f"/api/connections/{eva_conn.pk}/share"), admin.get(f"/api/connections/{mine.pk}/share"),
               admin.get("/api/connections/"), admin.get("/api/agents/eva/connections")]
    # The column is ciphertext: read while the row is still there.
    stored = list(ConnectionApp.objects.values())
    assert stored and stored[0]["client_secret_enc"]
    assert APP_SECRET not in json.dumps(stored, default=str)
    others.append(admin.delete(f"{apps_url}/salesforce"))
    assert others[-1].status_code in (200, 204)
    assert {"app_configured", "app_removed"} <= _kinds()
    blob = _scan_blob(caplog, others)
    assert APP_SECRET not in blob


def test_a_persons_lent_connection_leaves_granted_in_the_agents_workspace(caplog):
    from tests.fixtures.connections import ShareWorld

    world = ShareWorld()
    conn = world.shared(world.persons(), agents=[world.eva])
    [ev] = Event.objects.filter(kind="connections.granted")
    assert ev.workspace_id == world.ws.pk and ev.payload["grantee"] == "eva"
    assert ev.payload["owner"] == f"user:{world.person.pk}"
    world.shared(conn, agents=[])
    [gone] = Event.objects.filter(kind="connections.grant_revoked")
    assert gone.workspace_id == world.ws.pk


@pytest.fixture
def world_tokens():
    from tests.fixtures.connection_calls import TokensWorld

    return TokensWorld()
