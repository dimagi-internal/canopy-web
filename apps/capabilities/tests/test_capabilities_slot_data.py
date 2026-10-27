"""A `pool` slot's data: public values and a sealed, write-only secret half.

The rules (docs/architecture/capabilities.md § 6, "Slot data"): a workspace admin
or the agent's own login writes it per key; any member reads it MASKED (secret key
names, never values — admins included); a secret value is stored only SEALED to
the agent's readers (`zks1`) — canopy-web cannot open it — and leaves canopy-web,
still sealed, only on the lease read of the user who acquired a READY lease on
that slot; each such delivery is an event that never carries a value. Nothing —
events, the settings audit, the MCP call log, errors, the DB column — holds a
secret in clear.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import connection
from django.utils import timezone
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.credentials import vault_engine, zk_seal
from apps.capabilities import aws, services, slot_data
from apps.capabilities.models import CapabilityLease, CapabilitySlotData
from apps.events.models import Event, SettingChange
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerAssignment
from apps.passkeys.models import UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member
from tests.fixtures.machines import a_machine

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
ZK_DIR = Path(__file__).resolve().parents[3] / "docs" / "vault" / "zk-v1"


def _ref():
    spec = importlib.util.spec_from_file_location("zk_v1_reference", ZK_DIR / "zk_v1_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ref = _ref()


def _vault_key(user):
    """A vault key for `user` (the agent's owner, so a reader of its vault); its private half."""
    priv = ec.generate_private_key(ec.SECP256R1())
    UserVaultKeyset.objects.create(user=user, public_jwk=ref.jwk(priv.public_key()), seals=[])
    return priv


def _open(tenancy, delivered: dict, slot="worker-1", cap="test-worker") -> dict:
    """What the holder's machine does: open each sealed value — here with the owner's key."""
    owner = tenancy["owner"]
    return {k: ref.open_sealed_value(v, delivered["agent_id"], f"capability-slot:{cap}/{slot}/{k}",
                                     "member", str(owner.pk), tenancy["owner_key"])
            for k, v in delivered["sealed"].items()}


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
    tenancy["owner_key"] = _vault_key(tenancy["owner"])
    return c


def _put(c, slot="worker-1", **body):
    grant_step_up(c)   # a browser writing a slot's secret steps up; a token is left alone
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
    grant_step_up(c)
    assert _json(c, "put", f"{base}/worker-1", {"public": {"a": "b"}}).status_code == 422
    assert c.get(base).status_code == 422
    assert c.delete(f"{base}/worker-1").status_code == 422


def test_a_browser_writes_a_slot_secret_only_after_a_passkey_step_up(tenancy, pool):
    c = _as(tenancy["owner"])
    r = _json(c, "put", f"{SLOTS}/worker-1", {"secret": {"pin": PIN}})
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    assert not CapabilitySlotData.objects.exists()
    assert _json(_pat(tenancy["owner"]), "put", f"{SLOTS}/worker-1", {"secret": {"pin": PIN}}).status_code == 200


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
    assert len(rows) == 4 and len({r.secret_zk["pin"] for r in rows.values()}) == 4


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
                                "updated_at": None, "updated_by": None,
                                "held": False, "lease_id": None}


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


# ---- sealed at rest ------------------------------------------------------------

def test_the_secret_is_stored_sealed_to_the_agents_readers(pool, tenancy):
    _seed(pool)
    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM {CapabilitySlotData._meta.db_table}")
        raw = json.dumps(cur.fetchall(), default=str)
    _no_secret_in(raw)
    row = CapabilitySlotData.objects.get()
    _no_secret_in(str(row) + repr(row))
    assert sorted(row.secret_zk) == ["backup_code", "pin"]
    assert all(v.startswith("zks1:") for v in row.secret_zk.values())
    # Sealed to the owner's vault key, under the slot's name — not to anything canopy-web holds.
    opened = _open(tenancy, {"agent_id": str(tenancy["agent"].pk), "sealed": row.secret_zk})
    assert opened == {"pin": PIN, "backup_code": BACKUP}


def test_a_value_sealed_by_the_writer_is_stored_as_sent(pool, tenancy):
    agent, owner = tenancy["agent"], tenancy["owner"]
    sealed = ref.seal_value(PIN, str(agent.pk), "capability-slot:test-worker/worker-1/pin", agent.zk_kv,
                            [("member", str(owner.pk), tenancy["owner_key"].public_key())],
                            [ec.generate_private_key(ec.SECP256R1())], [os.urandom(12)])
    r = _put(pool, secret={"pin": sealed})
    assert r.status_code == 200, r.content
    assert CapabilitySlotData.objects.get().secret_zk == {"pin": sealed}


