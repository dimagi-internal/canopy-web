"""canopy-web opening, refreshing and revoking a sealed connection itself (`server_tokens`):
the spec's "canopy-web using a token" and "Disconnecting". The lease and compare-and-swap are the
machines' own (`tokens`); a machine reads what canopy-web writes through the reference, as the CLI
does. No KMS or provider call happens in a transaction, and no token reaches a log or an Event.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import connection
from django.utils import timezone

from apps.agents.credentials import vault_engine, zk, zk_seal
from apps.connections import keeper, providers, server_key, server_tokens, services, tokens, wire
from apps.connections import recipients as rcpt
from apps.connections.models import Connection
from apps.connections.providers.google import REVOKE_URL as GOOGLE_REVOKE
from apps.connections.providers.google import TOKEN_URL as GOOGLE_TOKEN
from apps.connections.providers.google import GoogleProvider
from apps.connections.providers.salesforce import SalesforceProvider
from apps.events.models import Event
from apps.harness.models import RunnerAssignment
from tests.fixtures.connection_calls import TokensWorld, reseal_body
from tests.fixtures.connections import (
    CLIENT_ID,
    FAKE_REFRESH,
    REVOKE_URL,
    FakeGoogle,
    FakeSF,
    _refresh_plain,
    _resp,
    _stored,
    _token_body,
    sealed_to_owner,
    sf_app,
)
from tests.fixtures.vault_zk import conn_ref
from tests.fixtures.vault_zk import ref as zk_ref

pytestmark = pytest.mark.django_db

ACCESS = "00D!stored-access"
INSTANCE = "https://dimagi.my.salesforce.com"
ACCESS_KEYS = {"v", "access_token", "expires_at", "instance_url", "account_id", "scope", "owner_key",
               "statement_floor"}
NO_ROTATION = {"token": [_resp(body=_token_body(access_token="00D!new", refresh_token=None))]}


@pytest.fixture(autouse=True)
def _empty_cache():
    server_tokens._forget_all()
    yield
    server_tokens._forget_all()


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


@pytest.fixture
def w(key):
    """eva's connection, shared with eva, the statement naming canopy-web; eva's boxes hold it."""
    world = TokensWorld()
    world.conn = world.shared(sealed_to_owner({"agent": world.eva}, world.person, app=world.app, server_use=True),
                              agents=[world.eva], canopy_web=server_key.thumbprint())
    return world


def me() -> tuple[str, str]:
    return ("device", server_key.thumbprint())


def never_sleep(_seconds):
    raise AssertionError("did not expect to wait")


def machine_writes_access(world, conn, *, token=ACCESS, life=1800, name=None, **over) -> Connection:
    """A machine's write of the access envelope, stored directly: what `tokens.write_back` keeps."""
    name = name or world.box_a
    device, _, ecdh = world.machines[name]
    conn.refresh_from_db()
    rs = rcpt.recipients(conn)
    expires = int(time.time()) + life
    plain = {"v": 1, "access_token": token, "expires_at": expires, "instance_url": INSTANCE,
             "account_id": conn.account_id, "scope": "api", "owner_key": conn.owner_key,
             "statement_floor": conn.statement_version, **over}
    value = zk_seal.seal_to(json.dumps(plain), agent_id=conn.binding, name="access", kv=0, recipients=rs)
    conn.access_sig = conn_ref.writer_sig(conn.binding, "access", vault_engine.value_digest(conn.access_sealed), value,
                                          ecdh, "device", device.ecdh_jkt,
                                          {(k, t): zk.public_key(j) for k, t, j in rs})
    conn.access_sealed = value
    conn.access_expires_at = timezone.now() + timedelta(seconds=life)
    conn.save(update_fields=["access_sealed", "access_sig", "access_expires_at"])
    return conn


def opened(world, conn, value, name) -> dict:
    return json.loads(zk_ref.open_sealed_value(value, conn.binding, name, "member", str(world.person.pk),
                                               world.person_priv))


def reseal_refresh(world, conn, **over) -> Connection:
    """The refresh envelope re-sealed by a box that holds it, with `over` in its plaintext."""
    device, _, ecdh = world.machines[world.box_a]
    rs = rcpt.recipients(conn)
    plain = {**json.loads(_refresh_plain(conn, statement_floor=conn.statement_version)), **over}
    value = zk_seal.seal_to(json.dumps(plain), agent_id=conn.binding, name="refresh", kv=0, recipients=rs)
    conn.sealed_sig = conn_ref.writer_sig(conn.binding, "refresh", conn.sealed_digest, value, ecdh, "device",
                                          device.ecdh_jkt, {(k, t): zk.public_key(j) for k, t, j in rs})
    conn.sealed, conn.sealed_digest = value, vault_engine.value_digest(value)
    conn.save(update_fields=["sealed", "sealed_sig", "sealed_digest"])
    return conn


def refused_access(conn) -> str:
    with pytest.raises(server_tokens.Refused) as e, FakeSF(), mock.patch.object(server_tokens, "_sleep", never_sleep):
        server_tokens.access_token(conn)
    return str(e.value)


# ---- the cache -----------------------------------------------------------------------

def test_opens_access_envelope(w):
    machine_writes_access(w, w.conn)
    with mock.patch.object(server_key, "derive", wraps=server_key.derive) as derive:
        assert server_tokens.access_token(w.conn) == (ACCESS, INSTANCE)
    assert derive.call_count == 2  # its own entry, and the writer's MAC


def test_uses_cached_token_without_kms(w):
    machine_writes_access(w, w.conn)
    server_tokens.access_token(w.conn)
    with mock.patch.object(server_key, "derive", side_effect=AssertionError("KMS was asked")), \
            mock.patch.object(server_key, "thumbprint", side_effect=AssertionError("KMS was asked")):
        assert server_tokens.access_token(w.conn) == (ACCESS, INSTANCE)


def test_cache_misses_when_digest_changes(w):
    machine_writes_access(w, w.conn)
    server_tokens.access_token(w.conn)
    machine_writes_access(w, w.conn, token="00D!other")
    assert server_tokens.access_token(w.conn)[0] == "00D!other"


def test_cache_lasts_five_minutes(w):
    machine_writes_access(w, w.conn)
    server_tokens.access_token(w.conn)
    later = time.time() + 301
    with mock.patch.object(server_tokens, "_now", return_value=later), \
            mock.patch.object(server_key, "derive", wraps=server_key.derive) as derive:
        server_tokens.access_token(w.conn)
    assert derive.called


