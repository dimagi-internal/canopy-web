"""Keepers re-seal, holders revoke, nothing waits forever (spec §4.1, §4.5; contract
§0.6 routes 5, 13-15; rulings R2, R7, R12).

A cloud box that holds a connection is told which machines the owner's statement
allows that hold no copy yet, and re-seals the SAME envelope for them (route 14).
Disconnecting marks the row `revoking`: it leaves every list at once and is handed
out nowhere, and the first holder online revokes at the provider and confirms
(route 15), which deletes it. A connection no cloud box holds is deleted at once,
and one nobody confirms within seven days is deleted by the sweep; both are
recorded `revoke_unconfirmed`. Removing an app hides it and the sweep deletes it
once its connections are gone.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from canopy_sdk.jose import make_dpop_proof
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.agents.credentials import vault_engine, zk, zk_seal
from apps.common import ticks
from apps.connections import keeper, services
from apps.connections import recipients as rcpt
from apps.connections.models import Connection, ConnectionApp
from apps.events.models import Event
from apps.harness.models import Runner, RunnerAccessToken, Turn
from apps.mcp import api_tools
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.connections import REDIRECT, REVOKE_URL, FakeSF, ShareWorld, _stored, sealed_to_owner, sf_app
from tests.fixtures.vault_zk import conn_ref

pytestmark = pytest.mark.django_db


class KeeperWorld(ShareWorld):
    """ShareWorld plus a second cloud box for eva (`eva-cloud-2`)."""

    def __init__(self):
        super().__init__()
        self.eva_box2, _, _ = self._box("eva-cloud-2", Runner.CLOUD, self.op, self.eva)

    def only_on_box_a(self) -> Connection:
        """eva's own, shared to eva but sealed only to eva-cloud: eva-cloud-2 waits."""
        return self.shared(self.evas(), agents=[self.eva],
                           to=[("device", self.eva_box.ecdh_jkt, self.eva_box.ecdh_public_jwk)])


@pytest.fixture
def world():
    return KeeperWorld()


def _call(world, name, method, path, body=None, *, proof=True):
    key, sign, _ = world.machines[name]
    raw, _ = RunnerAccessToken.mint(key)
    url = f"/api/harness/runners/{key.runner_id}{path}"
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    if proof:
        headers["HTTP_DPOP"] = make_dpop_proof(sign, method, f"http://testserver{url}")
    c = Client()
    if method == "GET":
        return c.get(url, **headers)
    return getattr(c, method.lower())(url, data=json.dumps(body or {}), content_type="application/json", **headers)


def work(world, name):
    return _call(world, name, "GET", "/vault-keeper/connections")


def reseal_body(world, conn, name, *, to=None):
    """What box `name` sends after opening the refresh envelope: the same plaintext
    re-sealed to `to` (default: everyone canopy-web lists), with its writer MACs."""
    key, _, ecdh = world.machines[name]
    conn.refresh_from_db()
    rs = rcpt.recipients(conn) if to is None else to
    pubs = {(k, t): zk.public_key(j) for k, t, j in rs}
    sealed = zk_seal.seal_to(json.dumps({"refresh_token": "same"}), agent_id=conn.binding, name="refresh", kv=0,
                             recipients=rs)
    return {"sealed": sealed,
            "sealed_sig": conn_ref.writer_sig(conn.binding, "refresh", conn.sealed_digest, sealed, ecdh, "device",
                                              key.ecdh_jkt, pubs),
            "access_sealed": "", "access_sig": None, "expect": conn.sealed_digest}


def reseal(world, name, conn, body):
    return _call(world, name, "PUT", f"/vault-keeper/connections/{conn.pk}", body)


def confirm(world, name, conn, revoked=True, error=""):
    return _call(world, name, "POST", f"/connections/{conn.pk}/revoked", {"revoked": revoked, "error": error})


def events(kind):
    return list(Event.objects.filter(kind=f"connections.{kind}").order_by("pk"))


def recorded(kind, caplog) -> bool:
    """An Event (an agent's connection) or a log line (a person's)."""
    return bool(events(kind)) or any(r.getMessage().startswith(f"connections.{kind} ") for r in caplog.records)


