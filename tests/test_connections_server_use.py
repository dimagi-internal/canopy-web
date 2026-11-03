"""The `server_use` switch: canopy-web as one more `device` recipient of a connection.

A new Salesforce connection is sealed to its owner and canopy-web; Google's, or any
connection when the key is missing or KMS is down, to the owner alone, and the sign-in
never fails for it. A reconnect keeps the owner's choice. On an `active` row only the
signed statement lets canopy-web stay; while `unshared`, an old statement decides nothing.
A connection lent with limits is never canopy-web's.
"""
from __future__ import annotations

import json
import logging
from unittest import mock
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.agents.credentials import vault_engine, zk_seal
from apps.connections import keeper, server_key, server_tokens, services, sharing, tokens
from apps.connections import recipients as rcpt
from apps.connections.models import Connection, ConnectionApp
from apps.connections.providers.google import GoogleProvider
from apps.connections.providers.salesforce import SalesforceProvider
from apps.connections.wire import WireError
from apps.passkeys.testing import grant_step_up
from tests.fixtures.connection_calls import TokensWorld, lease, machine_write, write_back
from tests.fixtures.connections import (
    REDIRECT,
    FakeGoogle,
    FakeSF,
    ShareWorld,
    _resp,
    browser_share,
    refused,
    sealed_to_owner,
)
from tests.fixtures.vault_zk import ref as zk_ref

pytestmark = pytest.mark.django_db

FAKE_REFRESH = "5Aep_refresh_1"
OTHER_KEY = "A" * 43


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


@pytest.fixture
def kms_down():
    """A key is configured, and KMS refuses every call."""
    server_key._reset()
    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetPublicKey")
    client = mock.Mock()
    client.get_public_key.side_effect = denied
    with override_settings(CONNECTIONS_SERVER_KMS_KEY="alias/canopy-web-connections"), \
            mock.patch.object(server_key, "_client", return_value=client):
        yield client
    server_key._reset()


@pytest.fixture
def world():
    return ShareWorld()


def connect(world, *, provider="salesforce", **limits) -> Connection:
    session = {}
    app = ConnectionApp.objects.get(workspace=world.ws, provider=provider)
    url = services.begin(session, app=app, owner=("user", world.person.pk), redirect_uri=REDIRECT, **limits)
    state = parse_qs(urlparse(url).query)["state"][0]
    return services.complete(session, provider=provider, workspace=world.ws.slug, code="c0de", state=state,
                             redirect_uri=REDIRECT, by=world.person)


def owner_target(world) -> tuple[str, str]:
    return ("member", str(world.person.pk))


def canopy_target() -> tuple[str, str]:
    return ("device", server_key.thumbprint())


def owner_opens(world, conn) -> dict:
    return json.loads(zk_ref.open_sealed_value(conn.sealed, conn.binding, "refresh", "member",
                                               str(world.person.pk), world.person_priv))


def on_row(world, **fields) -> Connection:
    return sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=True, **fields)


def statement_says(conn, **over) -> Connection:
    """`conn`'s stored statement, as an owner's earlier share left it, without the crypto."""
    conn.statement = {**(conn.statement or {}), **over}
    conn.save(update_fields=["statement"])
    return conn


def kms_outage():
    """Every read of canopy-web's key fails, and nothing is cached."""
    down = server_key.Unavailable("down")
    return mock.patch.multiple(server_key, public_jwk=mock.Mock(side_effect=down),
                               cached_public_jwk=mock.Mock(return_value=None))


# ---- connecting ---------------------------------------------------------------------

def test_provider_defaults():
    assert (SalesforceProvider.server_use_default, SalesforceProvider.server_calls) == (True, True)
    assert (GoogleProvider.server_use_default, GoogleProvider.server_calls) == (False, False)


def test_new_salesforce_connection_seals_to_owner_and_canopy_web(world, key):
    with FakeSF():
        conn = connect(world)
    conn.refresh_from_db()
    assert conn.server_use is True and conn.state == Connection.UNSHARED and conn.sealed_sig is None
    assert zk_seal.sealed_targets(conn.sealed) == {owner_target(world), canopy_target()}
    assert owner_opens(world, conn)["refresh_token"] == FAKE_REFRESH
    # canopy-web opens its own entry through the derive KMS answers.
    plain = zk_seal.open_for(conn.sealed, agent_id=conn.binding, name="refresh", kv=0, kind="device",
                             target=server_key.thumbprint(), derive=server_key.derive)
    assert json.loads(plain)["refresh_token"] == FAKE_REFRESH


def test_new_google_connection_seals_to_owner_only(world, key):
    services.configure_app(world.ws, "google", client_id="123.apps.googleusercontent.com", client_secret="GOCSPX-s",
                           scopes=[f"{FakeGoogle.G}gmail.readonly"], by=world.person)
    with FakeGoogle():
        conn = connect(world, provider="google")
    assert conn.server_use is False
    assert zk_seal.sealed_targets(conn.sealed) == {owner_target(world)}


def test_new_google_connection_never_asks_kms(world, kms_down):
    services.configure_app(world.ws, "google", client_id="123.apps.googleusercontent.com", client_secret="GOCSPX-s",
                           scopes=[f"{FakeGoogle.G}gmail.readonly"], by=world.person)
    with FakeGoogle():
        connect(world, provider="google")
    kms_down.get_public_key.assert_not_called()