def test_cache_never_outlives_the_token(w):
    machine_writes_access(w, w.conn, life=120)
    server_tokens.access_token(w.conn)
    assert server_tokens._cached(w.conn) is not None
    with mock.patch.object(server_tokens, "_now", return_value=time.time() + 90):
        assert server_tokens._cached(w.conn) is None  # 30 s left is under the minimum life


def test_cache_is_bounded(w):
    for i in range(server_tokens.CACHE_SIZE + 5):
        server_tokens._remember(f"id-{i}", "digest", "token", "", time.time() + 900)
    assert len(server_tokens._cache) == server_tokens.CACHE_SIZE
    assert "id-0" not in server_tokens._cache and f"id-{server_tokens.CACHE_SIZE + 4}" in server_tokens._cache


def test_the_cache_lock_is_never_held_across_a_derive(w):
    machine_writes_access(w, w.conn)
    real = server_key.derive

    def derive(jwk):
        assert not server_tokens._cache_lock.locked()
        return real(jwk)

    with mock.patch.object(server_key, "derive", derive):
        server_tokens.access_token(w.conn)


# ---- refreshing ----------------------------------------------------------------------

def test_refreshes_when_near_expiry_and_writes_back_with_macs(w):
    machine_writes_access(w, w.conn, life=30)
    digest = w.conn.sealed_digest
    with FakeSF(**NO_ROTATION) as sf:
        assert server_tokens.access_token(w.conn, sleep=never_sleep) == ("00D!new", INSTANCE)
    [(_, _, form)] = sf.token_calls()
    assert form == {"grant_type": "refresh_token", "refresh_token": FAKE_REFRESH, "client_id": CLIENT_ID,
                    "client_secret": "app-secret"}
    w.conn.refresh_from_db()
    assert w.conn.sealed_digest == digest  # no new refresh token: the old is kept, no `sealed`
    assert w.conn.lease_id is None and w.conn.lease_holder == ""
    # Everyone `recipients` names can check their own entry against the writer's key.
    assert zk_seal.sealed_targets(w.conn.access_sealed) == rcpt.targets(rcpt.recipients(w.conn))
    sig = w.conn.access_sig
    assert sig["by"] == {"kind": "device", "target": server_key.thumbprint(), "public_jwk": server_key.public_jwk()}
    entries = conn_ref.sealed_entries(w.conn.access_sealed)
    assert {(m["kind"], m["target"]) for m in sig["macs"]} == {(e["kind"], e["target"]) for e in entries}
    plain = opened(w, w.conn, w.conn.access_sealed, "access")
    assert set(plain) == ACCESS_KEYS and plain["v"] == 1 and plain["access_token"] == "00D!new"
    assert (plain["instance_url"], plain["account_id"], plain["owner_key"]) == (
        INSTANCE, w.conn.account_id, w.conn.owner_key)
    assert plain["scope"] == "api refresh_token" and plain["statement_floor"] == 1
    assert abs(w.conn.access_expires_at.timestamp() - plain["expires_at"]) < 1
    assert Event.objects.get(kind="connections.server_refreshed").payload["machine"] == "canopy-web"
    assert not Event.objects.filter(kind="connections.refreshed").exists()


def test_the_new_token_is_cached(w):
    with FakeSF(**NO_ROTATION) as sf:
        server_tokens.access_token(w.conn, sleep=never_sleep)
    with FakeSF() as sf, mock.patch.object(server_key, "derive", side_effect=AssertionError("KMS was asked")):
        assert server_tokens.access_token(w.conn)[0] == "00D!new"
    assert sf.calls == []


def test_a_days_refreshes_coalesce_into_one_event(w):
    for token in ("00D!one", "00D!two"):
        w.conn.refresh_from_db()
        Connection.objects.filter(pk=w.conn.pk).update(access_sealed="", access_sig=None, access_expires_at=None)
        server_tokens._forget_all()
        with FakeSF(token=[_resp(body=_token_body(access_token=token, refresh_token=None))]):
            assert server_tokens.access_token(w.conn, sleep=never_sleep)[0] == token
    [event] = Event.objects.filter(kind="connections.server_refreshed")
    assert event.count == 2


def test_rotated_refresh_token_is_resealed_with_same_floor_and_owner_key(w):
    before = opened(w, w.conn, w.conn.sealed, "refresh")
    assert before["refresh_token"] == FAKE_REFRESH
    old = w.conn.sealed_digest
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token="5Aep_rotated"))]):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    w.conn.refresh_from_db()
    after = opened(w, w.conn, w.conn.sealed, "refresh")
    assert after == {**before, "refresh_token": "5Aep_rotated"}  # only that field changed
    assert after["statement_floor"] == 1
    assert w.conn.sealed_digest != old and w.conn.sealed_sig["expect"] == old
    assert w.conn.lease_id is None  # stored: nothing left to protect
    assert zk_seal.sealed_targets(w.conn.sealed) == rcpt.targets(rcpt.recipients(w.conn))
    assert Event.objects.get(kind="connections.server_refreshed").payload["rotated"] is True


def test_one_derive_per_recipient_when_both_envelopes_are_written(w):
    n = len(rcpt.recipients(w.conn))
    real = server_key.derive
    seen = []

    def derive(jwk):
        seen.append(jwk["x"])
        return real(jwk)

    with FakeSF(token=[_resp(body=_token_body(refresh_token="5Aep_rotated"))]), \
            mock.patch.object(server_key, "derive", derive):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    # The refresh envelope: its own entry and its writer. Then each recipient once, for two envelopes.
    assert len(seen) == 2 + n


def test_write_back_conflict_retries_from_new_row(w):
    """A share lands during the refresh: the digest moved and the floor rose. The retry is sealed from
    the new row with the token already in hand, and the provider is not asked twice."""
    calls = []
    real = SalesforceProvider.refresh

    def refresh(self, token):
        calls.append(token)
        out = real(self, token)
        w.shared(Connection.objects.get(pk=w.conn.pk), agents=[w.eva], canopy_web=server_key.thumbprint())
        return out

    with FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token="5Aep_rotated"))]) as sf, \
            mock.patch.object(SalesforceProvider, "refresh", refresh):
        assert server_tokens.access_token(w.conn, sleep=never_sleep)[0] == "00D!new"
    assert calls == [FAKE_REFRESH] and len(sf.token_calls()) == 1
    w.conn.refresh_from_db()
    assert w.conn.statement_version == 2
    assert opened(w, w.conn, w.conn.sealed, "refresh")["statement_floor"] == 2
    assert opened(w, w.conn, w.conn.sealed, "refresh")["refresh_token"] == "5Aep_rotated"
    assert opened(w, w.conn, w.conn.access_sealed, "access")["statement_floor"] == 2