def _turn(agent, runner):
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_EMAIL, prompt="x",
                               idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=runner)


def material(world, name, agent=None):
    key = world.machines[name][0]
    turn = _turn(agent or world.eva, key.runner)
    return _call(world, name, "GET", f"/turns/{turn.pk}/connections/salesforce")


# ---- keeper work (route 13) ----------------------------------------------------------

def test_keeper_work_lists_only_connections_this_box_holds(world):
    conn = world.only_on_box_a()
    r = work(world, "eva-cloud")
    assert r.status_code == 200, r.content
    [item] = r.json()["connections"]
    # Every machine that runs eva and holds no copy: eva-cloud-2, and ada's laptop.
    assert item["id"] == str(conn.pk)
    assert item["waiting"] == sorted([world.eva_box2.ecdh_jkt, world.ada_laptop.ecdh_jkt])
    assert item["revoking"] is False and item["sealed"] == Connection.objects.get().sealed
    assert item["app"]["client_secret"] and item["statement"] == Connection.objects.get().statement
    assert item["reseal_path"] == f"/api/harness/runners/{world.eva_box.runner_id}/vault-keeper/connections/{conn.pk}"
    assert item["revoked_path"] == f"/api/harness/runners/{world.eva_box.runner_id}/connections/{conn.pk}/revoked"
    assert {e["agent_id"] for e in item["endorsements"]} <= {str(world.eva.pk)}
    # The waiting box holds nothing; another agent's box holds nothing.
    assert work(world, "eva-cloud-2").json() == {"connections": []}
    assert work(world, "echo-cloud").json() == {"connections": []}
    # Once nobody waits, there is no work.
    Connection.objects.all().delete()
    world.shared(world.evas(), agents=[world.eva])
    assert work(world, "eva-cloud").json() == {"connections": []}


def test_keeper_work_needs_a_device_proof(world):
    world.only_on_box_a()
    r = _call(world, "eva-cloud", "GET", "/vault-keeper/connections", proof=False)
    assert r.status_code == 401 and r.json()["error"] == "device_proof_required"


def test_a_laptop_that_holds_a_connection_keeps_nothing(world):
    """Only cloud runners keep or revoke: ada's laptop, which runs eva, is no keeper."""
    world.shared(world.evas(), agents=[world.eva],
                 to=[("device", world.ada_laptop.ecdh_jkt, world.ada_laptop.ecdh_public_jwk)])
    assert work(world, "ada-laptop").json() == {"connections": []}


# ---- re-sealing (route 14) -----------------------------------------------------------

def test_keeper_reseal_adds_a_waiting_box_and_records_resealed(world):
    conn = world.only_on_box_a()
    assert material(world, "eva-cloud-2").json()["error"] == "waiting"
    r = reseal(world, "eva-cloud", conn, reseal_body(world, conn, "eva-cloud"))
    assert r.status_code == 200, r.content
    assert r.json()["machines"] == ["ada-laptop", "eva-cloud", "eva-cloud-2"]
    conn.refresh_from_db()
    assert ("device", world.eva_box2.ecdh_jkt) in zk_seal.sealed_targets(conn.sealed)
    assert conn.sealed_digest == vault_engine.value_digest(conn.sealed)
    [ev] = events("resealed")
    assert ev.payload["machine"] == "eva-cloud" and ev.payload["added"] == "ada-laptop,eva-cloud-2"
    assert material(world, "eva-cloud-2").status_code == 200
    assert work(world, "eva-cloud").json() == {"connections": []}


def test_keeper_reseal_by_a_non_holder_is_refused(world):
    conn = world.only_on_box_a()
    r = reseal(world, "eva-cloud-2", conn, reseal_body(world, conn, "eva-cloud-2"))
    assert r.status_code == 403 and r.json()["error"] == "zk_not_a_holder"
    # A holder that leaves itself out is no holder of what it writes either.
    to = [x for x in rcpt.recipients(conn) if x[1] != world.eva_box.ecdh_jkt]
    r = reseal(world, "eva-cloud", conn, reseal_body(world, conn, "eva-cloud", to=to))
    assert r.status_code == 403 and r.json()["error"] == "zk_not_a_holder"
    assert not events("resealed")


