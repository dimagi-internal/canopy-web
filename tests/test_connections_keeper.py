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

from apps.agents.credentials import vault_engine, zk, zk_endorse, zk_seal
from apps.agents.models import AgentAdmin, AgentVaultEndorsement
from apps.common import ticks
from apps.connections import keeper, services, sharing
from apps.connections import recipients as rcpt
from apps.connections.models import Connection, ConnectionApp
from apps.events.models import Event
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerAccessToken, Turn
from apps.mcp import api_tools
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.connections import (
    REDIRECT,
    REVOKE_URL,
    FakeSF,
    ShareWorld,
    _stored,
    browser_share,
    endorse,
    revoke_admin,
    sealed_to_owner,
    sf_app,
    vault_key,
)
from tests.fixtures.vault_zk import conn_ref
from tests.fixtures.vault_zk import ref as zk_ref

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
    assert {e["agent_id"] for e in item["endorsements"]} <= {str(world.eva.pk)}
    # The waiting box holds nothing; another agent's box holds nothing.
    assert work(world, "eva-cloud-2").json() == {"connections": []}
    assert work(world, "echo-cloud").json() == {"connections": []}
    # Once nobody waits, there is no work.
    Connection.objects.all().delete()
    world.shared(world.evas(), agents=[world.eva])
    assert work(world, "eva-cloud").json() == {"connections": []}