def test_a_second_conflict_keeps_a_rotated_lease_and_stores_nothing(w):
    tries = []

    def conflict(*a, **kw):
        tries.append(kw["expect"])
        raise wire.WireError(409, "zk_value_changed", "changed")

    with FakeSF(token=[_resp(body=_token_body(refresh_token="5Aep_rotated"))]), \
            mock.patch.object(tokens, "write_back", conflict), pytest.raises(server_tokens.Refused):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert len(tries) == 2
    w.conn.refresh_from_db()
    assert w.conn.access_sealed == "" and w.conn.lease_id is not None  # the expiry says what was lost


def test_a_refresh_token_replaced_during_the_refresh_writes_nothing(w):
    """A reconnect's new grant must not be overwritten with a token from the old one."""
    real = SalesforceProvider.refresh
    swapped = []

    def refresh(self, token):
        out = real(self, token)
        if not swapped:
            swapped.append(True)
            reseal_refresh(w, Connection.objects.get(pk=w.conn.pk), refresh_token="5Aep_someone_elses")
        return out

    tokens_seen = [_resp(body=_token_body(access_token="00D!stale", refresh_token="5Aep_rotated")),
                   _resp(body=_token_body(access_token="00D!second", refresh_token=None))]
    with FakeSF(token=tokens_seen), mock.patch.object(SalesforceProvider, "refresh", refresh):
        assert server_tokens.access_token(w.conn, sleep=lambda _: None)[0] == "00D!second"
    w.conn.refresh_from_db()
    assert opened(w, w.conn, w.conn.sealed, "refresh")["refresh_token"] == "5Aep_someone_elses"


def test_refresh_goes_to_the_login_host_and_follows_no_redirect(w):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **{k: kw.get(k) for k in ("allow_redirects", "timeout")})
        return _resp(body=_token_body(refresh_token=None))

    with mock.patch("apps.connections.providers.base.requests.post", post):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert seen == {"url": "https://login.salesforce.com/services/oauth2/token", "allow_redirects": False,
                    "timeout": 15}


@pytest.mark.parametrize("status", [302, 307, 429, 503])
def test_a_provider_that_is_not_answering_is_not_a_rejection(w, status):
    with FakeSF(token=[_resp(status=status, body={"error": "invalid_grant"})]), \
            pytest.raises(services.ConnectionError) as e:
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert e.value.code == "provider_unavailable"
    w.conn.refresh_from_db()
    assert not w.conn.needs_reconnect and w.conn.lease_id is None


def test_unshared_row_is_sealed_only_to_its_owner_and_canopy_web(key):
    """A reconnect keeps the old statement, which decides nothing: the new token must not reach the
    machines it listed until the owner shares again."""
    world = TokensWorld()
    conn = world.shared(sealed_to_owner({"agent": world.eva}, world.person, app=world.app, server_use=True),
                        agents=[world.eva], canopy_web=server_key.thumbprint())
    assert any(k.ecdh_jkt for k in rcpt.devices(conn))
    conn.state = Connection.UNSHARED
    owner = rcpt.owner_member(conn)
    conn.sealed = zk_seal.seal_to(_refresh_plain(conn), agent_id=conn.binding, name="refresh", kv=0, recipients=[
        owner, ("device", server_key.thumbprint(), server_key.public_jwk())])
    conn.sealed_digest, conn.sealed_sig = vault_engine.value_digest(conn.sealed), None
    conn.statement_version = 0
    conn.statement = None
    conn.save()
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token="5Aep_rotated"))]):
        assert server_tokens.access_token(conn, sleep=never_sleep)[0] == "00D!new"
    conn.refresh_from_db()
    assert conn.state == Connection.UNSHARED and conn.sealed_sig is not None
    want = {("member", owner[1]), me()}
    assert zk_seal.sealed_targets(conn.access_sealed) == want == zk_seal.sealed_targets(conn.sealed)


def test_unshared_row_with_an_old_statement_does_not_reach_its_machines(w):
    conn = w.conn
    owner = rcpt.owner_member(conn)
    assert rcpt.devices(conn)  # the old statement still names them
    conn.state = Connection.UNSHARED
    conn.sealed = zk_seal.seal_to(_refresh_plain(conn), agent_id=conn.binding, name="refresh", kv=0, recipients=[
        owner, ("device", server_key.thumbprint(), server_key.public_jwk())])
    conn.sealed_digest, conn.sealed_sig = vault_engine.value_digest(conn.sealed), None
    conn.save()
    with FakeSF(**NO_ROTATION):
        server_tokens.access_token(conn, sleep=never_sleep)
    conn.refresh_from_db()
    assert zk_seal.sealed_targets(conn.access_sealed) == {("member", owner[1]), me()}


# ---- the lease -----------------------------------------------------------------------

def test_two_callers_one_provider_call(w):
    """The second worker arrives while the first is at the provider: it waits and never refreshes.
    (SQLite has no row locks; tests/test_connections_server_tokens_pg.py races real threads.)"""
    real = SalesforceProvider.refresh
    polls = []

    def refresh(self, token):
        with pytest.raises(server_tokens.Waiting):
            server_tokens.access_token(w.conn, sleep=lambda s: polls.append(s))
        return real(self, token)

    with FakeSF(**NO_ROTATION) as sf, mock.patch.object(SalesforceProvider, "refresh", refresh):
        assert server_tokens.access_token(w.conn, sleep=never_sleep)[0] == "00D!new"
        assert polls == [1] * 30
        # The first wrote: the second now gets the same token, with no call of its own.
        server_tokens._forget_all()
        assert server_tokens.access_token(w.conn, sleep=never_sleep)[0] == "00D!new"
    assert len(sf.token_calls()) == 1