def test_a_malformed_sealed_value_is_refused(pool):
    r = _put(pool, secret={"pin": "zks1:not-json"})
    assert r.status_code == 422, r.content
    assert not CapabilitySlotData.objects.exists()


def test_nobody_to_seal_to_is_409_and_nothing_is_stored(pool, tenancy):
    UserVaultKeyset.objects.all().delete()
    r = _put(pool, public={"phone": "+1"}, secret={"pin": PIN})
    assert r.status_code == 409, r.content
    _no_secret_in(r.content.decode())
    assert not CapabilitySlotData.objects.exists()
    assert _put(pool, public={"phone": "+1"}).status_code == 200  # public data needs no reader


# ---- delivery ----------------------------------------------------------------

def test_the_acquirer_gets_the_slot_data_on_acquire_and_on_get(pool, tenancy):
    _seed(pool)
    bot = _pat(tenancy["bot"])
    lease = _take(bot).json()
    assert lease["status"] == "ready" and lease["slot"] == "worker-1"
    stored = CapabilitySlotData.objects.get().secret_zk
    want = {"public": {"phone": "+74260000001", "name": "Test Worker One"}, "sealed": stored,
            "agent_id": str(tenancy["agent"].pk)}
    assert lease["slot_data"] == want
    _no_secret_in(json.dumps(lease))
    assert _open(tenancy, lease["slot_data"]) == {"pin": PIN, "backup_code": BACKUP}
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
    assert got["status"] == "ready" and _open(tenancy, got["slot_data"])["pin"] == PIN


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
    assert _open(tenancy, lease["slot_data"])["pin"] == PIN
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
    assert lease["slot_data"] == {"public": {"phone": "+1"}, "sealed": {}, "agent_id": str(tenancy["agent"].pk)}
    assert not Event.objects.filter(kind="capability.slot.secret_delivered").exists()


def test_data_for_a_slot_no_longer_declared_is_kept_but_never_delivered(pool, tenancy):
    _seed(pool, "worker-4")
    bot = _pat(tenancy["bot"])
    lease = _take(bot, params={"slot": "worker-4"}).json()
    assert _open(tenancy, lease["slot_data"], "worker-4")["pin"] == PIN
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
    assert _open(tenancy, _take(_pat(tenancy["bot"])).json()["slot_data"])["pin"] == PIN


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
    _no_secret_in(json.dumps(got))
    assert got["slot_data"]["public"] == {"phone": "+2"}
    assert _open(tenancy, got["slot_data"], "worker-2") == {"pin": PIN}
    _no_secret_in(json.dumps(list(MCPAuditLog.objects.values_list("args_summary", "error"))))


# ---- the keeper: a sealed value follows the agent's readers -----------------------------

PIN_NAME = "capability-slot:test-worker/worker-1/pin"


def _box(tenancy, name="cloud-1"):
    """A cloud runner of the owner's that runs ace."""
    r = Runner.objects.create(name=name, kind=Runner.CLOUD, owner=tenancy["owner"],
                              workspace=tenancy["ws"], last_heartbeat_at=timezone.now(),
                              status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=tenancy["agent"], runner=r, rank=0)
    return r


def _opened_by(value, tenancy, key, ecdh, name=PIN_NAME):
    return ref.open_sealed_value(value, str(tenancy["agent"].pk), name, "device", key.ecdh_jkt, ecdh)


def _keeper(c, runner):
    r = c.get(f"/api/harness/runners/{runner.pk}/vault-keeper/slot-secrets")
    assert r.status_code == 200, r.content
    return r.json()["slot_secrets"]


def _reseal_to(value, tenancy, opener, to, name=PIN_NAME):
    key, ecdh = opener
    plain = _opened_by(value, tenancy, key, ecdh, name)
    return zk_seal.seal_to(plain, agent_id=str(tenancy["agent"].pk), name=name, kv=tenancy["agent"].zk_kv,
                           recipients=to)