def test_no_key_configured_seals_to_owner_only(world):
    assert not server_key.available()
    with FakeSF():
        conn = connect(world)
    assert conn.server_use is False
    assert zk_seal.sealed_targets(conn.sealed) == {owner_target(world)}


def test_kms_failure_at_connect_still_connects_owner_only(world, kms_down, caplog):
    with FakeSF(), caplog.at_level(logging.WARNING, logger="apps.connections.services"):
        conn = connect(world)
    conn.refresh_from_db()
    assert conn.server_use is False and conn.state == Connection.UNSHARED
    assert zk_seal.sealed_targets(conn.sealed) == {owner_target(world)}
    assert owner_opens(world, conn)["refresh_token"] == FAKE_REFRESH  # the refresh token is not lost
    assert "connections.server_key_unavailable" in caplog.text
    assert FAKE_REFRESH not in caplog.text


def test_kms_failure_at_reconnect_keeps_the_owners_on_and_seals_to_owner_only(world, kms_down):
    conn = on_row(world)
    with FakeSF():
        again = connect(world)
    assert again.pk == conn.pk and again.server_use is True  # the owner's choice stands
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world)}
    assert owner_opens(world, again)["refresh_token"] == FAKE_REFRESH


def test_connecting_reads_the_key_before_the_row_is_locked(world, key, monkeypatch):
    real, in_upsert, read_inside = services._upsert_connection, [], []

    def upsert(*args):
        in_upsert.append(True)
        try:
            return real(*args)
        finally:
            in_upsert.pop()

    real_jwk = server_key.public_jwk

    def jwk():
        read_inside.append(bool(in_upsert))
        return real_jwk()

    monkeypatch.setattr(services, "_upsert_connection", upsert)
    monkeypatch.setattr(server_key, "public_jwk", jwk)
    with FakeSF():
        connect(world)
    assert read_inside and not any(read_inside)


# ---- reconnecting -------------------------------------------------------------------

def test_reconnect_keeps_off(world, key):
    conn = sealed_to_owner({"user": world.person}, world.person, app=world.app)
    assert conn.server_use is False
    with FakeSF():
        again = connect(world)
    again.refresh_from_db()
    assert again.pk == conn.pk and again.server_use is False  # never the provider's default
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world)}


def test_reconnect_keeps_on_and_goes_unshared(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    assert conn.state == Connection.ACTIVE
    with FakeSF():
        again = connect(world)
    again.refresh_from_db()
    assert again.pk == conn.pk and again.server_use is True
    assert again.state == Connection.UNSHARED and again.sealed_sig is None and again.access_sealed == ""
    # canopy-web can use the new token at once; machines stop until the owner shares again.
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world), canopy_target()}
    assert again.statement is not None  # kept, and it decides nothing now


def test_reconnect_of_an_on_row_with_no_statement_seals_to_canopy_web(world, key):
    conn = on_row(world)
    assert conn.statement is None
    with FakeSF():
        again = connect(world)
    again.refresh_from_db()
    assert again.pk == conn.pk and again.server_use is True and again.state == Connection.UNSHARED
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world), canopy_target()}


def test_reconnect_of_an_off_row_with_a_statement_stays_off(world, key):
    conn = world.shared(world.persons(), agents=[world.eva])
    assert conn.statement is not None and conn.server_use is False
    with FakeSF():
        again = connect(world)
    again.refresh_from_db()
    assert again.pk == conn.pk and again.server_use is False and again.state == Connection.UNSHARED
    assert again.statement is not None
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world)}


def test_reconnect_of_an_on_row_whose_statement_omits_canopy_web_still_seals_to_it(world, key):
    conn = world.shared(on_row(world), agents=[world.eva])
    # Sharing without canopy-web switches it off; a row switched on after that is what a reconnect meets.
    Connection.objects.filter(pk=conn.pk).update(server_use=True)
    conn.refresh_from_db()
    assert "canopy_web" not in conn.statement and conn.server_use is True
    with FakeSF():
        again = connect(world)
    assert again.server_use is True
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world), canopy_target()}