def test_a_write_landing_just_before_the_lease_is_used_not_repeated(w):
    """This worker read a stale row; another finished in between, leaving the lease free. Only
    once it holds the lease can no earlier holder land, so it looks again before refreshing."""
    real = tokens.take_lease

    def take(conn, who, lease_id=None):
        if not lease_id:
            machine_writes_access(w, Connection.objects.get(pk=conn.pk), token="00D!theirs")
        return real(conn, who, lease_id)

    with FakeSF() as sf, mock.patch.object(tokens, "take_lease", take):
        assert server_tokens.access_token(w.conn, sleep=never_sleep)[0] == "00D!theirs"
    assert sf.calls == []
    w.conn.refresh_from_db()
    assert w.conn.lease_id is None  # given back


def test_waits_for_machine_lease_then_uses_its_write(w):
    device = w.machines[w.box_a][0]
    tokens.take_lease(w.conn, device)
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 3:
            machine_writes_access(w, w.conn, token="00D!theirs")

    with FakeSF() as sf:
        assert server_tokens.access_token(w.conn, sleep=sleep) == ("00D!theirs", INSTANCE)
    assert polls == [1, 1, 1] and sf.calls == []


def test_a_held_lease_past_thirty_seconds_is_waiting(w):
    tokens.take_lease(w.conn, w.machines[w.box_a][0])
    polls = []
    with FakeSF() as sf, pytest.raises(server_tokens.Waiting):
        server_tokens.access_token(w.conn, sleep=polls.append)
    assert polls == [1] * 30 and sf.calls == []


def test_polling_takes_no_row_lock(w):
    tokens.take_lease(w.conn, w.machines[w.box_a][0])
    with mock.patch.object(Connection.objects, "select_for_update", side_effect=AssertionError("locked")), \
            pytest.raises(server_tokens.Waiting):
        server_tokens.access_token(w.conn, sleep=lambda _: None)


def test_a_lease_given_up_while_waiting_is_taken(w):
    device = w.machines[w.box_a][0]
    lease_id = tokens.take_lease(w.conn, device)["lease_id"]
    with FakeSF(**NO_ROTATION):
        assert server_tokens.access_token(
            w.conn, sleep=lambda _: tokens.release_lease(w.conn, device, lease_id))[0] == "00D!new"


def test_a_disconnect_while_waiting_is_not_connected(w):
    tokens.take_lease(w.conn, w.machines[w.box_a][0])

    def sleep(_):
        Connection.objects.filter(pk=w.conn.pk).delete()

    with pytest.raises(services.NotConnected):
        server_tokens.access_token(w.conn, sleep=sleep)


def test_canopy_webs_lease_blocks_a_machine_and_names_itself(w, caplog):
    jkt = server_key.thumbprint()
    writer = tokens.Writer(jkt, "canopy-web")
    held = tokens.take_lease(w.conn, writer)
    w.conn.refresh_from_db()
    assert w.conn.lease_holder == jkt
    device = w.machines[w.box_a][0]
    with pytest.raises(wire.WireError) as e:
        tokens.take_lease(w.conn, device)
    assert e.value.code == "lease_held"
    Connection.objects.filter(pk=w.conn.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert tokens.take_lease(w.conn, device)["lease_id"] != held["lease_id"]
    event = Event.objects.get(kind="connections.lease_expired_unwritten")
    assert event.payload["machine"] == "canopy-web" and jkt not in event.summary


# ---- what the provider says ----------------------------------------------------------

def test_invalid_grant_marks_needs_reconnect(w):
    with FakeSF(token=[_resp(status=400, body={"error": "invalid_grant", "error_description": "expired"})]), \
            pytest.raises(services.NotConnected) as e:
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert "connect it again" in str(e.value)
    w.conn.refresh_from_db()
    assert w.conn.needs_reconnect and w.conn.lease_id is None
    event = Event.objects.get(kind="connections.reconnect_required")
    assert (event.payload["error"], event.payload["machine"]) == ("invalid_grant", "canopy-web")
    assert "expired" in event.payload["detail"]
    with FakeSF() as sf, pytest.raises(services.NotConnected):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert sf.calls == []


def test_another_rejection_is_no_reconnect(w, caplog):
    caplog.set_level(logging.WARNING, logger="apps.connections.services")
    with FakeSF(token=[_resp(status=400, body={"error": "invalid_client"})]), \
            pytest.raises(services.ConnectionError) as e:
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert e.value.code == "provider_error"
    w.conn.refresh_from_db()
    assert not w.conn.needs_reconnect and w.conn.lease_id is None
    assert "connections.provider_error" in caplog.text


def test_a_provider_error_is_scrubbed_of_what_was_sent(w):
    words = f"bad token {FAKE_REFRESH} with app-secret"
    with FakeSF(token=[_resp(status=400, body={"error": "invalid_grant", "error_description": words})]), \
            pytest.raises(services.NotConnected):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    detail = Event.objects.get(kind="connections.reconnect_required").payload["detail"]
    assert FAKE_REFRESH not in detail and "app-secret" not in detail and "bad token" in detail


def test_an_app_with_no_client_is_not_configured(w):
    sf_app(w.ws, secret="")
    w.app.refresh_from_db()
    with FakeSF() as sf, pytest.raises(services.NotConfigured):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert sf.calls == []
    w.conn.refresh_from_db()
    assert w.conn.lease_id is None


# ---- off -----------------------------------------------------------------------------

def test_off_raises_server_use_off(w):
    Connection.objects.filter(pk=w.conn.pk).update(server_use=False)
    with mock.patch.object(server_key, "derive", side_effect=AssertionError("KMS was asked")), \
            pytest.raises(server_tokens.ServerUseOff) as e:
        server_tokens.access_token(w.conn)
    assert e.value.code == "server_use_off" and str(e.value)


def test_a_cached_token_is_not_used_once_off(w):
    machine_writes_access(w, w.conn)
    server_tokens.access_token(w.conn)
    Connection.objects.filter(pk=w.conn.pk).update(server_use=False)
    with pytest.raises(server_tokens.ServerUseOff):
        server_tokens.access_token(w.conn)


def test_no_key_configured_is_off(w):
    server_key.use_software_key_for_tests(None)
    with pytest.raises(server_tokens.ServerUseOff):
        server_tokens.access_token(w.conn)


def test_an_on_row_sealed_to_its_owner_alone_is_off_with_a_sentence(key):
    """A reconnect during a KMS failure: the switch stayed on and the seal reached only the owner."""
    world = TokensWorld()
    conn = sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=True)
    with pytest.raises(server_tokens.ServerUseOff) as e:
        server_tokens.access_token(conn)
    assert "connect" in str(e.value)


