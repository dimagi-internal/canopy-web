"""`kind: pool` — an exclusive checkout of a NAMED SLOT, with no machine.

The same grants, queue, heartbeat, idle/expiry/turn release and events as an
ec2-ephemeral capability, but a lease holds a slot: it is `ready` the moment a
slot is free, and AWS is never called. Every test here runs with `aws.get`
replaced by a function that fails the test, so "no AWS call" is checked by
every route the test touches, not only the sweep.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.capabilities import aws, services
from apps.capabilities.models import AgentCapability, CapabilityLease
from apps.events.models import Event
from apps.harness.models import Turn

from .helpers import MANIFEST, _as, _json, _kinds, _pat, _publish

pytestmark = pytest.mark.django_db

POOL = {
    "version": 1, "name": "test-worker", "kind": "pool",
    "description": "Connect test workers, one device session each.",
    "slots": ["worker-1", "worker-2", "worker-3", "worker-4"],
    "limits": {"max_lease_s": 14400, "idle_release_s": 900},
}
SLOTS = POOL["slots"]


def _no_aws(region):
    raise AssertionError(f"a pool capability must never call AWS (aws.get({region!r}))")


@pytest.fixture()
def pool(tenancy, monkeypatch):
    """ace declares and is granted the test-worker pool; AWS is off-limits."""
    monkeypatch.setattr(aws, "get", _no_aws)
    c = _as(tenancy["owner"])
    r = _publish(c, POOL)
    assert r.status_code == 200, r.content
    assert _grant_pool(c).status_code == 200
    return c


def _grant_pool(c, **limits):
    return _json(c, "put", "/api/capabilities/grants", {"agent": "ace", "capability": "test-worker", **limits})


def _take(c, **extra):
    return _json(c, "post", "/api/capabilities/leases", {"agent": "ace", "capability": "test-worker", **extra})


def _holders() -> dict[str, str]:
    return dict(CapabilityLease.objects.filter(capability__name="test-worker", status__in=CapabilityLease.LIVE)
                .exclude(slot="").values_list("slot", "pk"))


# ---- the manifest and its publish --------------------------------------------

def test_a_pool_publishes_without_a_stack(pool):
    body = _json(pool, "get", "/api/agents/ace/capabilities").json()
    (cap,) = body["capabilities"]
    assert (cap["kind"], cap["stack"], cap["region"], cap["stack_outputs"]) == ("pool", "", "", {})
    assert cap["manifest"]["slots"] == SLOTS
    assert cap["limits"]["max_concurrent"] == 4  # the slot count, not the default 1
    assert Event.objects.filter(kind="capability.published", summary__contains="slots worker-1").exists()


def test_a_pool_is_published_beside_an_ec2_capability(tenancy, world):
    r = _publish(_as(tenancy["owner"]), MANIFEST, POOL)
    assert r.status_code == 200, r.content
    assert {c["name"]: c["kind"] for c in r.json()["capabilities"]} == {
        "android-emulator": "ec2-ephemeral", "test-worker": "pool"}


def test_a_pool_manifest_is_validated_by_the_shared_schema(tenancy, monkeypatch):
    monkeypatch.setattr(aws, "get", _no_aws)
    c = _as(tenancy["owner"])
    assert _publish(c, {**POOL, "slots": ["worker-1", "worker-1"]}).status_code == 422
    assert _publish(c, {**POOL, "aws": MANIFEST["aws"]}).status_code == 422
    assert _publish(c, {**POOL, "params": {"slot": {"type": "string"}}}).status_code == 422


def test_refresh_and_terminate_orphans_say_a_pool_has_no_stack(pool):
    r = _json(pool, "post", "/api/agents/ace/capabilities/test-worker/refresh")
    assert r.status_code == 422 and "no stack" in r.json()["detail"]
    r = _json(pool, "post", "/api/agents/ace/capabilities/test-worker/terminate-orphans")
    assert r.status_code == 422 and "no machines" in r.json()["detail"]


# ---- acquire: a free slot is ready at once -----------------------------------

def test_acquire_holds_the_first_free_slot_at_once(pool):
    r = _take(pool)
    assert r.status_code == 200, r.content
    lease = r.json()
    assert (lease["status"], lease["ready"], lease["position"]) == ("ready", True, None)
    assert lease["slot"] == "worker-1"
    assert lease["params"] == {"slot": "worker-1"} and lease["ready_info"] == {"slot": "worker-1"}
    assert (lease["instance_id"], lease["out_prefix"]) == (None, "")
    assert lease["expires_at"] and lease["provisioning_at"] and lease["ready_at"]
    assert _kinds(lease["lease_id"]) == ["capability.lease.queued", "capability.lease.ready"]
    assert _take(pool).json()["slot"] == "worker-2"  # declared order


def test_a_requested_slot_is_held_exactly(pool):
    lease = _take(pool, params={"slot": "worker-3"}).json()
    assert (lease["status"], lease["slot"]) == ("ready", "worker-3")
    assert _take(pool).json()["slot"] == "worker-1"


def test_an_acquire_names_only_a_declared_slot(pool):
    r = _take(pool, params={"slot": "worker-9"})
    assert r.status_code == 422 and "not one of this pool's slots" in r.json()["detail"]
    r = _take(pool, params={"state": "cc-2.63.0"})
    assert r.status_code == 422 and "takes only 'slot'" in r.json()["detail"]
    assert not CapabilityLease.objects.exists()


# ---- exhaustion → queue → promotion ------------------------------------------

def test_an_exhausted_pool_queues_and_a_release_hands_the_slot_on_at_once(pool):
    held = [_take(pool).json() for _ in SLOTS]
    assert [h["slot"] for h in held] == SLOTS
    first, second = _take(pool).json(), _take(pool).json()
    assert (first["status"], first["position"], first["slot"]) == ("queued", 1, None)
    assert (second["status"], second["position"]) == ("queued", 2)

    released = pool.delete(f"/api/capabilities/leases/{held[2]['lease_id']}").json()
    assert (released["status"], released["slot"]) == ("released", "worker-3")
    # In the same request (one transaction): the oldest queued lease holds it now.
    got = CapabilityLease.objects.get(pk=first["lease_id"])
    assert (got.status, got.slot, got.ready_info) == ("ready", "worker-3", {"slot": "worker-3"})
    assert _json(pool, "get", f"/api/capabilities/leases/{second['lease_id']}").json()["position"] == 1


def test_a_long_poll_returns_when_the_queued_lease_gets_its_slot(pool, monkeypatch):
    held = [_take(pool).json() for _ in SLOTS]
    waiting = _take(pool).json()
    sleeps = []

    def release_one(_s):
        sleeps.append(1)
        if len(sleeps) == 2:  # another holder lets go while we wait
            services.release(CapabilityLease.objects.get(pk=held[0]["lease_id"]))

    monkeypatch.setattr(services, "_sleep", release_one)
    got = _json(pool, "get", f"/api/capabilities/leases/{waiting['lease_id']}?wait_s=10").json()
    assert (got["status"], got["slot"]) == ("ready", "worker-1")
    assert len(sleeps) == 2


def test_a_lease_waiting_for_a_held_slot_does_not_block_those_behind_it(pool):
    holders = {s: _take(pool, params={"slot": s}).json() for s in ("worker-2", "worker-3", "worker-4")}
    wants_2 = _take(pool, params={"slot": "worker-2"}).json()
    assert (wants_2["status"], wants_2["position"]) == ("queued", 1)
    anyone = _take(pool).json()
    assert (anyone["status"], anyone["slot"]) == ("ready", "worker-1")  # took the free one past it

    pool.delete(f"/api/capabilities/leases/{anyone['lease_id']}")  # worker-1 frees: not what it asked for
    assert CapabilityLease.objects.get(pk=wants_2["lease_id"]).status == "queued"
    pool.delete(f"/api/capabilities/leases/{holders['worker-2']['lease_id']}")
    got = CapabilityLease.objects.get(pk=wants_2["lease_id"])
    assert (got.status, got.slot) == ("ready", "worker-2")


def test_a_grant_may_lower_a_pools_concurrency(pool):
    assert _grant_pool(pool, max_concurrent=5).status_code == 422  # above the slot count
    assert _grant_pool(pool, max_concurrent=2).status_code == 200
    statuses = [_take(pool).json()["status"] for _ in range(3)]
    assert statuses == ["ready", "ready", "queued"]


# ---- never two live leases on one slot ---------------------------------------

def test_no_slot_is_ever_held_twice(pool):
    leases = [_take(pool).json() for _ in range(10)]
    ready = [lease for lease in leases if lease["status"] == "ready"]
    assert sorted(lease["slot"] for lease in ready) == SLOTS
    assert [lease["status"] for lease in leases[4:]] == ["queued"] * 6
    for lease in ready[:2]:
        pool.delete(f"/api/capabilities/leases/{lease['lease_id']}")
    services.sweep()
    live = CapabilityLease.objects.filter(status="ready")
    assert sorted(live.values_list("slot", flat=True)) == SLOTS


def test_the_database_refuses_a_second_live_lease_on_a_slot(pool):
    holder = CapabilityLease.objects.get(pk=_take(pool).json()["lease_id"])
    dup = CapabilityLease(workspace_id=holder.workspace_id, agent=holder.agent, capability=holder.capability,
                          status=CapabilityLease.READY, slot=holder.slot, max_lease_s=60, idle_release_s=60,
                          ready_timeout_s=60, last_heartbeat_at=timezone.now())
    with pytest.raises(IntegrityError), transaction.atomic():
        dup.save()
    # An ENDED lease on the slot is history, not a hold.
    CapabilityLease.objects.filter(pk=holder.pk).update(status=CapabilityLease.RELEASED)
    dup.save()


def test_a_slot_taken_behind_the_brokers_back_is_not_handed_out_twice(pool, monkeypatch, caplog):
    """Another writer takes worker-1 after the broker read who holds what: the
    constraint refuses the second assignment and the lease stays queued."""
    real = services.effective_limits
    cap = AgentCapability.objects.get(name="test-worker")
    calls = []

    def racing(c, g):
        calls.append(1)
        # Call 1 is acquire's (fixing the lease's limits); call 2 is inside the
        # slot assignment, AFTER it read who holds what — the race window.
        if len(calls) == 2:
            CapabilityLease.objects.create(
                workspace_id=cap.agent.workspace_id, agent=cap.agent, capability=cap,
                status=CapabilityLease.READY, slot="worker-1", max_lease_s=60, idle_release_s=60,
                ready_timeout_s=60, last_heartbeat_at=timezone.now())
        return real(c, g)

    monkeypatch.setattr(services, "effective_limits", racing)
    with caplog.at_level("WARNING", logger="apps.capabilities.services"):
        lease = _take(pool).json()
    assert len(calls) == 2
    assert "already held (constraint)" in caplog.text
    assert CapabilityLease.objects.filter(slot="worker-1", status="ready").count() == 1
    # The broker moved on: the lease stays queued for the next free slot.
    assert (lease["status"], lease["slot"]) == ("queued", None)
    services.sweep()
    assert CapabilityLease.objects.get(pk=lease["lease_id"]).slot == "worker-2"


# ---- the guards free the slot -------------------------------------------------

def test_idle_and_expiry_free_the_slot_for_the_queue(pool):
    held = [_take(pool).json() for _ in SLOTS]
    queued = [_take(pool).json() for _ in range(2)]
    now = timezone.now()
    CapabilityLease.objects.filter(pk=held[0]["lease_id"]).update(expires_at=now - dt.timedelta(seconds=1))
    CapabilityLease.objects.filter(pk=held[1]["lease_id"]).update(
        last_heartbeat_at=now - dt.timedelta(seconds=901))
    counts = services.sweep()
    assert (counts["expired"], counts["idle"], counts["errors"]) == (1, 1, 0)
    assert CapabilityLease.objects.get(pk=held[0]["lease_id"]).status == "expired"
    assert CapabilityLease.objects.get(pk=held[1]["lease_id"]).status == "released"
    assert sorted(CapabilityLease.objects.get(pk=q["lease_id"]).slot for q in queued) == ["worker-1", "worker-2"]


def test_the_sweep_promotes_a_pools_queue_without_aws(pool):
    """A queue left behind (a release whose promote never ran) is picked up by
    the sweep — with aws.get failing the test if anything reaches for AWS."""
    held = _take(pool).json()
    for s in SLOTS[1:]:
        _take(pool, params={"slot": s})
    waiting = _take(pool).json()
    CapabilityLease.objects.filter(pk=held["lease_id"]).update(status="released", ended_at=timezone.now())
    counts = services.sweep()
    assert (counts["promoted"], counts["errors"]) == (1, 0)
    assert CapabilityLease.objects.get(pk=waiting["lease_id"]).slot == "worker-1"


def test_a_turn_bound_pool_lease_frees_its_slot_when_the_turn_ends(pool, tenancy,
                                                                   django_capture_on_commit_callbacks):
    from apps.harness import services as hsvc

    turn = Turn.objects.create(agent=tenancy["agent"], prompt="phase 6", status=Turn.RUNNING)
    lease = _take(pool, turn_id=str(turn.pk)).json()
    with django_capture_on_commit_callbacks(execute=True):
        hsvc.finish_turn(turn, status=Turn.DONE)
    got = CapabilityLease.objects.get(pk=lease["lease_id"])
    assert (got.status, got.reason) == ("released", "its turn ended")
    assert _holders() == {}


def test_force_release_and_grant_revocation_free_slots(pool, tenancy):
    a, b = _take(pool).json(), _take(pool).json()
    r = _json(pool, "post", f"/api/capabilities/leases/{a['lease_id']}/force-release", {"reason": "stuck"})
    assert (r.status_code, r.json()["status"]) == (200, "released")
    grant_id = _json(pool, "get", "/api/capabilities/grants").json()[0]["id"]
    assert pool.delete(f"/api/capabilities/grants/{grant_id}").status_code == 204
    assert CapabilityLease.objects.get(pk=b["lease_id"]).status == "released"
    assert _holders() == {}


# ---- republishing slots ------------------------------------------------------

def test_a_republish_that_removes_a_held_slot_lets_the_lease_run_out(pool):
    on_4 = _take(pool, params={"slot": "worker-4"}).json()
    wants_4 = _take(pool, params={"slot": "worker-4"}).json()
    r = _publish(pool, {**POOL, "slots": ["worker-1", "worker-2", "worker-3"]})
    assert r.status_code == 200, r.content
    # The live lease keeps worker-4; the one waiting for it can never get it.
    assert CapabilityLease.objects.get(pk=on_4["lease_id"]).status == "ready"
    failed = CapabilityLease.objects.get(pk=wants_4["lease_id"])
    assert failed.status == "failed" and "no longer one of this pool's slots" in failed.reason
    pool.delete(f"/api/capabilities/leases/{on_4['lease_id']}")
    assert sorted(_take(pool).json()["slot"] for _ in range(3)) == ["worker-1", "worker-2", "worker-3"]
    assert _take(pool).json()["status"] == "queued"  # worker-4 is never handed out again


def test_changing_a_capabilitys_kind_ends_its_old_leases_the_old_way(tenancy, world):
    """ec2-ephemeral → pool under the same name: the live lease's MACHINE is
    terminated (it is ended as the kind it was), and the pool starts empty."""
    c = _as(tenancy["owner"])
    _publish(c, MANIFEST)
    _json(c, "put", "/api/capabilities/grants", {"agent": "ace", "capability": "android-emulator"})
    machine = _json(c, "post", "/api/capabilities/leases",
                    {"agent": "ace", "capability": "android-emulator"}).json()
    as_pool = {**POOL, "name": "android-emulator"}
    assert _publish(c, as_pool).status_code == 200
    got = CapabilityLease.objects.get(pk=machine["lease_id"])
    assert got.status == "released" and "kind changed" in got.reason
    assert machine["instance_id"] in world.terminated
    lease = _json(c, "post", "/api/capabilities/leases", {"agent": "ace", "capability": "android-emulator"}).json()
    assert (lease["status"], lease["slot"]) == ("ready", "worker-1")


# ---- no machine: exec, uploads, artifacts ------------------------------------

def test_machine_routes_refuse_a_pool_lease(pool):
    lease = _take(pool).json()["lease_id"]
    for method, path, body in (
        ("post", f"/api/capabilities/leases/{lease}/exec", {"argv": ["ace-mobile"]}),
        ("get", f"/api/capabilities/leases/{lease}/exec/00000000-0000-4000-8000-000000000001", None),
        ("post", f"/api/capabilities/leases/{lease}/uploads", {"names": ["a.apk"]}),
        ("get", f"/api/capabilities/leases/{lease}/artifacts", None),
    ):
        r = _json(pool, method, path, body)
        assert r.status_code == 422, (path, r.content)
        assert "pool capability" in r.json()["detail"] and "not a machine" in r.json()["detail"]


# ---- who holds which slot ----------------------------------------------------

def test_the_lease_list_shows_who_holds_which_slot(pool, tenancy):
    mine = _take(pool).json()
    theirs = _take(_pat(tenancy["bot"])).json()
    queued_count = len(SLOTS) - 2
    for _ in range(queued_count + 1):
        _take(pool)
    rows = _json(pool, "get", "/api/capabilities/leases?capability=test-worker&status=live").json()["leases"]
    assert {r["lease_id"]: r["slot"] for r in rows if r["status"] == "ready"}[mine["lease_id"]] == "worker-1"
    assert {r["lease_id"]: r["slot"] for r in rows}[theirs["lease_id"]] == "worker-2"
    assert sum(r["status"] == "queued" for r in rows) == 1
    live = _json(pool, "get", "/api/agents/ace/capabilities").json()["capabilities"][0]["live_leases"]
    assert sorted(r["slot"] for r in live if r["slot"]) == SLOTS
