"""A `pool` slot's data: public values and an encrypted, write-only secret half.

The rules (docs/architecture/capabilities.md § 6, "Slot data"): a workspace admin
or the agent's own login writes it per key; any member reads it MASKED (secret key
names, never values — admins included); the secret values leave canopy-web only
on the lease read of the user who acquired a READY lease on that slot, and each
such delivery is an event that never carries a value. Nothing — events, the
settings audit, the MCP call log, errors, the DB column — holds a secret in clear.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.utils import timezone
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.capabilities import aws, services
from apps.capabilities.models import CapabilityLease, CapabilitySlotData
from apps.events.models import Event, SettingChange
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member

from .helpers import MANIFEST, _as, _json, _pat, _publish

pytestmark = pytest.mark.django_db

POOL = {
    "version": 1, "name": "test-worker", "kind": "pool",
    "description": "Connect test workers, one device session each.",
    "slots": ["worker-1", "worker-2", "worker-3", "worker-4"],
    "limits": {"max_lease_s": 14400, "idle_release_s": 900},
}
PIN = "PIN-73914-sentinel"
BACKUP = "BACKUP-55102-sentinel"
SECRETS = (PIN, BACKUP)
SLOTS = "/api/agents/ace/capabilities/test-worker/slots"


def _no_aws(region):
    raise AssertionError(f"a pool capability must never call AWS (aws.get({region!r}))")


@pytest.fixture()
def pool(tenancy, monkeypatch):
    monkeypatch.setattr(aws, "get", _no_aws)
    c = _as(tenancy["owner"])
    assert _publish(c, POOL).status_code == 200
    r = _json(c, "put", "/api/capabilities/grants", {"agent": "ace", "capability": "test-worker"})
    assert r.status_code == 200, r.content
    tenancy["admin"] = a_member(tenancy["ws"], email="ad@dimagi.com", role=M.ADMIN)
    return c


def _put(c, slot="worker-1", **body):
    return _json(c, "put", f"{SLOTS}/{slot}", body)


def _seed(c, slot="worker-1"):
    r = _put(c, slot, public={"phone": "+74260000001", "name": "Test Worker One"},
             secret={"pin": PIN, "backup_code": BACKUP})
    assert r.status_code == 200, r.content
    return r


def _take(c, **extra):
    return _json(c, "post", "/api/capabilities/leases", {"agent": "ace", "capability": "test-worker", **extra})


def _no_secret_in(text: str) -> None:
    for s in SECRETS:
        assert s not in text


def _events_text() -> str:
    return json.dumps([[e.kind, e.summary, e.payload] for e in Event.objects.all()], default=str)


# ---- who may write -----------------------------------------------------------

@pytest.mark.parametrize("who, status", [
    ("owner", 200), ("admin", 200), ("editor", 403), ("viewer", 403), ("outsider", 404),
])
def test_put_permission_matrix(pool, tenancy, who, status):
    r = _put(_as(tenancy[who]), public={"phone": "+1"})
    assert r.status_code == status, r.content


def test_the_agents_own_login_may_write_with_its_pat(pool, tenancy):
    r = _put(_pat(tenancy["bot"]), public={"phone": "+1"}, secret={"pin": PIN})
    assert r.status_code == 200, r.content
    assert r.json()["updated_by"] == "ace@dimagi-ai.com"


@pytest.mark.parametrize("who, status", [
    ("owner", 204), ("admin", 204), ("editor", 403), ("viewer", 403), ("outsider", 404),
])
def test_delete_permission_matrix(pool, tenancy, who, status):
    _seed(pool)
    r = _as(tenancy[who]).delete(f"{SLOTS}/worker-1")
    assert r.status_code == status, r.content
    assert CapabilitySlotData.objects.filter(slot="worker-1").exists() is (status != 204)


def test_the_agent_may_delete_its_own_slot_data(pool, tenancy):
    _seed(pool)
    assert _pat(tenancy["bot"]).delete(f"{SLOTS}/worker-1").status_code == 204
    assert not CapabilitySlotData.objects.exists()
    assert _pat(tenancy["bot"]).delete(f"{SLOTS}/worker-1").status_code == 204  # idempotent


# ---- what a write may name ---------------------------------------------------

def test_an_undeclared_slot_is_422(pool):
    r = _put(pool, "worker-9", public={"phone": "+1"})
    assert r.status_code == 422 and "not one of test-worker's slots" in r.json()["detail"]


def test_an_ec2_capability_has_no_slots(tenancy, world):
    c = _as(tenancy["owner"])
    assert _publish(c, MANIFEST, POOL).status_code == 200
    base = "/api/agents/ace/capabilities/android-emulator/slots"
    assert _json(c, "put", f"{base}/worker-1", {"public": {"a": "b"}}).status_code == 422
    assert c.get(base).status_code == 422
    assert c.delete(f"{base}/worker-1").status_code == 422


def test_bad_values_are_refused_without_echoing_them(pool):
    r = _put(pool, secret={"pin": "   "})
    assert r.status_code == 422 and "send null" in r.json()["detail"]
    r = _put(pool, secret={"bad key!": PIN})
    assert r.status_code == 422
    _no_secret_in(r.content.decode())
    r = _put(pool, public={"pin": "1"}, secret={"pin": PIN})
    assert r.status_code == 422 and "not both" in r.json()["detail"]
    _no_secret_in(r.content.decode())
    assert not CapabilitySlotData.objects.exists()


# ---- per-key upsert ----------------------------------------------------------

def test_per_key_upsert_and_delete(pool):
    _seed(pool)
    # A key left out is kept; a section left out is untouched.
    r = _put(pool, public={"name": "Renamed"})
    assert r.json()["public"] == {"phone": "+74260000001", "name": "Renamed"}
    assert r.json()["secret_keys"] == ["backup_code", "pin"]
    # null deletes one key; the rest stay.
    r = _put(pool, secret={"backup_code": None}, public={"phone": None})
    assert r.json()["public"] == {"name": "Renamed"} and r.json()["secret_keys"] == ["pin"]
    # A key may move from secret to public in one write.
    r = _put(pool, secret={"pin": None}, public={"pin": "0000"})
    assert r.status_code == 200 and r.json()["secret_keys"] == [] and r.json()["public"]["pin"] == "0000"
    # Nothing left: the row goes.
    r = _put(pool, public={"name": None, "pin": None})
    assert r.status_code == 200 and r.json()["public"] == {} and r.json()["updated_at"] is None
    assert not CapabilitySlotData.objects.exists()


def test_every_slot_keeps_its_own_values(pool):
    """Per login even if the same: no shared or default values across slots."""
    for i, slot in enumerate(POOL["slots"], 1):
        _put(pool, slot, public={"phone": f"+7426000000{i}"}, secret={"pin": f"{PIN}-{i}"})
    rows = {r.slot: r for r in CapabilitySlotData.objects.all()}
    assert len(rows) == 4 and len({r.secret_enc for r in rows.values()}) == 4


# ---- masked reads ------------------------------------------------------------

@pytest.mark.parametrize("who", ["owner", "admin", "editor", "viewer"])
def test_the_listing_is_masked_for_everyone(pool, tenancy, who):
    _seed(pool)
    r = _as(tenancy[who]).get(SLOTS)
    assert r.status_code == 200, r.content
    _no_secret_in(r.content.decode())
    body = r.json()
    assert [s["slot"] for s in body["slots"]] == POOL["slots"]
    one = body["slots"][0]
    assert one["public"] == {"phone": "+74260000001", "name": "Test Worker One"}
    assert one["secret_keys"] == ["backup_code", "pin"] and "secret" not in one
    assert one["updated_by"] == "jj@dimagi.com" and one["held"] is False
    assert body["slots"][1] == {"slot": "worker-2", "declared": True, "public": {}, "secret_keys": [],
                                "updated_at": None, "updated_by": None, "held": False, "lease_id": None}


def test_the_listing_says_who_holds_a_slot(pool, tenancy):
    _seed(pool)
    lease = _take(_pat(tenancy["bot"])).json()
    one = pool.get(SLOTS).json()["slots"][0]
    assert one["held"] is True and one["lease_id"] == lease["lease_id"]


def test_the_listing_is_not_for_outsiders_and_is_open_to_the_agent(pool, tenancy):
    _seed(pool)
    assert _as(tenancy["outsider"]).get(SLOTS).status_code == 404
    r = _pat(tenancy["bot"]).get(SLOTS)
    assert r.status_code == 200
    _no_secret_in(r.content.decode())


def test_the_put_response_is_masked(pool):
    r = _seed(pool)
    _no_secret_in(r.content.decode())


# ---- encryption at rest ------------------------------------------------------

def test_the_secret_column_is_ciphertext(pool):
    _seed(pool)
    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM {CapabilitySlotData._meta.db_table}")
        raw = json.dumps(cur.fetchall(), default=str)
    _no_secret_in(raw)
    assert "pin" not in CapabilitySlotData.objects.get().secret_enc
    row = CapabilitySlotData.objects.get()
    _no_secret_in(str(row) + repr(row))


# ---- delivery ----------------------------------------------------------------

def test_the_acquirer_gets_the_slot_data_on_acquire_and_on_get(pool, tenancy):
    _seed(pool)
    bot = _pat(tenancy["bot"])
    lease = _take(bot).json()
    assert lease["status"] == "ready" and lease["slot"] == "worker-1"
    want = {"public": {"phone": "+74260000001", "name": "Test Worker One"},
            "secret": {"pin": PIN, "backup_code": BACKUP}}
    assert lease["slot_data"] == want
    got = _json(bot, "get", f"/api/capabilities/leases/{lease['lease_id']}?wait_s=0").json()
    assert got["slot_data"] == want
    delivered = Event.objects.filter(kind="capability.slot.secret_delivered")
    assert delivered.count() == 2
    p = delivered.first().payload
    assert (p["lease_id"], p["slot"], p["user"], p["secret_keys"]) == (
        lease["lease_id"], "worker-1", "ace@dimagi-ai.com", ["backup_code", "pin"])
    _no_secret_in(_events_text())


def test_a_long_poll_that_gets_the_slot_delivers_it(pool, tenancy, monkeypatch):
    _seed(pool)
    bot = _pat(tenancy["bot"])
    held = _take(bot).json()
    queued = _take(bot, params={"slot": "worker-1"}).json()
    assert queued["status"] == "queued" and queued["slot_data"] is None
    calls = {"n": 0}

    def _sleep(_s):
        calls["n"] += 1
        if calls["n"] == 1:
            bot.delete(f"/api/capabilities/leases/{held['lease_id']}")

    monkeypatch.setattr(services, "_sleep", _sleep)
    got = _json(bot, "get", f"/api/capabilities/leases/{queued['lease_id']}?wait_s=5").json()
    assert got["status"] == "ready" and got["slot_data"]["secret"]["pin"] == PIN


def test_not_to_an_admin_or_another_editor_reading_the_lease(pool, tenancy):
    _seed(pool)
    lease = _take(_pat(tenancy["bot"])).json()
    for who in ("owner", "editor"):
        r = _json(_as(tenancy[who]), "get", f"/api/capabilities/leases/{lease['lease_id']}")
        assert r.status_code == 200 and r.json()["slot_data"] is None
        _no_secret_in(r.content.decode())


def test_an_editor_who_acquired_gets_it_and_the_agent_does_not(pool, tenancy):
    _seed(pool)
    ed = _as(tenancy["editor"])
    lease = _take(ed).json()
    assert lease["slot_data"]["secret"]["pin"] == PIN
    r = _json(_pat(tenancy["bot"]), "get", f"/api/capabilities/leases/{lease['lease_id']}")
    assert r.json()["slot_data"] is None


def test_never_in_a_list_or_capability_read(pool, tenancy):
    _seed(pool)
    bot = _pat(tenancy["bot"])
    _take(bot)
    for path in ("/api/capabilities/leases", "/api/capabilities/leases?status=live",
                 "/api/agents/ace/capabilities"):
        r = bot.get(path)
        assert r.status_code == 200, path
        _no_secret_in(r.content.decode())


@pytest.mark.parametrize("end", ["release", "expire", "idle"])
def test_not_once_the_lease_has_ended(pool, tenancy, end):
    _seed(pool)
    bot = _pat(tenancy["bot"])
    lease = _take(bot).json()
    lid = lease["lease_id"]
    now = timezone.now()
    if end == "release":
        r = bot.delete(f"/api/capabilities/leases/{lid}")
        _no_secret_in(r.content.decode())
    elif end == "expire":
        CapabilityLease.objects.filter(pk=lid).update(expires_at=now - dt.timedelta(seconds=1))
        services.sweep()
    else:
        CapabilityLease.objects.filter(pk=lid).update(last_heartbeat_at=now - dt.timedelta(seconds=901))
        services.sweep()
    assert CapabilityLease.objects.get(pk=lid).status in CapabilityLease.TERMINAL
    r = _json(bot, "get", f"/api/capabilities/leases/{lid}")
    assert r.status_code == 200 and r.json()["slot_data"] is None


def test_a_slot_without_data_delivers_null(pool, tenancy):
    lease = _take(_pat(tenancy["bot"])).json()
    assert lease["status"] == "ready" and lease["slot_data"] is None
    assert not Event.objects.filter(kind="capability.slot.secret_delivered").exists()


def test_public_only_data_is_delivered_without_a_secret_event(pool, tenancy):
    _put(pool, public={"phone": "+1"})
    lease = _take(_pat(tenancy["bot"])).json()
    assert lease["slot_data"] == {"public": {"phone": "+1"}, "secret": {}}
    assert not Event.objects.filter(kind="capability.slot.secret_delivered").exists()


def test_data_for_a_slot_no_longer_declared_is_kept_but_never_delivered(pool, tenancy):
    _seed(pool, "worker-4")
    bot = _pat(tenancy["bot"])
    lease = _take(bot, params={"slot": "worker-4"}).json()
    assert lease["slot_data"]["secret"]["pin"] == PIN
    assert _publish(pool, {**POOL, "slots": ["worker-1", "worker-2", "worker-3"]}).status_code == 200
    assert CapabilitySlotData.objects.filter(slot="worker-4").exists()  # survives the republish
    r = _json(bot, "get", f"/api/capabilities/leases/{lease['lease_id']}")
    assert r.json()["status"] == "ready" and r.json()["slot_data"] is None
    assert [s["slot"] for s in pool.get(SLOTS).json()["slots"]] == ["worker-1", "worker-2", "worker-3"]
    assert _put(pool, "worker-4", public={"a": "b"}).status_code == 422
    assert pool.delete(f"{SLOTS}/worker-4").status_code == 204  # but it can be removed
    assert not CapabilitySlotData.objects.exists()


def test_data_survives_a_republish_of_the_same_slots(pool, tenancy):
    _seed(pool)
    assert _publish(pool, {**POOL, "description": "changed"}).status_code == 200
    assert _take(_pat(tenancy["bot"])).json()["slot_data"]["secret"]["pin"] == PIN


# ---- never logged -------------------------------------------------------------

def test_no_secret_in_events_or_the_settings_audit(pool, tenancy):
    _seed(pool)
    _put(pool, secret={"pin": "OTHER-sentinel-2"})
    bot = _pat(tenancy["bot"])
    lease = _take(bot).json()
    _json(bot, "get", f"/api/capabilities/leases/{lease['lease_id']}")
    bot.delete(f"/api/capabilities/leases/{lease['lease_id']}")
    pool.delete(f"{SLOTS}/worker-1")
    text = _events_text()
    _no_secret_in(text)
    assert "OTHER-sentinel-2" not in text
    kinds = set(Event.objects.values_list("kind", flat=True))
    assert {"capability.slot.data_set", "capability.slot.data_deleted",
            "capability.slot.secret_delivered"} <= kinds
    changes = SettingChange.objects.filter(operation__in=["set_capability_slot_data",
                                                          "delete_capability_slot_data"])
    assert changes.count() == 3
    audit_text = json.dumps([[c.request, c.changes, c.summary] for c in changes], default=str)
    _no_secret_in(audit_text)
    assert "OTHER-sentinel-2" not in audit_text
    assert '"secret_keys"' in audit_text  # the masked before/after names the keys
    # A rotation keeps the key names, yet is still a recorded change.
    assert '"secret_keys_written": ["pin"]' in audit_text


# ---- MCP ----------------------------------------------------------------------

@contextlib.contextmanager
def _as_mcp(user):
    access = AccessToken(token="t", client_id="c", scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "auth_method": "pat"})
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def test_the_slot_routes_are_mcp_tools_and_the_lease_tool_delivers(pool, tenancy):
    from apps.mcp.models import MCPAuditLog
    from apps.mcp.server import mcp

    def call(name, args):
        return async_to_sync(mcp.call_tool)(name, args).structured_content

    tools = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"set_capability_slot_data", "list_capability_slot_data", "delete_capability_slot_data",
            "get_capability_lease"} <= tools
    with _as_mcp(tenancy["bot"]):
        out = call("set_capability_slot_data", {"slug": "ace", "name": "test-worker", "slot": "worker-2",
                                                "public": {"phone": "+2"}, "secret": {"pin": PIN}})
        assert out["secret_keys"] == ["pin"]
        listing = call("list_capability_slot_data", {"slug": "ace", "name": "test-worker"})
        lease = call("acquire_capability_lease", {"agent": "ace", "capability": "test-worker",
                                                  "params": {"slot": "worker-2"}})
        got = call("get_capability_lease", {"lease_id": lease["lease_id"]})
    _no_secret_in(json.dumps(listing))
    assert got["slot_data"] == {"public": {"phone": "+2"}, "secret": {"pin": PIN}}
    _no_secret_in(json.dumps(list(MCPAuditLog.objects.values_list("args_summary", "error"))))