def test_a_statement_naming_another_key_is_off(w):
    Connection.objects.filter(pk=w.conn.pk).update(statement={**w.conn.statement, "canopy_web": "A" * 43})
    with pytest.raises(server_tokens.ServerUseOff):
        server_tokens.access_token(w.conn)


def test_kms_down_is_unavailable(w):
    machine_writes_access(w, w.conn)
    with mock.patch.object(server_key, "derive", side_effect=server_key.Unavailable("down")), \
            pytest.raises(server_key.Unavailable):
        server_tokens.access_token(w.conn)


def test_being_revoked_and_gone_are_not_connected(w):
    Connection.objects.filter(pk=w.conn.pk).update(state=Connection.REVOKING)
    with pytest.raises(services.NotConnected):
        server_tokens.access_token(w.conn)
    Connection.objects.filter(pk=w.conn.pk).delete()
    with pytest.raises(services.NotConnected):
        server_tokens.access_token(w.conn)


# ---- what canopy-web checks before it trusts what it opens --------------------------------

@pytest.mark.parametrize("over", [{"owner_key": "B" * 43}, {"account_id": "00Dother/005user"},
                                  {"instance_url": "https://evil.example"},
                                  {"instance_url": "http://dimagi.my.salesforce.com"}, {"v": 2},
                                  {"access_token": ""}, {"statement_floor": 99}, {"expires_at": "soon"},
                                  {"extra": "field"}, {"instance_url": None}])
def test_an_access_that_fails_a_check_is_refused(w, over):
    machine_writes_access(w, w.conn, **over)
    refused_access(w.conn)


@pytest.mark.parametrize("over", [{"owner_key": "B" * 43}, {"account_id": "00Dother/005user"},
                                  {"login_host": "login.evil.example"}, {"login_host": "test.salesforce.com"},
                                  {"instance_url": "https://evil.example"}, {"client_id": "someone-elses"},
                                  {"provider": "google"}, {"statement_floor": 99}, {"v": 2},
                                  {"refresh_token": ""}, {"login_host": None}, {"instance_url": None}])
def test_a_refresh_that_fails_a_check_is_refused_and_no_host_is_called(w, over):
    reseal_refresh(w, w.conn, **over)
    with FakeSF() as sf, pytest.raises(server_tokens.Refused):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert sf.calls == []
    w.conn.refresh_from_db()
    assert w.conn.lease_id is None


def test_a_mac_that_does_not_verify_is_refused(w):
    machine_writes_access(w, w.conn)
    sig = w.conn.access_sig
    me_target = server_key.thumbprint()
    sig["macs"] = [{**m, "mac": zk_ref.b64u(b"\0" * 32)} if m["target"] == me_target else m for m in sig["macs"]]
    Connection.objects.filter(pk=w.conn.pk).update(access_sig=sig)
    refused_access(w.conn)


def test_a_writer_nobody_vouches_for_is_refused(w):
    """Anyone can build a `zks1` from public keys: a good MAC from a stranger's key means nothing."""
    stranger = ec.generate_private_key(ec.SECP256R1())
    conn = w.conn
    rs = rcpt.recipients(conn)
    plain = {"v": 1, "access_token": "00D!forged", "expires_at": int(time.time()) + 1800, "instance_url": INSTANCE,
             "account_id": conn.account_id, "scope": "api", "owner_key": conn.owner_key, "statement_floor": 1}
    value = zk_seal.seal_to(json.dumps(plain), agent_id=conn.binding, name="access", kv=0, recipients=rs)
    sig = conn_ref.writer_sig(conn.binding, "access", "", value, stranger, "device",
                              zk_ref.jkt(stranger.public_key()), {(k, t): zk.public_key(j) for k, t, j in rs})
    Connection.objects.filter(pk=conn.pk).update(access_sealed=value, access_sig=sig,
                                                 access_expires_at=timezone.now() + timedelta(minutes=30))
    refused_access(conn)


def test_a_member_writer_must_be_the_owner_by_its_key(w):
    conn = w.conn
    other = ec.generate_private_key(ec.SECP256R1())
    rs = rcpt.recipients(conn)
    plain = {"v": 1, "access_token": "00D!forged", "expires_at": int(time.time()) + 1800, "instance_url": INSTANCE,
             "account_id": conn.account_id, "scope": "api", "owner_key": conn.owner_key, "statement_floor": 1}
    value = zk_seal.seal_to(json.dumps(plain), agent_id=conn.binding, name="access", kv=0, recipients=rs)
    sig = conn_ref.writer_sig(conn.binding, "access", "", value, other, "member", str(w.person.pk),
                              {(k, t): zk.public_key(j) for k, t, j in rs})
    Connection.objects.filter(pk=conn.pk).update(access_sealed=value, access_sig=sig,
                                                 access_expires_at=timezone.now() + timedelta(minutes=30))
    refused_access(conn)


def test_a_pruned_writer_the_statement_still_vouches_for_is_read(w):
    """Machines judge a writer by the statement, never today's recipients, or nobody could open
    what a box wrote before it stopped running the agent."""
    machine_writes_access(w, w.conn)
    RunnerAssignment.objects.filter(runner=w.machines[w.box_a][0].runner).delete()
    assert w.machines[w.box_a][0].ecdh_jkt not in {k.ecdh_jkt for k in rcpt.devices(w.conn)}
    assert server_tokens.access_token(w.conn)[0] == ACCESS


def test_an_unsigned_refresh_is_read_only_on_an_unshared_row(w):
    conn = w.conn
    conn.sealed_sig = None
    conn.save(update_fields=["sealed_sig"])
    assert conn.state == Connection.ACTIVE
    refused_access(conn)
    Connection.objects.filter(pk=conn.pk).update(state=Connection.UNSHARED)
    with FakeSF(**NO_ROTATION):
        assert server_tokens.access_token(conn, sleep=never_sleep)[0] == "00D!new"


def test_an_unsigned_access_is_refused_even_unshared(w):
    machine_writes_access(w, w.conn)
    Connection.objects.filter(pk=w.conn.pk).update(state=Connection.UNSHARED, access_sig=None)
    refused_access(w.conn)