def test_a_box_that_holds_a_slot_secret_reseals_it_for_a_machine_approved_later(pool, tenancy):
    box = _box(tenancy)
    key, _, ecdh = a_machine(box, tenancy["owner"])
    _seed(pool)
    assert _keeper(pool, box) == []                         # nobody waits yet
    new, _, new_ecdh = a_machine(box, tenancy["owner"])
    work = {w["name"]: w for w in _keeper(pool, box)}
    assert sorted(work) == ["capability-slot:test-worker/worker-1/backup_code", PIN_NAME]
    pin = work[PIN_NAME]
    assert pin["waiting"] == [new.ecdh_jkt] and pin["device_jkt"] == key.ecdh_jkt
    _no_secret_in(json.dumps(pin))

    everyone = [(r["kind"], r["target"], r["public_jwk"]) for r in pin["recipients"]]
    value = _reseal_to(pin["value_zk"], tenancy, (key, ecdh), everyone)
    r = _json(pool, "put", pin["reseal_path"], {"value_zk": value, "expect": pin["expect"]})
    assert r.status_code == 200, r.content
    assert r.json()["machines"] == ["cloud-1"]
    row = CapabilitySlotData.objects.get()
    assert _opened_by(row.secret_zk["pin"], tenancy, new, new_ecdh) == PIN
    assert [w["name"] for w in _keeper(pool, box)] == ["capability-slot:test-worker/worker-1/backup_code"]
    assert Event.objects.filter(kind="capability.slot.secret_resealed").exists()
    _no_secret_in(_events_text())

    r = _json(pool, "put", pin["reseal_path"], {"value_zk": value, "expect": pin["expect"]})
    assert r.status_code == 409 and r.json()["error"] == "zk_value_changed"   # stale


def test_a_reseal_to_a_stranger_or_dropping_a_reader_is_refused(pool, tenancy):
    box = _box(tenancy)
    key, _, ecdh = a_machine(box, tenancy["owner"])
    _seed(pool)
    stored = CapabilitySlotData.objects.get().secret_zk["pin"]
    expect = vault_engine.value_digest(stored)
    url = f"/api/harness/runners/{box.pk}/vault-keeper/ace/slot-secrets/test-worker/worker-1/pin"
    theirs = Runner.objects.create(name="out-box", kind=Runner.CLOUD, owner=tenancy["outsider"],
                                   status=Runner.ONLINE)
    other, _, _ = a_machine(theirs, tenancy["outsider"])
    mine = ("device", key.ecdh_jkt, key.ecdh_public_jwk)
    stranger = ("device", other.ecdh_jkt, other.ecdh_public_jwk)
    r = _json(pool, "put", url, {"value_zk": _reseal_to(stored, tenancy, (key, ecdh), [mine, stranger]),
                                 "expect": expect})
    assert r.status_code == 422 and r.json()["error"] == "zk_invalid_target"
    r = _json(pool, "put", url, {"value_zk": _reseal_to(stored, tenancy, (key, ecdh), [mine]),
                                 "expect": expect})
    assert r.status_code == 422 and r.json()["error"] == "zk_rewrap_required"   # drops the owner's key
    other_box = _box(tenancy, "cloud-2")                     # a box with no copy is no keeper
    r = _json(pool, "put", url.replace(str(box.pk), str(other_box.pk)),
              {"value_zk": _reseal_to(stored, tenancy, (key, ecdh), [mine]), "expect": expect})
    assert r.status_code == 403 and r.json()["error"] == "zk_not_a_holder"
    assert CapabilitySlotData.objects.get().secret_zk["pin"] == stored


def test_a_revoked_machine_is_dropped_from_every_slot_secret_without_decrypting(pool, tenancy):
    box = _box(tenancy)
    key, _, ecdh = a_machine(box, tenancy["owner"])
    gone, _, _ = a_machine(box, tenancy["owner"])
    _seed(pool)
    assert ("device", gone.ecdh_jkt) in zk_seal.sealed_targets(CapabilitySlotData.objects.get().secret_zk["pin"])
    assert slot_data.prune() == {}                          # nothing to drop yet
    device_keys.revoke(gone, by=tenancy["owner"])
    assert slot_data.prune() == {"ace/test-worker/worker-1/backup_code": [gone.ecdh_jkt],
                                 "ace/test-worker/worker-1/pin": [gone.ecdh_jkt]}
    row = CapabilitySlotData.objects.get()
    assert ("device", gone.ecdh_jkt) not in zk_seal.sealed_targets(row.secret_zk["pin"])
    assert _opened_by(row.secret_zk["pin"], tenancy, key, ecdh) == PIN   # the readers left still open it
    assert slot_data.prune() == {}