def test_keeper_reseal_checks_as_a_write_back(world):
    conn = world.only_on_box_a()
    body = reseal_body(world, conn, "eva-cloud")
    r = reseal(world, "eva-cloud", conn, {**body, "expect": "0" * 64})
    assert r.status_code == 409 and r.json()["error"] == "zk_value_changed"
    echo = ("device", world.echo_box.ecdh_jkt, world.echo_box.ecdh_public_jwk)
    r = reseal(world, "eva-cloud", conn, reseal_body(world, conn, "eva-cloud", to=[*rcpt.recipients(conn), echo]))
    assert r.status_code == 422 and r.json()["error"] == "foreign_reader"
    no_owner = [x for x in rcpt.recipients(conn) if x[0] != "member"]
    r = reseal(world, "eva-cloud", conn, reseal_body(world, conn, "eva-cloud", to=no_owner))
    assert r.status_code == 422 and r.json()["error"] == "zk_rewrap_required"
    other = reseal_body(world, conn, "eva-cloud-2")      # MACs by eva-cloud-2's key
    r = reseal(world, "eva-cloud", conn, {**body, "sealed_sig": other["sealed_sig"]})
    assert r.status_code == 422 and r.json()["error"] == "bad_writer_sig"


def test_an_access_envelope_must_be_resealed_with_the_refresh_one(world):
    conn = world.only_on_box_a()
    Connection.objects.filter(pk=conn.pk).update(access_sealed=conn.sealed, access_sig=conn.sealed_sig)
    r = reseal(world, "eva-cloud", conn, reseal_body(world, conn, "eva-cloud"))
    assert r.status_code == 422 and r.json()["error"] == "zk_rewrap_required"


# ---- disconnecting (route 5) -----------------------------------------------------------

def test_disconnect_hides_the_row_at_once_and_hands_it_out_nowhere(world):
    conn = world.shared(world.evas(), agents=[world.eva])
    services.disconnect(conn, by=world.person)
    conn.refresh_from_db()
    assert conn.state == Connection.REVOKING and conn.revoking_since is not None
    assert len(events("disconnected")) == 1
    c = Client()
    c.force_login(world.person)
    assert c.get("/api/agents/eva/connections").json()["connections"] == []
    assert services.providers_for_agent(world.eva) == []
    assert material(world, "eva-cloud").json()["error"] == "not_connected"
    # Disconnecting it again changes nothing.
    since = conn.revoking_since
    services.disconnect(conn, by=world.person)
    conn.refresh_from_db()
    assert conn.revoking_since == since and len(events("disconnected")) == 1
    # Every cloud holder is told to revoke it.
    for box in ("eva-cloud", "eva-cloud-2"):
        [item] = work(world, box).json()["connections"]
        assert item["revoking"] is True and item["revoke_at_provider"] is True and item["waiting"] == []