# ---- the bytes a machine reads ---------------------------------------------------------

def test_writer_sig_matches_the_vectors():
    """The reference's own device writer, over its fixed envelope, byte for byte."""
    from tests.fixtures.vault_zk import ZK_DIR

    env = json.loads((ZK_DIR / "connections_v1_vectors.json").read_text())["envelope"]
    want = env["device_writer"]["sig"]
    writer = ec.derive_private_key(int(env["device_writer"]["priv_scalar_hex"], 16), ec.SECP256R1())
    keys = {(r["kind"], r["target"]): r["public_jwk"] for r in env["recipients"]}
    got = wire.writer_sig(env["sealed"], binding=env["binding"], name="refresh", expect=want["expect"],
                          by_kind="device", by_target=want["by"]["target"], public_jwk=want["by"]["public_jwk"],
                          keys=keys, derive=lambda jwk: writer.exchange(ec.ECDH(), zk.public_key(jwk)))
    assert got == want
    for e, m in zip(env["entries"], want["macs"], strict=True):
        assert wire.envelope_msg(env["binding"], "refresh", want["expect"], next(
            x for x in wire.sealed_entries(env["sealed"])
            if (x["kind"], x["target"]) == (e["kind"], e["target"]))) == e["device_writer_msg"]
        assert (m["kind"], m["target"]) == (e["kind"], e["target"])
    for r in env["recipients"]:
        recipient = ec.derive_private_key(int(r["priv_scalar_hex"], 16), ec.SECP256R1())
        z = recipient.exchange(ec.ECDH(), zk.public_key(want["by"]["public_jwk"]))
        assert wire.mac_verifies(env["sealed"], want, binding=env["binding"], name="refresh", kind=r["kind"],
                                 target=r["target"], z=z)
        assert not wire.mac_verifies(env["sealed"], want, binding=env["binding"], name="access", kind=r["kind"],
                                     target=r["target"], z=z)


def test_a_machine_reads_what_canopy_web_wrote(w):
    """Review Focus 3: Salesforce rotates during canopy-web's refresh. A box checks both envelopes as
    the reference does, the statement naming their writer lets it, and its reseal keeps canopy-web."""
    device, _, ecdh = w.machines[w.box_a]
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token="5Aep_rotated"))]):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    conn = Connection.objects.get(pk=w.conn.pk)
    pub = server_key._software.public_key()
    for name, value, sig in (("refresh", conn.sealed, conn.sealed_sig),
                             ("access", conn.access_sealed, conn.access_sig)):
        entries = {(e["kind"], e["target"]): e for e in conn_ref.sealed_entries(value)}
        macs = {(m["kind"], m["target"]): m["mac"] for m in sig["macs"]}
        assert set(entries) == set(macs)
        mine = ("device", device.ecdh_jkt)
        assert conn_ref.verify_envelope_mac(conn.binding, name, sig["expect"], entries[mine], macs[mine], ecdh, pub)
        assert not conn_ref.verify_envelope_mac(conn.binding, name, "0" * 64, entries[mine], macs[mine], ecdh, pub)
        assert sig["by"]["target"] == conn.statement["canopy_web"]
        wire.check_writer_sig(value, sig)
        assert rcpt._writer_provable(conn, sig)
        plain = json.loads(zk_ref.open_sealed_value(value, conn.binding, name, "device", device.ecdh_jkt, ecdh))
        assert plain["statement_floor"] <= conn.statement["version"] and plain["owner_key"] == conn.owner_key
    # A keeper's re-seal is checked as any other, and must keep canopy-web.
    [work] = [x for x in keeper.work(device.runner) if x["id"] == conn.pk] or [None]
    assert work is None  # no machine waits: eva-cloud-2 and ada's laptop hold it already
    body = reseal_body(w, conn, w.box_a)
    without = [r for r in rcpt.recipients(conn) if r[1] != server_key.thumbprint()]
    bad = reseal_body(w, conn, w.box_a, to=without)
    with pytest.raises(wire.WireError) as e:
        keeper.reseal(conn, device.runner, device, sealed=bad["sealed"], sealed_sig=bad["sealed_sig"],
                      expect=bad["expect"], access_sealed=conn.access_sealed, access_sig=conn.access_sig)
    assert e.value.code == "zk_rewrap_required"
    assert me() in zk_seal.sealed_targets(body["sealed"])


# ---- nothing across a network call -------------------------------------------------------

def test_no_kms_or_provider_call_in_a_transaction(w):
    """`atomic()` pushes a savepoint here (the test itself runs in one), so the depth says."""
    machine_writes_access(w, w.conn, life=30)
    base = len(connection.savepoint_ids)
    depths = []
    real_derive, real_refresh = server_key.derive, SalesforceProvider.refresh

    def derive(jwk):
        depths.append(len(connection.savepoint_ids))
        return real_derive(jwk)

    def refresh(self, token):
        depths.append(len(connection.savepoint_ids))
        return real_refresh(self, token)

    with FakeSF(token=[_resp(body=_token_body(refresh_token="5Aep_rotated"))]), \
            mock.patch.object(server_key, "derive", derive), \
            mock.patch.object(SalesforceProvider, "refresh", refresh):
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert depths and set(depths) == {base}


def test_no_kms_or_provider_call_in_a_disconnects_transaction(w):
    base = len(connection.savepoint_ids)
    depths = []
    real_derive, real_revoke = server_key.derive, SalesforceProvider.revoke

    def derive(jwk):
        depths.append(len(connection.savepoint_ids))
        return real_derive(jwk)

    def revoke(self, token, **kw):
        depths.append(len(connection.savepoint_ids))
        return real_revoke(self, token, **kw)

    with FakeSF(), mock.patch.object(server_key, "derive", derive), \
            mock.patch.object(SalesforceProvider, "revoke", revoke):
        assert services.disconnect(w.conn, by=w.person) == "deleted"
    assert depths and set(depths) == {base}


# ---- disconnecting ---------------------------------------------------------------------