def test_reconnect_of_an_on_row_whose_statement_names_another_key_still_seals_to_it(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    statement_says(conn, canopy_web=OTHER_KEY)
    with FakeSF():
        again = connect(world)
    assert canopy_target() in zk_seal.sealed_targets(again.sealed)


# ---- the rule -----------------------------------------------------------------------

def test_off_row_has_no_server_recipient_whatever_the_statement_says(world, key):
    conn = world.persons()
    assert rcpt.server_recipient(conn) is None
    statement_says(conn, canopy_web=server_key.thumbprint())
    assert rcpt.server_recipient(conn) is None


def test_no_key_means_no_server_recipient(world):
    assert rcpt.server_recipient(on_row(world)) is None


def test_recipients_include_canopy_web_with_its_public_key(world, key):
    got = rcpt.recipients(on_row(world))
    assert ("device", server_key.thumbprint(), server_key.public_jwk()) in got
    assert owner_target(world) in rcpt.targets(got)


def test_unshared_row_ignores_old_statement(world, key):
    conn = on_row(world)
    statement_says(conn, agents=[], my_computers=[])  # names no canopy-web
    assert rcpt.server_recipient(conn)[1] == server_key.thumbprint()
    statement_says(conn, canopy_web=OTHER_KEY)  # names another key
    assert rcpt.server_recipient(conn)[1] == server_key.thumbprint()


def test_a_statement_being_shared_decides_even_on_an_unshared_row(world, key):
    """The new statement is what will be in force: an envelope can't reach canopy-web
    if the statement it comes with leaves it out."""
    conn = on_row(world)
    assert rcpt.server_recipient(conn, {"agents": []}) is None
    assert rcpt.server_recipient(conn, {"agents": [], "canopy_web": OTHER_KEY}) is None
    named = {"agents": [], "canopy_web": server_key.thumbprint()}
    assert rcpt.server_recipient(conn, named)[1] == server_key.thumbprint()


def test_active_row_needs_the_current_key_named(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    assert rcpt.server_recipient(conn)[1] == server_key.thumbprint()
    statement_says(conn, canopy_web=OTHER_KEY)
    assert rcpt.server_recipient(conn) is None


# ---- pruning ------------------------------------------------------------------------

def test_prune_keeps_canopy_web_when_named(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    assert canopy_target() in zk_seal.sealed_targets(conn.sealed)
    assert rcpt.prune(conn) == []
    conn.refresh_from_db()
    assert canopy_target() in zk_seal.sealed_targets(conn.sealed)
    assert canopy_target() in {(m["kind"], m["target"]) for m in conn.sealed_sig["macs"]}


def test_active_row_without_canopy_web_in_statement_drops_it(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    conn.statement.pop("canopy_web")
    conn.save(update_fields=["statement"])
    target = canopy_target()
    assert rcpt.prune(conn)
    conn.refresh_from_db()
    assert target not in zk_seal.sealed_targets(conn.sealed)
    assert target not in {(m["kind"], m["target"]) for m in conn.sealed_sig["macs"]}
    assert owner_target(world) in zk_seal.sealed_targets(conn.sealed)


def test_a_kms_outage_never_prunes_canopy_web(world, key):
    """Only the owner's statement drops it: an outage says nothing about what they chose."""
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    target = canopy_target()
    with kms_outage():
        assert rcpt.prune(conn) == []
    conn.refresh_from_db()
    assert target in zk_seal.sealed_targets(conn.sealed)


def test_prune_keeps_named_canopy_web_when_key_unset(world, key):
    """A deployment that lost the setting says nothing about what the owner chose."""
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    target = canopy_target()
    server_key.use_software_key_for_tests(None)
    assert not server_key.available()
    assert rcpt.server_recipient(conn) is None  # it behaves as off
    assert rcpt.prune(conn) == []
    conn.refresh_from_db()
    assert target in zk_seal.sealed_targets(conn.sealed)


def test_wrong_key_prunes_named_canopy_web(world, key):
    """The key can be read and it isn't the one the owner named: that is not an outage."""
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    target = canopy_target()
    server_key.use_software_key_for_tests(ec.generate_private_key(ec.SECP256R1()))
    assert rcpt.prune(conn)
    conn.refresh_from_db()
    assert target not in zk_seal.sealed_targets(conn.sealed)


def test_prune_reads_the_key_before_it_takes_the_row_lock(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    calls = mock.Mock()
    real_jwk, real_lock = server_key.public_jwk, Connection.objects.select_for_update

    def jwk():
        calls.jwk()
        return real_jwk()

    def lock(*a, **kw):
        calls.lock()
        return real_lock(*a, **kw)

    with mock.patch.object(server_key, "public_jwk", jwk), \
            mock.patch.object(Connection.objects, "select_for_update", lock):
        rcpt.prune(conn)
    assert [c[0] for c in calls.mock_calls][:2] == ["jwk", "lock"]


# ---- canopy-web has no RunnerDeviceKey: every reader of the recipients ------------------

def test_writer_provable_when_the_statement_names_canopy_web(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    by_canopy = {"by": {"kind": "device", "target": server_key.thumbprint()}}
    assert rcpt._writer_provable(conn, by_canopy) is True
    conn.statement.pop("canopy_web")
    assert rcpt._writer_provable(conn, by_canopy) is False


def test_writer_proof_asks_no_kms(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    by_canopy = {"by": {"kind": "device", "target": server_key.thumbprint()}}
    with mock.patch.object(server_key, "public_jwk", side_effect=server_key.Unavailable("down")):
        assert rcpt._writer_provable(conn, by_canopy) is True


def test_canopy_web_is_no_machine_and_no_revoker(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    jkt = server_key.thumbprint()
    assert ("device", jkt) in zk_seal.sealed_targets(conn.sealed)
    assert jkt not in {k.ecdh_jkt for k in rcpt.holders(conn)}
    assert jkt not in {w["target"] for w in rcpt.waiting(conn)}
    assert jkt not in {k.ecdh_jkt for k, _ in rcpt.device_reasons(conn)}
    assert rcpt._revokers([jkt]) == []
    assert jkt not in rcpt.revokers(conn)
    assert rcpt._last_holder(set(), {("device", jkt)}) is None


def test_an_envelope_held_only_by_canopy_web_and_the_owner_is_revoked_by_canopy_web(world, key):
    with FakeSF() as sf:
        conn = connect(world)
        assert rcpt.revokers(conn) == []
        assert services.disconnect(conn, by=world.person) == "revoked"
    assert sf.calls[-1][1].endswith("/services/oauth2/revoke")


def test_keeper_work_keeps_canopy_web_and_never_waits_for_it(world, key):
    conn = sealed_to_owner({"agent": world.eva}, world.person, app=world.app, server_use=True)
    to = [r for r in rcpt.recipients(conn, {"agents": [str(world.eva.pk)], "my_computers": [],
                                            "canopy_web": server_key.thumbprint()})
          if r[0] == "device" and r[1] != world.ada_laptop.ecdh_jkt]
    world.shared(conn, agents=[world.eva], canopy_web=server_key.thumbprint(), to=to)
    [work] = keeper.work(world.eva_box.runner)
    assert work["waiting"] == [world.ada_laptop.ecdh_jkt]
    assert server_key.thumbprint() in {r["target"] for r in work["recipients"]}


def test_material_lists_canopy_web_so_a_machine_keeps_it(world, key):
    """A machine seals only to those this list names, and a write-back that drops a
    recipient still allowed is refused: leaving canopy-web out would stop every refresh."""
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    rows = {r["target"]: r for r in tokens.material_fields(conn, world.eva_box)["recipients"]}
    row = rows[server_key.thumbprint()]
    assert (row["kind"], row["public_jwk"], row["runner_name"]) == ("device", server_key.public_jwk(), "canopy-web")


def test_material_does_not_list_canopy_web_when_the_statement_omits_it(world, key):
    conn = world.shared(on_row(world), agents=[world.eva])
    rows = tokens.material_fields(conn, world.eva_box)["recipients"]
    assert server_key.thumbprint() not in {r["target"] for r in rows}


# ---- a machine writing back ------------------------------------------------------------

@pytest.fixture
def machines(key):
    w = TokensWorld()
    w.conn = w.shared(on_row(w), agents=[w.eva], canopy_web=server_key.thumbprint())
    return w


def test_a_machine_write_reaching_canopy_web_is_stored(machines):
    w = machines
    lid = lease(w, w.box_a).json()["lease_id"]
    assert write_back(w, w.box_a, machine_write(w, w.conn, w.box_a, lid)).status_code == 200
    w.conn.refresh_from_db()
    assert canopy_target() in zk_seal.sealed_targets(w.conn.access_sealed)


def test_a_machine_write_leaving_canopy_web_out_is_refused(machines):
    w = machines
    lid = lease(w, w.box_a).json()["lease_id"]
    to = [r for r in rcpt.recipients(w.conn) if r[1] != server_key.thumbprint()]
    r = write_back(w, w.box_a, machine_write(w, w.conn, w.box_a, lid, to=to))
    assert r.status_code == 422 and r.json()["error"] == "zk_rewrap_required"


def test_a_kms_outage_refuses_no_machine_write(machines):
    """Another web worker may have shown canopy-web's key, or none could: this machine's new
    token may be the only copy, so neither write is refused for it."""
    w = machines
    lid = lease(w, w.box_a).json()["lease_id"]
    reaching = machine_write(w, w.conn, w.box_a, lid)
    without = [r for r in rcpt.recipients(w.conn) if r[1] != server_key.thumbprint()]
    with kms_outage():
        assert rcpt.server_recipient(w.conn) is None
        rows = tokens.material_fields(w.conn, w.machines[w.box_a][0])["recipients"]
        assert "canopy-web" not in {r["runner_name"] for r in rows}
        assert write_back(w, w.box_a, reaching).status_code == 200
        lid = lease(w, w.box_a, w.conn, lid).json()["lease_id"]
        leaving_out = machine_write(w, w.conn, w.box_a, lid, to=without)
        assert write_back(w, w.box_a, leaving_out).status_code == 200


# ---- under a row lock, the cached key or nothing ------------------------------------------

@pytest.fixture
def cold_kms(machines):
    """The key is configured and nothing is cached. `warm_server_key` does nothing, as when it
    read a stale `server_use` or its failure memo lapsed during the lock wait, so the code under
    the lock is on its own. KMS would answer, but must not be asked."""
    client = mock.Mock()
    server_key.use_software_key_for_tests(None)
    with override_settings(CONNECTIONS_SERVER_KMS_KEY="alias/canopy-web-connections"), \
            mock.patch.object(server_key, "_client", return_value=client), \
            mock.patch.object(rcpt, "warm_server_key"):
        yield client
    server_key._reset()


def test_a_cold_cache_under_a_lock_asks_no_kms_in_the_rule(machines, cold_kms):
    assert rcpt.server_recipient(machines.conn, cached_only=True) is None
    assert rcpt.unconfirmed_server_target(machines.conn) == {("device", machines.conn.statement["canopy_web"])}
    cold_kms.get_public_key.assert_not_called()


def test_a_cold_cache_in_prune_asks_no_kms_and_keeps_canopy_web(machines, cold_kms):
    before = zk_seal.sealed_targets(machines.conn.sealed)
    assert rcpt.prune(machines.conn) == []
    machines.conn.refresh_from_db()
    assert zk_seal.sealed_targets(machines.conn.sealed) == before
    cold_kms.get_public_key.assert_not_called()


def test_a_cold_cache_in_write_back_asks_no_kms(machines, key):
    w = machines
    lid = lease(w, w.box_a).json()["lease_id"]
    body = machine_write(w, w.conn, w.box_a, lid)
    client = mock.Mock()
    server_key.use_software_key_for_tests(None)
    with override_settings(CONNECTIONS_SERVER_KMS_KEY="alias/k"), \
            mock.patch.object(server_key, "_client", return_value=client), \
            mock.patch.object(rcpt, "warm_server_key"):
        machine_key = w.machines[w.box_a][0]
        tokens.write_back(w.conn, device_key=machine_key, lease_id=lid, access_sealed=body["access_sealed"],
                          access_sig=body["access_sig"], access_expires_at=timezone.now(), expect=body["expect"],
                          rotated=False)
    client.get_public_key.assert_not_called()
    server_key._reset()


def test_a_cold_cache_in_reseal_asks_no_kms(machines, key):
    w = machines
    body = machine_write(w, w.conn, w.box_a, "unused", rotated=True)
    client = mock.Mock()
    server_key.use_software_key_for_tests(None)
    with override_settings(CONNECTIONS_SERVER_KMS_KEY="alias/k"), \
            mock.patch.object(server_key, "_client", return_value=client), \
            mock.patch.object(rcpt, "warm_server_key"):
        machine_key = w.machines[w.box_a][0]
        keeper.reseal(w.conn, machine_key.runner, machine_key, sealed=body["sealed"], sealed_sig=body["sealed_sig"],
                      expect=w.conn.sealed_digest)
    client.get_public_key.assert_not_called()
    server_key._reset()


def test_a_cold_cache_in_share_asks_no_kms_and_says_try_again(world, key):
    conn = on_row(world)
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva],
                         canopy_web=server_key.thumbprint())
    client = mock.Mock()
    server_key.use_software_key_for_tests(None)
    with override_settings(CONNECTIONS_SERVER_KMS_KEY="alias/k"), \
            mock.patch.object(server_key, "_client", return_value=client), \
            mock.patch.object(rcpt, "warm_server_key"), pytest.raises(WireError) as e:
        sharing.share(conn, user=world.person, **body)
    assert (e.value.status, e.value.code) == (503, "try_again")
    client.get_public_key.assert_not_called()
    server_key._reset()


# ---- a lend with limits is never canopy-web's -----------------------------------------

LIMITS = [{"lend_minutes": 60}, {"owner_turns_only": True}, {"lend_minutes": 60, "owner_turns_only": True}]


@pytest.mark.parametrize("limits", LIMITS)
def test_a_new_limited_row_is_sealed_to_its_owner_only_whatever_the_provider_says(world, key, limits):
    assert SalesforceProvider.server_use_default is True
    with FakeSF():
        conn = connect(world, **limits)
    conn.refresh_from_db()
    assert conn.limited and conn.server_use is False
    assert zk_seal.sealed_targets(conn.sealed) == {owner_target(world)}
    assert rcpt.server_reachable(conn) is False


def test_a_limited_connect_never_asks_kms(world, kms_down):
    with FakeSF():
        connect(world, owner_turns_only=True)
    kms_down.get_public_key.assert_not_called()


@pytest.mark.parametrize("limits", LIMITS)
def test_a_limited_reconnect_of_an_on_row_seals_without_canopy_web_and_turns_it_off(world, key, caplog, limits):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    assert ("device", server_key.thumbprint()) in zk_seal.sealed_targets(conn.sealed)
    with caplog.at_level(logging.INFO, logger="apps.connections.services"), FakeSF():
        again = connect(world, **limits)
    again.refresh_from_db()
    assert again.pk == conn.pk and again.limited and again.server_use is False
    assert again.state == Connection.UNSHARED and again.access_sealed == ""
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world)}
    assert rcpt.server_recipient(again) is None and rcpt.server_reachable(again) is False
    assert switch_lines(caplog) == ["server_use_off"]


def test_a_reconnect_naming_no_limits_keeps_on_and_records_no_switch(world, key, caplog):
    on_row(world)
    with caplog.at_level(logging.INFO, logger="apps.connections.services"), FakeSF():
        again = connect(world)
    again.refresh_from_db()
    assert again.server_use is True and not again.limited
    assert switch_lines(caplog) == []


def test_a_reconnect_lifting_the_limits_leaves_the_switch_off(world, key):
    with FakeSF():
        connect(world, lend_minutes=60)
        again = connect(world)
    again.refresh_from_db()
    assert not again.limited and again.server_use is False  # turning it on is still the owner's, on Share
    assert zk_seal.sealed_targets(again.sealed) == {owner_target(world)}


@pytest.mark.parametrize("limit", [{"owner_turns_only": True},
                                   {"expires_at": timezone.now() + timedelta(hours=1)}])
def test_sharing_a_limited_row_with_canopy_web_is_refused(world, key, limit):
    conn = sealed_to_owner({"user": world.person}, world.person, app=world.app, **limit)
    e = refused(conn, world.person, "bad_canopy_web",
                **browser_share(conn, world.person, world.person_priv, agents=[world.eva], to=[],
                                canopy_web=server_key.thumbprint()))
    assert e.status == 422
    conn.refresh_from_db()
    assert conn.state == Connection.UNSHARED and conn.server_use is False
    # Without canopy-web it shares as any other.
    sharing.share(conn, user=world.person,
                  **browser_share(conn, world.person, world.person_priv, agents=[world.eva], to=[]))
    conn.refresh_from_db()
    assert conn.state == Connection.ACTIVE and conn.server_use is False


def test_access_token_on_a_limited_row_is_server_use_off(world, key):
    with FakeSF():
        conn = connect(world, owner_turns_only=True)
    with pytest.raises(server_tokens.ServerUseOff):
        server_tokens.access_token(conn)
    # Even with the switch and the envelope saying otherwise: the limits win.
    forced = on_row(world, account_id="acct-forced", expires_at=timezone.now() + timedelta(hours=1))
    with pytest.raises(server_tokens.ServerUseOff):
        server_tokens.access_token(forced)


def test_a_limited_row_shows_no_switch_and_no_canopy_web_box(world, key):
    with FakeSF():
        conn = connect(world, lend_minutes=60, owner_turns_only=True)
    state = sharing.share_state(conn, world.person)
    assert state["server"]["provider_calls"] is False and state["server"]["on"] is False
    assert state["owner_turns_only"] is True and state["expires_at"] == conn.expires_at
    row = _client(world.person).get("/api/connections/").json()["connections"][0]
    assert row["provider_calls"] is False and row["server_use"] is False and row["owner_turns_only"] is True


def test_a_lapsed_row_sealed_to_canopy_web_is_revoked_by_the_sweep_not_the_disconnect(world, key):
    conn = _sf_unshared_on(world)
    Connection.objects.filter(pk=conn.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    with FakeSF() as sf, mock.patch.object(services, "disconnect", wraps=services.disconnect) as disconnect, \
            mock.patch.object(server_tokens, "try_revoke", wraps=server_tokens.try_revoke) as revoke:
        out = keeper.sweep()
    assert disconnect.call_args.kwargs == {"by": None, "revoke_now": False, "lapsed": True}
    assert revoke.call_count == 1 and revoke.call_args.kwargs == {"stop_on_kms": True}  # the sweep's own try
    assert out["lapsed"] == 1 and out["revoke_unconfirmed"] == 0
    assert sf.calls[-1][1].endswith("/services/oauth2/revoke")
    assert not Connection.objects.filter(pk=conn.pk).exists()


def test_a_lapsed_row_whose_revoke_fails_is_retried_by_later_sweeps(world, key):
    conn = _sf_unshared_on(world)
    Connection.objects.filter(pk=conn.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    with FakeSF(revoke=_resp(status=503)):
        assert keeper.sweep()["lapsed"] == 1
    conn.refresh_from_db()
    assert conn.state == Connection.REVOKING
    cache.delete(f"connections:revoke-attempt:{conn.pk}")  # an hour later
    with FakeSF() as sf:
        assert keeper.sweep()["lapsed"] == 0  # already `revoking`: not disconnected twice
    assert sf.calls[-1][1].endswith("/services/oauth2/revoke")
    assert not Connection.objects.filter(pk=conn.pk).exists()


# ---- the switch: share ---------------------------------------------------------------

def _sf_unshared_on(world):
    with FakeSF():
        conn = connect(world)
    conn.refresh_from_db()
    return conn


def test_share_with_wrong_thumbprint_refused(world, key):
    conn = on_row(world)
    e = refused(conn, world.person, "bad_canopy_web",
                **browser_share(conn, world.person, world.person_priv, agents=[world.eva], canopy_web=OTHER_KEY))
    assert e.status == 422
    assert Connection.objects.get(pk=conn.pk).state == "unshared"


def test_share_with_canopy_web_while_statements_disabled_refused(world, key):
    conn = on_row(world)
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva],
                         canopy_web=server_key.thumbprint())
    with override_settings(CONNECTIONS_SERVER_STATEMENTS=False):
        e = refused(conn, world.person, "bad_canopy_web", **body)
    assert e.status == 422


def test_share_naming_canopy_web_with_no_key_configured_refused(world):
    conn = on_row(world)
    refused(conn, world.person, "bad_canopy_web",
            **browser_share(conn, world.person, world.person_priv, agents=[world.eva], canopy_web=OTHER_KEY,
                            to=[]))


def switch_lines(caplog):
    return [r.getMessage().split()[0].removeprefix("connections.") for r in caplog.records
            if r.name == "apps.connections.services" and r.getMessage().startswith("connections.server_use_")]


def test_share_sets_server_use_both_ways(world, key, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    # An off row naming canopy-web turns on: its entry is allowed, not `foreign_reader`.
    conn = sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=False)
    here = ("device", server_key.thumbprint(), server_key.public_jwk())  # the browser seals to the key it was shown
    conn = world.shared(conn, agents=[world.eva], canopy_web=server_key.thumbprint(), to=[here])
    conn.refresh_from_db()
    assert conn.server_use is True and canopy_target() in zk_seal.sealed_targets(conn.sealed)
    # The next statement omits it: the switch goes off and canopy-web is pruned.
    conn = world.shared(conn, agents=[world.eva])
    conn.refresh_from_db()
    assert conn.server_use is False and canopy_target() not in zk_seal.sealed_targets(conn.sealed)
    assert switch_lines(caplog) == ["server_use_on", "server_use_off"]


def test_share_that_keeps_the_switch_records_no_change(world, key, caplog):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    world.shared(conn, agents=[world.eva], canopy_web=server_key.thumbprint())
    assert switch_lines(caplog) == []


def test_share_naming_only_canopy_web_is_a_valid_first_share(world, key):
    conn = on_row(world)
    conn = world.shared(conn, canopy_web=server_key.thumbprint())
    conn.refresh_from_db()
    assert conn.state == "active" and conn.server_use is True
    assert zk_seal.sealed_targets(conn.sealed) == {owner_target(world), canopy_target()}


def test_share_route_answers_try_again_as_503(world, key):
    conn = on_row(world)
    body = browser_share(conn, world.person, world.person_priv, agents=[world.eva],
                         canopy_web=server_key.thumbprint())
    with mock.patch.object(server_key, "cached_public_jwk", return_value=None), \
            mock.patch.object(rcpt, "warm_server_key"), mock.patch.object(server_key, "public_jwk",
                                                                          side_effect=server_key.Unavailable("x")):
        r = _client(world.person, step_up=True).put(f"/api/connections/{conn.pk}/share", body,
                                                    content_type="application/json")
    assert r.status_code == 503 and r.json()["error"] == "try_again"
    assert "forged" not in r.json()["detail"]


# ---- the switch: share_state and the list --------------------------------------------

def test_share_state_describes_canopy_web(world, key):
    conn = _sf_unshared_on(world)
    s = sharing.share_state(conn, world.person)["server"]
    assert s == {"available": True, "thumbprint": server_key.thumbprint(), "public_jwk": server_key.public_jwk(),
                 "on": True, "statements_enabled": True, "provider_calls": True, "reachable": True}


def test_share_state_without_a_key_has_no_thumbprint(world):
    s = sharing.share_state(world.persons(), world.person)["server"]
    assert s["available"] is False and s["thumbprint"] == "" and s["public_jwk"] is None
    assert s["on"] is False and s["reachable"] is False


def test_share_state_survives_a_kms_failure(world, kms_down):
    s = sharing.share_state(world.persons(), world.person)["server"]
    assert s["available"] is False and s["thumbprint"] == "" and s["public_jwk"] is None


def test_share_state_says_statements_disabled_and_google_has_no_calls(world, key):
    conn = world.persons()
    with override_settings(CONNECTIONS_SERVER_STATEMENTS=False):
        assert sharing.share_state(conn, world.person)["server"]["statements_enabled"] is False
    assert sharing.share_state(conn, world.person)["server"]["provider_calls"] is True


def test_connection_out_says_whether_canopy_web_can_use_it(world, key):
    conn = _sf_unshared_on(world)
    row = _client(world.person).get("/api/connections/").json()["connections"][0]
    assert row["server_use"] is True and row["server_reachable"] is True
    # A reconnect during a KMS outage leaves the switch on and canopy-web outside the envelope.
    conn.sealed = zk_seal.keep_only(conn.sealed, {owner_target(world)})[0]
    conn.save()
    row = _client(world.person).get("/api/connections/").json()["connections"][0]
    assert row["server_use"] is True and row["server_reachable"] is False


def test_connection_out_without_a_key_is_not_reachable(world):
    world.persons()
    row = _client(world.person).get("/api/connections/").json()["connections"][0]
    assert row["server_use"] is False and row["server_reachable"] is False


def test_no_key_lists_no_switch_for_a_provider_canopy_web_calls(world):
    """After a deploy with no key, a switch's Turn on would lead to a Share screen with no box."""
    sealed_to_owner({"user": world.person}, world.person, app=world.app)
    row = _client(world.person).get("/api/connections/").json()["connections"][0]
    assert row["provider"] == "salesforce" and row["provider_calls"] is False and row["server_use"] is False


def test_no_key_still_lists_the_switch_of_a_row_that_is_on(world):
    """The key was taken away: its owner can still see and turn off what is on."""
    on_row(world)
    row = _client(world.person).get("/api/connections/").json()["connections"][0]
    assert row["provider_calls"] is True and row["server_reachable"] is False


def test_a_key_lists_the_switch_on_an_off_row(world, key):
    sealed_to_owner({"user": world.person}, world.person, app=world.app)
    row = _client(world.person).get("/api/connections/").json()["connections"][0]
    assert row["provider_calls"] is True and row["server_use"] is False


def _key_unreadable():
    return (mock.patch.object(server_key, "cached_public_jwk", return_value=None),
            mock.patch.object(server_key, "public_jwk", side_effect=server_key.Unavailable("down")))


def test_a_key_that_cannot_be_read_is_unknown_not_unreachable(world, key):
    conn = _sf_unshared_on(world)
    no_cache, down = _key_unreadable()
    with no_cache, down:
        assert rcpt.server_reachable(conn) is None
        row = _client(world.person).get("/api/connections/").json()["connections"][0]
        assert row["server_use"] is True and row["server_reachable"] is None
        assert sharing.share_state(conn, world.person)["server"]["reachable"] is None


def test_an_off_row_is_not_reachable_whatever_the_key_is_doing(world, key):
    conn = sealed_to_owner({"user": world.person}, world.person, app=world.app)
    no_cache, down = _key_unreadable()
    with no_cache, down:
        assert rcpt.server_reachable(conn) is False


# ---- the switch: turning it off ------------------------------------------------------

def _client(user, *, step_up=False):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c


def _off(user, conn, **kw):
    return _client(user, **kw).post(f"/api/connections/{conn.pk}/server-use", {"on": False},
                                    content_type="application/json")


def test_turn_off_unshared_prunes_both_envelopes_no_passkey(world, key, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    conn = _sf_unshared_on(world)
    # A server-written access envelope too, sealed to both.
    conn.access_sealed = zk_seal.seal_to(json.dumps({"access_token": "x"}), agent_id=conn.binding, name="access",
                                         kv=0, recipients=[r for r in rcpt.recipients(conn)])
    conn.access_sig = {"v": 1, "by": {"kind": "device", "target": server_key.thumbprint()},
                       "expect": "", "macs": [{"kind": "member", "target": str(world.person.pk), "mac": "m"},
                                              {"kind": "device", "target": server_key.thumbprint(), "mac": "d"}]}
    conn.sealed_sig = {"v": 1, "by": {"kind": "device", "target": server_key.thumbprint()}, "expect": "",
                       "macs": [{"kind": "member", "target": str(world.person.pk), "mac": "m"},
                                {"kind": "device", "target": server_key.thumbprint(), "mac": "d"}]}
    conn.access_expires_at = timezone.now()
    conn.save()
    r = _off(world.person, conn)  # no step-up
    assert r.status_code == 200, r.json()
    assert r.json()["server_use"] is False and r.json()["server_reachable"] is False
    conn.refresh_from_db()
    assert conn.server_use is False and conn.state == "unshared"
    assert zk_seal.sealed_targets(conn.sealed) == {owner_target(world)}
    assert conn.sealed_digest == vault_engine.value_digest(conn.sealed)
    assert [m["target"] for m in conn.sealed_sig["macs"]] == [str(world.person.pk)]
    assert zk_seal.sealed_targets(conn.access_sealed) == {owner_target(world)}
    assert [m["target"] for m in conn.access_sig["macs"]] == [str(world.person.pk)]
    assert owner_opens(world, conn)["refresh_token"] == FAKE_REFRESH
    assert switch_lines(caplog) == ["server_use_off"]


def test_turn_off_drops_an_access_envelope_only_canopy_web_could_open(world, key):
    conn = _sf_unshared_on(world)
    conn.access_sealed = zk_seal.seal_to("{}", agent_id=conn.binding, name="access", kv=0,
                                         recipients=[r for r in rcpt.recipients(conn) if r[0] == "device"])
    conn.access_sig, conn.access_expires_at = {"v": 1, "macs": [], "by": {}, "expect": ""}, timezone.now()
    conn.save()
    assert _off(world.person, conn).status_code == 200
    conn.refresh_from_db()
    assert (conn.access_sealed, conn.access_sig, conn.access_expires_at) == ("", None, None)


def test_turn_off_is_idempotent(world, key, caplog):
    conn = _sf_unshared_on(world)
    assert _off(world.person, conn).status_code == 200
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    before = Connection.objects.get(pk=conn.pk).sealed
    r = _off(world.person, conn)
    assert r.status_code == 200 and r.json()["server_use"] is False
    assert Connection.objects.get(pk=conn.pk).sealed == before
    assert switch_lines(caplog) == []


def test_turn_off_active_is_needs_share(world, key):
    conn = world.shared(on_row(world), agents=[world.eva], canopy_web=server_key.thumbprint())
    r = _off(world.person, conn)
    assert r.status_code == 409 and r.json()["error"] == "needs_share"
    conn.refresh_from_db()
    assert conn.server_use is True and canopy_target() in zk_seal.sealed_targets(conn.sealed)


def test_turn_on_is_refused(world, key):
    conn = sealed_to_owner({"user": world.person}, world.person, app=world.app)
    r = _client(world.person).post(f"/api/connections/{conn.pk}/server-use", {"on": True},
                                   content_type="application/json")
    assert r.status_code == 422
    assert Connection.objects.get(pk=conn.pk).server_use is False


def test_someone_elses_connection_404(world, key):
    conn = _sf_unshared_on(world)
    assert _off(world.op, conn).status_code == 404
    assert Connection.objects.get(pk=conn.pk).server_use is True


def test_turn_off_an_agents_connection_as_its_admin(world, key):
    conn = sealed_to_owner({"agent": world.eva}, world.person, app=world.app, server_use=True)
    conn.sealed = zk_seal.seal_to(zk_ref.open_sealed_value(conn.sealed, conn.binding, "refresh", "member",
                                                           str(world.person.pk), world.person_priv),
                                  agent_id=conn.binding, name="refresh", kv=0,
                                  recipients=rcpt.recipients(conn))
    conn.save()
    assert canopy_target() in zk_seal.sealed_targets(conn.sealed)
    assert _off(world.person, conn).status_code == 200
    conn.refresh_from_db()
    assert canopy_target() not in zk_seal.sealed_targets(conn.sealed) and conn.server_use is False


def test_turn_off_while_revoking_is_refused(world, key):
    conn = _sf_unshared_on(world)
    Connection.objects.filter(pk=conn.pk).update(state="revoking")
    r = _off(world.person, conn)
    assert r.status_code == 409 and r.json()["error"] == "revoking"


def test_problems_have_plain_titles():
    from apps.connections.problems import _PROBLEMS
    for code in ("server_use_off", "waiting", "server_refused", "needs_share", "bad_canopy_web", "try_again"):
        assert code in _PROBLEMS and _PROBLEMS[code][1]