def test_a_persons_disconnect_route_hides_it_from_their_list(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    c = Client()
    c.force_login(world.person)
    assert c.delete(f"/api/connections/{conn.pk}").status_code == 204
    assert Connection.objects.get().state == Connection.REVOKING
    assert c.get("/api/connections/").json()["connections"] == []


def test_disconnecting_an_unshared_connection_deletes_it_at_once(world, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    unshared = world.persons()
    services.disconnect(unshared, by=world.person)
    assert not Connection.objects.exists() and recorded("revoke_unconfirmed", caplog)


def test_a_connection_only_a_laptop_holds_is_deleted_at_once(world):
    """A laptop never revokes (R3), so nothing would ever finish it."""
    conn = world.shared(world.evas(), agents=[world.eva],
                        to=[("device", world.ada_laptop.ecdh_jkt, world.ada_laptop.ecdh_public_jwk)])
    services.disconnect(conn, by=world.person)
    assert not Connection.objects.exists()
    [ev] = events("revoke_unconfirmed")
    assert ev.payload["connection"] == str(conn.pk)


def test_revoke_at_provider_false_when_another_connection_of_that_account_remains(world):
    theirs = world.shared(world.persons(), agents=[world.eva])
    evas = world.shared(world.evas(), agents=[world.eva])
    assert theirs.account_id == evas.account_id and theirs.app_id == evas.app_id
    services.disconnect(evas, by=world.person)
    evas.refresh_from_db()
    assert evas.revoke_at_provider is False
    # The last one of that account through that app: revoked at the provider.
    services.disconnect(theirs, by=world.person)
    theirs.refresh_from_db()
    assert theirs.revoke_at_provider is True
    # And work says so per row, never revoking while a live connection of the account appears.
    flags = {i["id"]: i["revoke_at_provider"] for i in work(world, "eva-cloud").json()["connections"]}
    assert flags == {str(evas.pk): False, str(theirs.pk): True}


# ---- confirming a revoke (route 15) --------------------------------------------------

def test_first_holder_confirms_revoked_and_the_row_is_deleted(world):
    conn = world.shared(world.evas(), agents=[world.eva])
    r = confirm(world, "eva-cloud", conn)
    assert r.status_code == 409 and r.json()["error"] == "not_revoking"
    services.disconnect(conn, by=world.person)
    r = confirm(world, "echo-cloud", conn)
    assert r.status_code == 403 and r.json()["error"] == "zk_not_a_holder"
    assert confirm(world, "eva-cloud", conn).status_code == 204
    assert not Connection.objects.exists()
    [ev] = events("revoked")
    assert ev.payload["machine"] == "eva-cloud" and ev.payload["at_provider"] is True
    # The second holder, a moment later: already gone.
    assert confirm(world, "eva-cloud-2", conn).status_code == 404


def test_a_failed_revoke_is_recorded_and_the_row_still_goes(world, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    conn = world.shared(world.evas(), agents=[world.eva])
    services.disconnect(conn, by=world.person)
    assert confirm(world, "eva-cloud", conn, revoked=False, error="invalid_token").status_code == 204
    assert not Connection.objects.exists() and not events("revoked")
    assert recorded("revoke_failed", caplog)


# ---- the sweep -----------------------------------------------------------------------

def test_revoking_for_seven_days_deletes_and_records_revoke_unconfirmed(world):
    old = world.shared(world.evas(), agents=[world.eva])
    services.disconnect(old, by=world.person)
    now = timezone.now()
    Connection.objects.filter(pk=old.pk).update(revoking_since=now - keeper.REVOKE_GRACE + timedelta(hours=1))
    assert keeper.sweep(now=now)["revoke_unconfirmed"] == 0
    assert Connection.objects.filter(pk=old.pk).exists()
    out = keeper.sweep(now=now + timedelta(hours=2))
    assert out["revoke_unconfirmed"] == 1 and not Connection.objects.exists()
    [ev] = events("revoke_unconfirmed")
    assert ev.payload["connection"] == str(old.pk)


def test_a_revoking_row_whose_last_holder_is_gone_is_swept_at_once(world):
    conn = world.shared(world.evas(), agents=[world.eva],
                        to=[("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    services.disconnect(conn, by=world.person)
    assert Connection.objects.get().state == Connection.REVOKING
    Runner.objects.filter(pk=world.eva_box.runner_id).update(status=Runner.RETIRED)
    assert keeper.sweep()["revoke_unconfirmed"] == 1 and not Connection.objects.exists()


def test_removing_an_app_revokes_its_connections_then_deletes_the_app(world):
    conn = world.shared(world.evas(), agents=[world.eva])
    services.remove_app(world.app, by=world.person)
    app = ConnectionApp.objects.get()
    assert app.removed_at is not None
    assert Connection.objects.get().state == Connection.REVOKING
    assert [e.payload["workspace"] for e in events("app_removed")] == ["w"]
    # Hidden at once: nobody connects with it, and Settings lists it as not set up.
    with pytest.raises(services.NotConfigured):
        services.app_for("w", "salesforce")
    WorkspaceMembership.objects.filter(workspace=world.ws, user=world.person).update(role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(world.person)
    [row] = [a for a in c.get("/api/workspaces/w/connection-apps").json()["apps"] if a["provider"] == "salesforce"]
    assert row["configured"] is False
    # Still the client the holder revokes with.
    [item] = work(world, "eva-cloud").json()["connections"]
    assert item["app"]["client_id"] == app.client_id and item["revoking"] is True
    assert keeper.sweep()["apps_deleted"] == 0
    assert confirm(world, "eva-cloud", conn).status_code == 204
    assert keeper.sweep()["apps_deleted"] == 1 and not ConnectionApp.objects.exists()


def test_removing_an_app_with_nothing_held_deletes_it_at_once(world):
    world.persons()
    services.remove_app(world.app, by=world.person)
    assert not Connection.objects.exists() and not ConnectionApp.objects.exists()


def test_setting_a_removed_app_up_again_brings_it_back(world):
    world.shared(world.evas(), agents=[world.eva])
    services.remove_app(world.app, by=world.person)
    services.configure_app("w", "salesforce", client_id=world.app.client_id, by=world.person)
    app = ConnectionApp.objects.get()
    assert app.removed_at is None and services.app_for("w", "salesforce").pk == app.pk
    assert Connection.objects.get().state == Connection.REVOKING


def test_the_sweep_command_runs_the_sweep(world, capsys):
    conn = world.shared(world.evas(), agents=[world.eva])
    services.disconnect(conn, by=world.person)
    Connection.objects.update(revoking_since=timezone.now() - timedelta(days=8))
    call_command("connections_sweep")
    assert not Connection.objects.exists() and "revoke_unconfirmed=1" in capsys.readouterr().out


def test_the_sweep_is_on_the_off_request_clock():
    assert "connections.sweep" in ticks.jobs()


# ---- a reconnect never resurrects a row being revoked --------------------------------

def _connect(world, owner, by):
    session = {}
    url = services.begin(session, app=world.app, owner=owner, redirect_uri=REDIRECT)
    state = parse_qs(urlparse(url).query)["state"][0]
    return services.complete(session, provider="salesforce", workspace="w", code="c0de", state=state,
                             redirect_uri=REDIRECT, by=by)


def test_reconnecting_an_account_being_revoked_is_refused_and_the_new_grant_revoked(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    services.disconnect(conn, by=world.person)
    before = Connection.objects.get()
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        _connect(world, ("user", world.person.pk), world.person)
    assert e.value.code == "revoking"
    after = Connection.objects.get()
    assert (after.state, after.sealed, after.revoking_since) == (Connection.REVOKING, before.sealed,
                                                                  before.revoking_since)
    # Salesforce: the grant just issued is revoked; the old one is the holder's to revoke.
    assert [c[2]["token"] for c in fake.calls if c[1] == REVOKE_URL] == ["5Aep_refresh_1"]


def test_the_callback_lands_with_reason_revoking(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    services.disconnect(conn, by=world.person)
    client = Client()
    client.force_login(world.person)
    with FakeSF():
        start = client.get(reverse("connections_start", args=["salesforce", "w"]))
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        resp = client.get(reverse("connections_callback", args=["salesforce", "w"]), {"code": "c", "state": state})
    q = parse_qs(urlparse(resp["Location"]).query)
    assert q["status"] == ["error"] and q["reason"] == ["revoking"]
    assert Connection.objects.get().state == Connection.REVOKING


def test_a_person_with_a_row_deleted_at_once_connects_again_freely(world):
    sealed_to_owner({"user": world.person}, world.person, app=world.app)
    services.disconnect(Connection.objects.get(), by=world.person)
    with FakeSF():
        conn = _connect(world, ("user", world.person.pk), world.person)
    assert conn.state == Connection.UNSHARED


# ---- not model tools -------------------------------------------------------------------

@pytest.mark.parametrize("op", ["get_connection_keeper_work", "put_connection_reseal", "post_connection_revoked"])
def test_keeper_routes_are_not_mcp_tools(op):
    assert op in api_tools.EXCLUDED


def test_a_stored_connection_with_no_readable_envelope_is_deleted_at_once(world):
    conn = _stored({"agent": world.eva}, world.person, app=sf_app(world.ws))
    services.disconnect(conn, by=world.person)
    assert not Connection.objects.exists()