def test_disconnect_revokes_at_once(w):
    with FakeSF() as sf:
        assert services.disconnect(w.conn, by=w.person) == "deleted"
    revokes = [c for c in sf.calls if c[1] == REVOKE_URL]
    assert len(revokes) == 1 and revokes[0][2]["token"] == FAKE_REFRESH
    assert revokes[0][2]["token_type_hint"] == "refresh_token"
    assert not Connection.objects.filter(pk=w.conn.pk).exists()
    event = Event.objects.get(kind="connections.revoked")
    assert event.payload["machine"] == "canopy-web" and event.payload["at_provider"] is True
    assert Event.objects.filter(kind="connections.disconnected").exists()
    assert not Event.objects.filter(kind="connections.revoke_unconfirmed").exists()


def test_a_never_shared_connection_is_revoked_too(key):
    """Its unsigned first seal: `unshared` until the disconnect, `revoking` when it is opened."""
    world = TokensWorld()
    conn = sealed_to_owner({"agent": world.eva}, world.person, app=world.app, server_use=True)
    owner = rcpt.owner_member(conn)
    conn.sealed = zk_seal.seal_to(_refresh_plain(conn), agent_id=conn.binding, name="refresh", kv=0, recipients=[
        owner, ("device", server_key.thumbprint(), server_key.public_jwk())])
    conn.sealed_digest = vault_engine.value_digest(conn.sealed)
    conn.save()
    with FakeSF() as sf:
        assert services.disconnect(conn, by=world.person) == "deleted"
    assert [c[1] for c in sf.calls] == [REVOKE_URL]


def test_a_failed_revoke_stays_revoking_for_the_sweep_to_retry(w):
    with FakeSF(revoke=_resp(status=503)):
        assert services.disconnect(w.conn, by=w.person) == "revoking"
    w.conn.refresh_from_db()
    assert w.conn.state == Connection.REVOKING
    with FakeSF(revoke=_resp(status=503)):
        keeper.sweep()
    assert Connection.objects.filter(pk=w.conn.pk).exists()
    with FakeSF() as sf:
        out = keeper.sweep()
    assert [c[1] for c in sf.calls] == [REVOKE_URL] and out["revoke_unconfirmed"] == 0
    assert not Connection.objects.filter(pk=w.conn.pk).exists()


def test_with_no_cloud_box_a_failed_revoke_still_waits_for_canopy_web(key):
    world = TokensWorld()
    conn = world.shared(sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=True),
                        canopy_web=server_key.thumbprint())
    assert rcpt.revokers(conn) == []
    with FakeSF(revoke=_resp(status=503)):
        assert services.disconnect(conn, by=world.person) == "revoking"
        assert keeper.sweep()["revoke_unconfirmed"] == 0
    assert Connection.objects.filter(pk=conn.pk).exists()
    with FakeSF(revoke=_resp(status=503)):  # a week of that is enough
        assert keeper.sweep(now=timezone.now() + keeper.REVOKE_GRACE + timedelta(hours=1))["revoke_unconfirmed"] == 1
    assert not Connection.objects.filter(pk=conn.pk).exists()


def test_a_token_the_provider_says_is_dead_needs_no_retry(w):
    with FakeSF(revoke=_resp(status=400, body={"error": "invalid_token"})):
        assert services.disconnect(w.conn, by=w.person) == "deleted"


def test_revoke_if_able_never_raises_out_of_a_disconnect(w):
    with mock.patch.object(SalesforceProvider, "revoke", side_effect=RuntimeError("boom")):
        assert services.disconnect(w.conn, by=w.person) == "revoking"
    with mock.patch.object(server_key, "derive", side_effect=server_key.Unavailable("down")):
        assert server_tokens.revoke_if_able(w.conn) is False
    assert Connection.objects.filter(pk=w.conn.pk, state=Connection.REVOKING).exists()


def test_revoke_if_able_leaves_what_it_cannot_open(w):
    Connection.objects.filter(pk=w.conn.pk).update(state=Connection.REVOKING)
    server_key.use_software_key_for_tests(None)
    assert server_tokens.revoke_if_able(w.conn) is False
    server_key.use_software_key_for_tests(ec.generate_private_key(ec.SECP256R1()))  # another key: no entry
    with FakeSF() as sf:
        assert server_tokens.revoke_if_able(w.conn) is False
    assert sf.calls == [] and Connection.objects.filter(pk=w.conn.pk).exists()


def test_revoke_if_able_only_touches_a_revoking_row(w):
    with FakeSF() as sf:
        assert server_tokens.revoke_if_able(w.conn) is False
    assert sf.calls == []


def test_removing_an_app_inside_a_transaction_defers_the_revoke_to_the_sweep(w):
    """`keeper._finish_removal` holds the app's lock: the provider call waits for the next tick."""
    with mock.patch.object(server_tokens, "revoke_if_able", side_effect=AssertionError("in a transaction")):
        assert services.disconnect(w.conn, by=None, revoke_now=False) == "revoking"
    with FakeSF() as sf:
        keeper.sweep()
    assert [c[1] for c in sf.calls] == [REVOKE_URL]


def _google_on_row(world, **owner_kw):
    app = sf_app(world.ws, provider="google", client_id="123.apps.googleusercontent.com", secret="GOCSPX-s")
    conn = _stored(owner_kw, world.person, app=app, sealed="", account_id="1098", account_label="a@gmail.com",
                   instance_url="", server_use=True, owner_key=rcpt.device_keys.thumbprint(
                       zk_ref.jwk(world.person_priv.public_key())),
                   owner_public_jwk=zk_ref.jwk(world.person_priv.public_key()), owner_user_id=str(world.person.pk),
                   signer=world.person)
    plain = {"v": 1, "provider": "google", "refresh_token": "1//refresh", "client_id": app.client_id,
             "login_host": "", "instance_url": "", "account_id": "1098", "owner_key": conn.owner_key,
             "issued_at": 1793500000, "scopes": "openid", "statement_floor": 0}
    conn.sealed = zk_seal.seal_to(json.dumps(plain), agent_id=conn.binding, name="refresh", kv=0, recipients=[
        rcpt.owner_member(conn), ("device", server_key.thumbprint(), server_key.public_jwk())])
    conn.sealed_digest = vault_engine.value_digest(conn.sealed)
    conn.save()
    return conn