def test_keeper_work_includes_a_pruned_writers_endorsement(world):
    """Nobody can re-seal an envelope they cannot validate, so the box re-sealing for
    a waiting machine still gets what vouches for the writer that has left."""
    conn = world.shared(world.persons(), agents=[world.eva, world.echo], to=[
        ("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk),
        ("device", world.echo_box.ecdh_jkt, world.echo_box.ecdh_public_jwk)])
    Connection.objects.filter(pk=conn.pk).update(sealed_sig={
        **conn.sealed_sig, "by": {"kind": "device", "target": world.echo_box.ecdh_jkt,
                                  "public_jwk": world.echo_box.ecdh_public_jwk}})
    AgentAdmin.objects.filter(agent=world.echo, user=world.person).delete()
    conn.refresh_from_db()
    assert rcpt.prune(conn) == ["echo-cloud"]
    [item] = work(world, "eva-cloud").json()["connections"]
    assert world.eva_box2.ecdh_jkt in item["waiting"]
    [e] = [e for e in item["endorsements"] if e["target"] == world.echo_box.ecdh_jkt]
    assert e["agent_id"] == str(world.echo.pk) and e["by_public_jwk"]


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
    assert c.delete(f"/api/connections/{conn.pk}").json() == {"outcome": "revoking", "machine_revokes": True}
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
    [item] = work(world, "eva-cloud").json()["connections"]
    assert item["id"] == str(evas.pk) and item["revoke_at_provider"] is False
    # The last one of that account through that app: revoked at the provider.
    services.disconnect(theirs, by=world.person)
    theirs.refresh_from_db()
    assert theirs.revoke_at_provider is True
    flags = {i["id"]: i["revoke_at_provider"] for i in work(world, "eva-cloud").json()["connections"]}
    assert flags == {str(evas.pk): True, str(theirs.pk): True}


def test_revoke_at_provider_is_decided_when_the_holder_reads_it(world):
    """A revoking with the flag false because B was live; B then goes without any
    revoke at the provider (deleted at once, R7). A must now be revoked there, or
    nothing ever revokes its token."""
    a = world.shared(world.evas(), agents=[world.eva])
    b = sealed_to_owner({"user": world.person}, world.person, app=world.app)     # live, held by nobody
    services.disconnect(a, by=world.person)
    [item] = work(world, "eva-cloud").json()["connections"]
    assert item["revoke_at_provider"] is False
    services.disconnect(b, by=world.person)                                      # deleted at once
    assert not Connection.objects.filter(pk=b.pk).exists()
    [item] = work(world, "eva-cloud").json()["connections"]
    assert item["revoke_at_provider"] is True
    assert confirm(world, "eva-cloud", a).status_code == 204
    assert events("revoked")[0].payload["at_provider"] is True


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


def test_an_app_set_up_again_during_the_sweep_keeps_its_connections(world, monkeypatch):
    """The sweep listed the removed app; an admin set it up again and someone
    connected through it before the sweep got to it: nothing of it is touched."""
    world.shared(world.evas(), agents=[world.eva])
    services.remove_app(world.app, by=world.person)
    real = keeper._finish_removal

    def restored_first(app):
        services.configure_app("w", "salesforce", client_id=world.app.client_id, by=world.person)
        sealed_to_owner({"user": world.person}, world.person, app=ConnectionApp.objects.get())
        return real(app)
    monkeypatch.setattr(keeper, "_finish_removal", restored_first)
    assert keeper.sweep()["apps_deleted"] == 0
    app = ConnectionApp.objects.get()
    assert app.removed_at is None
    assert Connection.objects.get(user=world.person).state == Connection.UNSHARED


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


def test_a_disconnect_committing_during_the_connect_is_seen_by_the_upsert(world, monkeypatch):
    """The row is `active` when the callback starts; a disconnect commits between
    then and the upsert's row lock. The upsert must refuse on the row it locked."""
    conn = world.shared(world.persons(), agents=[world.eva])
    real = services._upsert_connection

    def racing(lookup, defaults, seal):
        services.disconnect(Connection.objects.get(pk=conn.pk), by=world.person)
        return real(lookup, defaults, seal)
    monkeypatch.setattr(services, "_upsert_connection", racing)
    before = Connection.objects.get()
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        _connect(world, ("user", world.person.pk), world.person)
    assert e.value.code == "revoking"
    after = Connection.objects.get()
    assert after.state == Connection.REVOKING
    assert (after.sealed, after.sealed_digest, after.statement_version) == (
        before.sealed, before.sealed_digest, before.statement_version)
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


# ---- a holder that stopped -----------------------------------------------------------

def _silent_for(minutes, *boxes):
    Runner.objects.filter(pk__in=[b.runner_id for b in boxes]).update(
        last_heartbeat_at=timezone.now() - timedelta(minutes=minutes))


def _stopped(*boxes):
    _silent_for(20, *boxes)


def test_a_box_silent_for_two_minutes_still_counts_and_for_twenty_does_not(world):
    """A reboot must not lose a revoke, and an outage of canopy-web leaves every box silent."""
    conn = world.only_on_box_a()
    _silent_for(2, world.eva_box)
    assert rcpt.revokers(conn) == [world.eva_box.ecdh_jkt]
    services.disconnect(conn, by=world.person)
    assert keeper.sweep()["revoke_unconfirmed"] == 0
    assert Connection.objects.get().state == Connection.REVOKING
    _silent_for(20, world.eva_box)
    assert rcpt.revokers(conn) == []
    assert keeper.sweep()["revoke_unconfirmed"] == 1
    [ev] = events("revoke_unconfirmed")
    assert ev.level == "warn" and ev.payload["reason"] == "no_holder"


def test_a_stopped_holder_is_no_revoker_so_a_disconnect_deletes_at_once(world):
    conn = world.only_on_box_a()
    _stopped(world.eva_box)
    assert rcpt.revokers(conn) == []
    c = Client()
    c.force_login(world.person)
    grant_step_up(c)
    assert c.delete(f"/api/agents/eva/connections/{conn.pk}").json() == {"outcome": "deleted", "machine_revokes": False}
    assert not Connection.objects.exists()


def test_a_revoking_row_whose_holders_stopped_is_swept_and_the_account_can_connect_again(world):
    conn = world.shared(world.persons(), agents=[world.eva], to=[
        ("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)])
    services.disconnect(conn, by=world.person)
    assert keeper.sweep()["revoke_unconfirmed"] == 0  # still up: it has its chance
    _stopped(world.eva_box)
    assert keeper.sweep()["revoke_unconfirmed"] == 1
    with FakeSF():
        assert _connect(world, ("user", world.person.pk), world.person).state == Connection.UNSHARED


def test_a_stopped_box_still_counts_as_one_prune_keeps_to_revoke(world):
    conn = world.shared(world.persons(), agents=[world.echo])
    _stopped(world.echo_box)
    revoke_admin(world.person, world.echo)
    assert rcpt.prune(conn) == []  # it may come back; only a disconnect asks whether it is up now


# ---- an owner key replaced, or its signer gone ---------------------------------------

def _validates_as_a_box_does(body, owner_key):
    """What a box checks before trusting anything: the key shown has the thumbprint sealed
    in the envelope, and the statement verifies under it."""
    jwk = body["owner"]["public_jwk"]
    assert jwk and device_keys.thumbprint(jwk) == owner_key
    assert body["owner"]["user_id"]
    assert conn_ref.verify_statement(body["statement"], body["statement_sig"], zk.public_key(jwk))


def test_a_person_who_replaced_their_key_disconnects_a_holder_revokes_and_they_connect(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    old_key = conn.owner_key
    vault_key(world.person)

    # Boxes keep being able to judge it.
    r = material(world, "eva-cloud")
    assert r.status_code == 200, r.content
    _validates_as_a_box_does(r.json(), old_key)
    # The share page explains; connecting straight over it is refused (R10).
    c = Client()
    c.force_login(world.person)
    r = c.get(f"/api/connections/{conn.pk}/share")
    assert r.status_code == 403 and r.json()["error"] == "not_the_owner_key"
    assert "Disconnect it, then connect it again" in r.json()["detail"]
    with FakeSF(), pytest.raises(services.ConnectionError) as e:
        _connect(world, ("user", world.person.pk), world.person)
    assert e.value.code == "owner_changed"

    # The way out: disconnect, a holder can read it and revokes it, connect again.
    assert c.delete(f"/api/connections/{conn.pk}").json() == {"outcome": "revoking", "machine_revokes": True}
    [item] = work(world, "eva-cloud").json()["connections"]
    assert item["revoking"] is True
    _validates_as_a_box_does(item, old_key)
    assert confirm(world, "eva-cloud", conn).status_code == 204
    with FakeSF():
        fresh = _connect(world, ("user", world.person.pk), world.person)
    assert fresh.pk != conn.pk and fresh.owner_key != old_key
    assert device_keys.thumbprint(fresh.owner_public_jwk) == fresh.owner_key


def test_re_endorsing_under_a_new_key_keeps_the_old_signature_for_a_holders_revoke(world):
    """`zk_endorse.add` replaces the row, so only the snapshot still holds what the sealed
    owner key signed. Without it every box refuses the writer and the revoke never runs."""
    conn = world.only_on_box_a()
    old_key = conn.owner_key
    key = world.eva_box
    assert [(e["agent_id"], e["target"]) for e in conn.owner_endorsements if e["target"] == key.ecdh_jkt] == [
        (str(world.eva.pk), key.ecdh_jkt)]
    # A box wrote the envelope last, so a reader needs an endorsement to accept it.
    assert reseal(world, "eva-cloud", conn, reseal_body(world, conn, "eva-cloud")).status_code == 200
    conn.refresh_from_db()
    assert conn.sealed_sig["by"]["target"] == key.ecdh_jkt

    new = vault_key(world.person)
    endorse(world.eva, key, world.person, new)  # the live row now holds the new key's signature
    live = AgentVaultEndorsement.objects.get(agent=world.eva, target=key.ecdh_jkt, endorser=world.person)
    assert live.endorser_jkt != old_key

    assert key.ecdh_jkt in rcpt.revokers(conn)
    services.disconnect(conn, by=world.person)
    assert Connection.objects.get().state == Connection.REVOKING
    [item] = work(world, "eva-cloud").json()["connections"]
    _validates_as_a_box_does(item, old_key)
    rows = [e for e in item["endorsements"] if e["target"] == key.ecdh_jkt and e["by"] == str(world.person.pk)]
    assert len(rows) == 1 and rows[0]["by_jkt"] == old_key
    assert device_keys.thumbprint(rows[0]["by_public_jwk"]) == old_key
    assert zk_endorse.verify(world.eva.pk, kind="device", target=key.ecdh_jkt, key_jkt=key.ecdh_jkt, sig=rows[0]["sig"],
                             endorser_jwk=rows[0]["by_public_jwk"])
    assert confirm(world, "eva-cloud", conn).status_code == 204


def test_a_share_keeps_the_owners_old_signature_and_a_tampered_snapshot_admits_nobody(world):
    conn = world.only_on_box_a()
    key = world.eva_box
    kept = list(conn.owner_endorsements)
    endorse(world.eva, key, world.person, vault_key(world.person))
    conn.refresh_from_db()
    # Snapshotted again with the live row now under another key: the old signature stands.
    assert rcpt.snapshot_owner_endorsements(conn) == kept
    assert key.ecdh_jkt in {t for k, t, _ in rcpt.recipients(conn) if k == "device"}
    # The server's word for it is worth nothing: a signature that does not verify is no one's vouching.
    conn.owner_endorsements = [{**e, "sig": kept[-1]["sig"][::-1]} for e in kept]
    assert key.ecdh_jkt not in {t for k, t, _ in rcpt.recipients(conn) if k == "device"}


def _adas_connection(world):
    """eva's, connected and shared by ada, who could be deleted: eva-cloud vouched for by jj,
    whom she names, and sealed to it."""
    conn = sealed_to_owner({"agent": world.eva}, world.ada, app=world.app)
    box = ("device", world.eva_box.ecdh_jkt, world.eva_box.ecdh_public_jwk)
    jj = zk_ref.jkt(world.person_priv.public_key())
    return sharing.share(conn, user=world.ada, **browser_share(conn, world.ada, world.ada_priv, agents=[world.eva],
                                                               to=[box], endorsers=[jj]))


def test_an_agents_connection_whose_signer_was_deleted_can_still_be_revoked(world):
    conn = _adas_connection(world)
    signer_id, old_key = str(world.ada.pk), conn.owner_key
    world.ada.delete()
    conn.refresh_from_db()
    assert conn.signer_id is None

    services.disconnect(conn, by=world.op)
    conn.refresh_from_db()
    assert conn.state == Connection.REVOKING
    [item] = work(world, "eva-cloud").json()["connections"]
    assert item["revoking"] is True and item["owner"]["user_id"] == signer_id
    _validates_as_a_box_does(item, old_key)
    assert confirm(world, "eva-cloud", conn).status_code == 204
    assert not Connection.objects.filter(pk=conn.pk).exists()


def test_an_envelope_no_holder_could_accept_is_deleted_at_once_on_disconnect(world):
    # A device wrote it, and the only endorsement of that device went with its deleted signer.
    conn = _adas_connection(world)
    AgentVaultEndorsement.objects.filter(target=world.eva_box.ecdh_jkt).delete()
    conn.sealed_sig = {**conn.sealed_sig, "by": {"kind": "device", "target": world.eva_box.ecdh_jkt,
                                                 "public_jwk": world.eva_box.ecdh_public_jwk}}
    conn.save()
    assert rcpt.revokers(conn) == []
    services.disconnect(conn, by=world.op)
    assert not Connection.objects.filter(pk=conn.pk).exists()


def test_a_replaced_key_still_verifies_what_the_owner_vouched_for_and_only_that(world):
    conn = world.shared(world.persons(), agents=[world.eva])
    new = vault_key(world.person)
    # The owner's old endorsement stands, under the old key, for this connection.
    assert world.eva_box.ecdh_jkt in {t for k, t, _ in rcpt.recipients(conn) if k == "device"}
    # A row claiming to be the owner's, signed by the new key, does not, with no snapshot to fall back on.
    conn.owner_endorsements = []
    forged = zk_ref.endorse(world.eva.pk, "device", world.eva_box.ecdh_jkt, zk.public_key(world.eva_box.ecdh_public_jwk),
                            new, world.person.pk)
    AgentVaultEndorsement.objects.filter(target=world.eva_box.ecdh_jkt).update(sig=forged["sig"])
    assert world.eva_box.ecdh_jkt not in {t for k, t, _ in rcpt.recipients(conn) if k == "device"}


# ---- not model tools -------------------------------------------------------------------

@pytest.mark.parametrize("op", ["get_connection_keeper_work", "put_connection_reseal", "post_connection_revoked"])
def test_keeper_routes_are_not_mcp_tools(op):
    assert op in api_tools.EXCLUDED


def test_a_stored_connection_with_no_readable_envelope_is_deleted_at_once(world):
    conn = _stored({"agent": world.eva}, world.person, app=sf_app(world.ws))
    services.disconnect(conn, by=world.person)
    assert not Connection.objects.exists()