def test_google_revoke_skipped_when_other_connection_remains(key):
    world = TokensWorld()
    conn = _google_on_row(world, user=world.person)
    other = _google_on_row(world, agent=world.eva)
    with FakeGoogle() as g:
        assert services.disconnect(conn, by=world.person) == "deleted"
    assert g.revokes() == []  # revoking there would end `other` too
    assert not Connection.objects.filter(pk=conn.pk).exists() and Connection.objects.filter(pk=other.pk).exists()
    with FakeGoogle() as g:
        assert services.disconnect(other, by=world.person) == "deleted"
    assert [r["token"] for r in g.revokes()] == ["1//refresh"]


# ---- never a token -----------------------------------------------------------------------

def test_no_token_in_events_or_logs(w, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="apps")
    raised = []
    secrets = [FAKE_REFRESH, "5Aep_rotated", "00D!new", ACCESS, "app-secret"]
    machine_writes_access(w, w.conn)
    assert server_tokens.access_token(w.conn)[0] == ACCESS
    machine_writes_access(w, w.conn, life=30)
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token="5Aep_rotated"))]):
        assert server_tokens.access_token(w.conn, sleep=never_sleep)[0] == "00D!new"
    # Each refusal, with what was sent in the provider's words.
    for body, status in (({"error": "invalid_client", "error_description": "no 5Aep_rotated"}, 400),
                         ({"error": "invalid_grant", "error_description": "no 5Aep_rotated app-secret"}, 400),
                         ({}, 503)):
        Connection.objects.filter(pk=w.conn.pk).update(access_sealed="", access_sig=None, access_expires_at=None,
                                                       refresh_failed_at=None)
        server_tokens._forget_all()
        with FakeSF(token=[_resp(status=status, body=body)]), pytest.raises(services.ConnectionError) as e:
            server_tokens.access_token(w.conn, sleep=never_sleep)
        raised += [str(e.value), repr(e.value)]
    Connection.objects.filter(pk=w.conn.pk).update(refresh_failed_at=None)
    with FakeSF(revoke=_resp(status=400, body={"error": "x", "error_description": "no 5Aep_rotated"})):
        assert services.disconnect(w.conn, by=w.person) == "revoking"
    with FakeSF():
        assert server_tokens.revoke_if_able(w.conn) is True
    assert {"server_refreshed", "reconnect_required", "disconnected", "revoked"} <= {
        e.kind.removeprefix("connections.") for e in Event.objects.all()}
    tracebacks = [logging.Formatter().formatException(r.exc_info) for r in caplog.records if r.exc_info]
    blob = "\n".join([r.getMessage() for r in caplog.records] + tracebacks + raised
                     + [json.dumps(e.payload) + e.summary for e in Event.objects.all()]
                     + [json.dumps(list(Connection.objects.values()), default=str)])
    assert [s for s in secrets if s in blob] == []


def test_a_refusal_carries_nothing_from_the_envelope(w):
    """A `Refused` says what failed, in a fixed sentence: never what the envelope held."""
    reseal_refresh(w, w.conn, owner_key="B" * 43)
    with FakeSF(), pytest.raises(server_tokens.Refused) as e:
        server_tokens.access_token(w.conn, sleep=never_sleep)
    assert FAKE_REFRESH not in str(e.value) and FAKE_REFRESH not in repr(e.value)
    assert e.value.__cause__ is None


# ---- what the CLI agrees on -------------------------------------------------------------

def test_the_cli_and_canopy_web_agree_on_the_provider_rules():
    """Literals, read from canopy's `src/orchestrator/connections/providers/{salesforce,google}.py`:
    `f"https://{login_host}/services/oauth2/token"` and `/revoke`, `LOGIN_HOSTS`, `MY_DOMAIN_SUFFIX`,
    `TOKEN_URL`, `REVOKE_URL`, `rotates_refresh_token`. A change on either side changes this test."""
    assert SalesforceProvider.rotates_refresh_token is True
    assert GoogleProvider.rotates_refresh_token is False
    assert (GOOGLE_TOKEN, GOOGLE_REVOKE) == ("https://oauth2.googleapis.com/token",
                                             "https://oauth2.googleapis.com/revoke")
    for host in ("login.salesforce.com", "test.salesforce.com", "acme.my.salesforce.com"):
        app = mock.Mock(options={"login_url": f"https://{host}"}, extra_scopes=())
        p = SalesforceProvider(app)
        assert p.token_url() == f"https://{host}/services/oauth2/token"
        assert p.revoke_url() == f"https://{host}/services/oauth2/revoke"
        p.check_sealed_hosts(login_host=host)
    p = SalesforceProvider(mock.Mock(options={"login_url": "https://login.salesforce.com"}))
    for bad in ("evil.example", "login.salesforce.com.evil.example", "my.salesforce.com", "a.salesforce.com",
                "x:1.my.salesforce.com", ""):
        with pytest.raises(providers.ProviderError):
            p.check_sealed_hosts(login_host=bad)
    p.check_sealed_hosts(instance_url="https://acme.my.salesforce.com")
    p.check_sealed_hosts(instance_url="https://acme.my.salesforce.com/")
    for bad in ("http://acme.my.salesforce.com", "https://evil.example", "https://acme.my.salesforce.com/x",
                "https://u@acme.my.salesforce.com", "https://acme.my.salesforce.com:1", ""):
        with pytest.raises(providers.ProviderError):
            p.check_sealed_hosts(instance_url=bad)
    g = GoogleProvider()
    g.check_sealed_hosts(login_host="", instance_url="")
    for over in ({"login_host": "accounts.google.com"}, {"instance_url": "https://evil.example"}):
        with pytest.raises(providers.ProviderError):
            g.check_sealed_hosts(**over)


def test_a_sealed_login_host_must_be_the_apps():
    p = SalesforceProvider(mock.Mock(options={"login_url": "https://login.salesforce.com"}))
    with pytest.raises(providers.ProviderError):
        p.check_sealed_hosts(login_host="test.salesforce.com")


def test_provider_refresh_comes_with_the_apps_client(w):
    p = providers.for_app(w.app)
    with FakeSF(token=[_resp(body=_token_body(refresh_token=None))]) as sf:
        answer = p.refresh("5Aep_x")
    assert answer.refresh_token == "" and answer.access_token == "00D!access_1"
    assert sf.token_calls()[0][2]["client_secret"] == "app-secret"


def test_machines_lease_routes_still_store_their_own_key(w):
    device = w.machines[w.box_a][0]
    tokens.take_lease(w.conn, device)
    w.conn.refresh_from_db()
    assert w.conn.lease_holder == device.ecdh_jkt
