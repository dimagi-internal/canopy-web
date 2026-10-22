"""The capability broker's cost and security guards, and its operator surface.

What the 2026-10-14 audit found missing or unproven, each pinned here: the sweep
off-request (fleet hold, no runner, Redis down, failures as events); leases that
outlive their agent; a running exec holding its lease awake; per-machine object
prefixes and the exec document on a 0.9.0 stack; long-poll; the broker tag; the
queue not lagging on a just-ended lease; argv kept out of the event log; the
list, spend, force-release, orphans-now and refresh routes.
"""
from __future__ import annotations

import base64
import datetime as dt

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.capabilities import scheduler, services
from apps.capabilities.models import AgentCapability, CapabilityLease
from apps.events.models import Event
from apps.harness.models import Turn

from .helpers import MANIFEST, _acquire, _as, _grant, _json, _pat, _publish

pytestmark = pytest.mark.django_db


@pytest.fixture()
def no_sleep(monkeypatch):
    """Long-polls without real time: each sleep advances a fake clock by POLL_S."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(services, "_monotonic", lambda: clock["t"])

    def sleep(s):
        clock["t"] += s

    monkeypatch.setattr(services, "_sleep", sleep)
    return clock


@pytest.fixture()
def v09(tenancy, world):
    """A 0.9.0 stack (per-instance prefixes, the exec document), published and granted."""
    world.v09()
    c = _as(tenancy["owner"])
    assert _publish(c).status_code == 200
    assert _grant(c).status_code == 200
    return c


def _lease(lid) -> CapabilityLease:
    return CapabilityLease.objects.get(pk=lid)


def _ready(c, world, **acquire):
    lease = _acquire(c, **acquire).json()
    world.ready(lease["instance_id"])
    return _json(c, "get", f"/api/capabilities/leases/{lease['lease_id']}").json()


# ---- the sweep runs off-request, whatever the runners are doing ----------------

def test_the_scheduler_sweeps_with_no_runner_at_all(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    CapabilityLease.objects.filter(pk=lease["lease_id"]).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    scheduler.run()  # the registered off-request job, every CAPABILITY_SWEEP_INTERVAL_S
    assert _lease(lease["lease_id"]).status == "expired"
    assert lease["instance_id"] in world.terminated


def test_the_sweep_is_a_registered_off_request_job(settings):
    from apps.common import ticks

    job = ticks.jobs()["capabilities.sweep"]
    settings.CAPABILITY_SWEEP_INTERVAL_S = 30
    assert job.interval() == 30 and job.dedupe is False  # sweep_tick holds its own cache lock
    settings.CAPABILITY_SWEEP_INTERVAL_S = 0
    assert job.interval() == 0  # 0 turns it off


async def test_the_tick_loop_runs_until_the_lifespan_closes(monkeypatch, settings):
    import asyncio

    from apps.common import ticks as clock

    ticks = []
    monkeypatch.setattr(clock, "_tick_off_request", lambda: ticks.append(1))
    monkeypatch.setattr(clock.random, "uniform", lambda a, b: 0)
    settings.TICK_BASE_S = 0.01
    async with clock.running():
        for _ in range(100):
            if len(ticks) >= 2:
                break
            await asyncio.sleep(0.01)
    assert len(ticks) >= 2
    n = len(ticks)
    await asyncio.sleep(0.05)
    assert len(ticks) == n  # cancelled with the lifespan


async def test_a_base_interval_of_zero_starts_no_loop(monkeypatch, settings):
    from apps.common import ticks as clock

    monkeypatch.setattr(clock, "_tick_off_request", lambda: pytest.fail("no loop expected"))
    settings.TICK_BASE_S = 0
    async with clock.running():
        pass


def _runner(owner, **kw):
    from apps.harness.models import Runner

    return Runner.objects.create(name="box", kind=Runner.EMDASH, owner=owner, status=Runner.ONLINE,
                                 last_heartbeat_at=timezone.now(), **kw)


def test_a_fleet_hold_does_not_stop_the_sweep(ready_to_lease, tenancy, monkeypatch):
    from apps.harness import claim
    from apps.harness.models import FleetHold

    FleetHold.objects.update_or_create(pk=FleetHold.SINGLETON_PK, defaults={"held": True})
    ran = []
    monkeypatch.setattr(services, "sweep", lambda: ran.append(1) or {})
    assert claim.claim_next_turn(_runner(tenancy["owner"])) is None  # held: nothing claimed
    assert ran == [1]  # but the capability sweep still ran


def test_an_offline_runner_does_not_stop_the_sweep(ready_to_lease, tenancy, monkeypatch):
    from apps.harness import claim

    ran = []
    monkeypatch.setattr(services, "sweep", lambda: ran.append(1) or {})
    stale = _runner(tenancy["owner"])
    type(stale).objects.filter(pk=stale.pk).update(last_heartbeat_at=timezone.now() - dt.timedelta(days=1))
    stale.refresh_from_db()
    assert claim.claim_next_turn(stale) is None
    assert ran == [1]


def test_the_claim_tick_leaves_the_sweep_to_a_scheduler_that_keeps_up(ready_to_lease, monkeypatch):
    ran = []
    monkeypatch.setattr(services, "sweep", lambda: ran.append(1) or {})
    assert services.sweep_tick(trigger="scheduler") is True
    cache.delete(services.SWEEP_CACHE_KEY)  # due again — but the scheduler just finished one
    assert services.sweep_tick(trigger="claim") is False
    assert ran == [1]
    cache.delete(services.SWEEP_RAN_KEY)  # the scheduler stalled: the claim tick takes over
    assert services.sweep_tick(trigger="claim") is True
    assert ran == [1, 1]


def test_a_redis_failure_does_not_disable_the_sweep(ready_to_lease, monkeypatch):
    ran = []
    monkeypatch.setattr(services, "sweep", lambda: ran.append(1) or {})
    monkeypatch.setattr(services, "_local_last_sweep", 0.0)

    def down(*a, **k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(cache, "add", down)
    monkeypatch.setattr(cache, "get", down)
    monkeypatch.setattr(cache, "set", down)
    assert services.sweep_tick(trigger="scheduler") is True
    assert ran == [1]
    # Throttled on this process's own clock instead.
    assert services.sweep_tick(trigger="scheduler") is False
    assert ran == [1]


def test_a_failing_sweep_is_an_event(ready_to_lease, tenancy, monkeypatch):
    def boom():
        raise RuntimeError("the database went away")

    monkeypatch.setattr(services, "sweep", boom)
    assert services.sweep_tick(trigger="scheduler") is False
    cache.delete(services.SWEEP_CACHE_KEY)
    services.sweep_tick(trigger="scheduler")
    (ev,) = Event.objects.filter(kind="capability.sweep.failed")
    assert ev.level == "error" and ev.workspace_id == "connect" and ev.count == 2  # coalesced
    assert "the database went away" in ev.summary


def test_an_aws_outage_in_the_sweep_is_an_event(ready_to_lease, world):
    from apps.capabilities import aws

    _acquire(ready_to_lease)
    world.list_error = aws.AwsError("throttled")
    services.sweep()
    assert Event.objects.filter(kind="capability.sweep.unreachable", level="warn").exists()


def test_a_lease_the_sweep_cannot_process_is_an_event_and_the_pass_goes_on(ready_to_lease, world, monkeypatch):
    first, second = (_acquire(ready_to_lease).json() for _ in range(2))
    CapabilityLease.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    real_end = services.end

    def end(lease, *a, **k):
        if str(lease.pk) == first["lease_id"]:
            raise RuntimeError("one bad row")
        return real_end(lease, *a, **k)

    monkeypatch.setattr(services, "end", end)
    counts = services.sweep()
    assert counts["errors"] == 1 and counts["expired"] == 1
    assert _lease(second["lease_id"]).status == "expired"
    assert Event.objects.filter(kind="capability.sweep.error", level="error").exists()


def test_the_sweep_releases_a_lease_whose_turn_ended_without_the_signal(ready_to_lease, tenancy, world):
    turn = Turn.objects.create(agent=tenancy["agent"], prompt="phase 6", status=Turn.RUNNING)
    lease = _acquire(ready_to_lease, turn_id=str(turn.pk)).json()
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)  # no ledger row: the receiver never fired
    assert services.sweep()["turn_ended"] == 1
    got = _lease(lease["lease_id"])
    assert got.status == "released" and got.reason == "its turn ended"


def test_the_orphan_pass_covers_a_capability_with_no_recent_lease(ready_to_lease, world):
    """A machine can outlive the lease rows that would lead the sweep to it."""
    orphan = world.add_instance(lease_id="6f1c0000-0000-4000-8000-00000000dead")
    assert not CapabilityLease.objects.exists()
    assert services.sweep()["orphans"] == 1
    assert orphan in world.terminated


# ---- leases do not outlive what they belong to --------------------------------

def test_deleting_the_agent_terminates_its_machines(ready_to_lease, tenancy, world):
    held = _acquire(ready_to_lease).json()
    tenancy["agent"].delete()
    assert not CapabilityLease.objects.exists()
    assert held["instance_id"] in world.terminated


def test_deleting_the_capability_row_terminates_its_machines(ready_to_lease, world):
    """(A workspace cannot be deleted under an agent — Agent.workspace is PROTECT —
    so the agent and the capability are the cascades that reach a lease.)"""
    held = _acquire(ready_to_lease).json()
    AgentCapability.objects.all().delete()
    assert not CapabilityLease.objects.exists()
    assert held["instance_id"] in world.terminated


def test_a_failed_terminate_does_not_block_the_delete(ready_to_lease, tenancy, world):
    from apps.capabilities import aws

    _acquire(ready_to_lease)
    world.terminate_error = aws.AwsError("throttled")
    tenancy["agent"].delete()  # the stack's backstop Lambda covers the machine
    assert not CapabilityLease.objects.exists()


# ---- a running exec keeps its lease awake -------------------------------------

def test_an_exec_in_flight_holds_its_lease_past_idle(ready_to_lease, ready_lease, world):
    lid = ready_lease["lease_id"]
    exec_id = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
                    {"argv": ["ace-mobile", "run-recipe"], "timeout_s": 3600}).json()["exec_id"]
    entry = _lease(lid).execs[exec_id]
    assert entry["timeout_s"] == 3600 and entry["done"] is False and entry["sent_at"]
    # The holder waits on the exec without polling for longer than idle_release_s.
    CapabilityLease.objects.filter(pk=lid).update(last_heartbeat_at=timezone.now() - dt.timedelta(seconds=1000))
    assert services.sweep()["idle"] == 0
    assert _lease(lid).status == "ready"

    # Once SSM says it is done, the exec no longer counts.
    from canopy_sdk.ondemand import Invocation

    iid = ready_lease["instance_id"]
    world.invocations[(exec_id, iid)] = Invocation(exec_id, iid, "Success", 0, "", "")
    _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{exec_id}")
    assert _lease(lid).execs[exec_id]["done"] is True
    CapabilityLease.objects.filter(pk=lid).update(last_heartbeat_at=timezone.now() - dt.timedelta(seconds=1000))
    assert services.sweep()["idle"] == 1


def test_an_exec_past_its_timeout_no_longer_holds_the_lease(ready_to_lease, ready_lease):
    lid = ready_lease["lease_id"]
    old = (timezone.now() - dt.timedelta(seconds=5000)).isoformat()
    CapabilityLease.objects.filter(pk=lid).update(
        execs={"00000000-0000-4000-8000-000000000001": {"sent_at": old, "timeout_s": 600, "done": False}},
        last_heartbeat_at=timezone.now() - dt.timedelta(seconds=1000))
    assert services.sweep()["idle"] == 1


def test_execs_written_before_0_9_still_read(ready_to_lease, ready_lease, world):
    """Old rows hold a bare ISO sent-at: still found, never extending idleness."""
    lid, iid = ready_lease["lease_id"], ready_lease["instance_id"]
    exec_id = "00000000-0000-4000-8000-000000000042"
    from canopy_sdk.ondemand import Invocation

    world.invocations[(exec_id, iid)] = Invocation(exec_id, iid, "Success", 0, "ok", "")
    CapabilityLease.objects.filter(pk=lid).update(execs={exec_id: timezone.now().isoformat()})
    r = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{exec_id}")
    assert r.status_code == 200 and r.json()["done"] is True
    assert _lease(lid).execs[exec_id]["done"] is True


def test_the_exec_timeout_is_capped_at_the_lease_expiry(ready_to_lease, ready_lease, world):
    lid = ready_lease["lease_id"]
    CapabilityLease.objects.filter(pk=lid).update(expires_at=timezone.now() + dt.timedelta(seconds=50))
    body = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
                 {"argv": ["ace-mobile", "diagnose"], "timeout_s": 600}).json()
    assert 45 <= body["timeout_s"] <= 50
    assert world.sent[-1]["timeout_s"] == body["timeout_s"]


def test_exec_sent_logs_argv0_and_a_count_never_the_arguments(ready_to_lease, ready_lease):
    lid = ready_lease["lease_id"]
    _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
          {"argv": ["ace-mobile", "register", "--pin", "SECRET-1234"]})
    (ev,) = Event.objects.filter(kind="capability.exec.sent")
    assert ev.payload["argv0"] == "ace-mobile" and ev.payload["argc"] == 4
    assert "argv" not in ev.payload
    assert "SECRET-1234" not in ev.summary and "SECRET-1234" not in str(ev.payload)


def test_uploads_to_an_ended_lease_are_409(ready_to_lease, ready_lease):
    lid = ready_lease["lease_id"]
    ready_to_lease.delete(f"/api/capabilities/leases/{lid}")
    r = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/uploads", {"names": ["a.yaml"]})
    assert r.status_code == 409 and "released" in r.json()["detail"]


# ---- the queue ----------------------------------------------------------------

def test_two_promotes_racing_never_exceed_the_cap(tenancy, world):
    """EC2 is read outside the lock; the count inside it reads the DB, so a
    launch another process made after our read still counts."""
    c = _as(tenancy["owner"])
    _publish(c)
    _grant(c, max_concurrent=1)
    world.list_error = __import__("apps.capabilities.aws", fromlist=["AwsError"]).AwsError("hold the queue")
    a, b = (_acquire(c).json() for _ in range(2))
    assert a["status"] == b["status"] == "queued"
    world.list_error = None
    cap = AgentCapability.objects.get()
    world.list_hook = lambda: services.promote(AgentCapability.objects.get(pk=cap.pk))  # the other process
    services.promote(cap)
    statuses = sorted(CapabilityLease.objects.values_list("status", flat=True))
    assert statuses == ["provisioning", "queued"]
    assert len(world.run_calls) == 1


def test_a_release_during_the_launch_terminates_the_new_machine(ready_to_lease, world):
    def release_mid_launch(lease_id):
        world.launch_hook = None
        services.release(_lease(lease_id), reason="the holder gave up")

    world.launch_hook = release_mid_launch
    lease = _acquire(ready_to_lease).json()
    assert lease["status"] == "released"
    iid = world.tokens[lease["lease_id"]]
    assert iid in world.terminated and _lease(lease["lease_id"]).instance_id == ""


def test_a_just_ended_lease_does_not_hold_the_queue(tenancy, world):
    c = _as(tenancy["owner"])
    _publish(c)
    _grant(c, max_concurrent=1)
    first = _acquire(c).json()
    second = _acquire(c).json()
    assert second["status"] == "queued"
    # The first ends but EC2 still lists its instance as running for a moment.
    from apps.capabilities.aws import AwsError

    world.terminate_error = AwsError("eventually consistent")
    c.delete(f"/api/capabilities/leases/{first['lease_id']}")
    assert world.instances[first["instance_id"]].state in ("pending", "running")
    assert _lease(second["lease_id"]).status == "provisioning"


def test_the_launch_is_tagged_with_this_broker(ready_to_lease, world, settings):
    settings.CAPABILITY_BROKER_ID = "canopy.example"
    _acquire(ready_to_lease)
    assert world.run_calls[-1]["tags"] == {"canopy:broker": "canopy.example"}


def test_the_broker_id_defaults_to_the_identity_host(settings):
    settings.CAPABILITY_BROKER_ID = ""
    settings.CANOPY_IDENTITY_BASE_URL = "https://canopy.dimagi.com"
    assert services.broker_id() == "canopy.dimagi.com"


def test_another_brokers_machines_are_neither_counted_nor_killed(tenancy, world, settings):
    settings.CAPABILITY_BROKER_ID = "canopy.dimagi.com"
    c = _as(tenancy["owner"])
    _publish(c)
    _grant(c, max_concurrent=1)
    theirs = world.add_instance(lease_id="6f1c0000-0000-4000-8000-00000000beef", broker="canopy-staging")
    legacy = world.add_instance(lease_id="6f1c0000-0000-4000-8000-00000000dead")  # untagged: pre-0.9.0
    assert _acquire(c).json()["status"] == "queued"  # the legacy orphan still counts
    services.sweep()
    assert legacy in world.terminated and theirs not in world.terminated
    assert CapabilityLease.objects.get().status == "provisioning"  # theirs never held our slot


def test_a_transient_launch_failure_is_an_event_once_per_distinct_error(ready_to_lease, world):
    from .fakes import transient

    world.launch_error = transient("capacity")
    lease = _acquire(ready_to_lease).json()
    for _ in range(3):
        services.check_ready(_lease(lease["lease_id"]))
    world.launch_error = transient("throttled")
    services.check_ready(_lease(lease["lease_id"]))
    retries = Event.objects.filter(kind="capability.lease.launch_retry")
    assert retries.count() == 2 and {e.level for e in retries} == {"warn"}


# ---- a 0.9.0 stack: per-machine prefixes and the exec document ----------------

def test_a_v09_lease_lives_under_its_machines_own_prefix(v09, world):
    lease = _ready(v09, world)
    lid, iid = lease["lease_id"], lease["instance_id"]
    base = f"vm/AROAEXAMPLE:{iid}/lease/{lid}"
    assert _lease(lid).s3_base == base
    assert lease["out_prefix"] == f"s3://cap-bucket/{base}/out/"
    ups = _json(v09, "post", f"/api/capabilities/leases/{lid}/uploads", {"names": ["recipe.yaml"]}).json()
    assert ups["uploads"][0]["s3_uri"] == f"s3://cap-bucket/{base}/in/recipe.yaml"
    world.objects[("cap-bucket", f"{base}/out/run/shot.png")] = 99
    arts = _json(v09, "get", f"/api/capabilities/leases/{lid}/artifacts").json()["artifacts"]
    assert [(a["path"], a["size"]) for a in arts] == [("run/shot.png", 99)]


def test_a_v09_exec_goes_through_the_stacks_exec_document(v09, world):
    lease = _ready(v09, world)
    lid, iid = lease["lease_id"], lease["instance_id"]
    argv = ["ace-mobile", "run-recipe", "--out", "x y; rm -rf /", ""]
    _json(v09, "post", f"/api/capabilities/leases/{lid}/exec", {"argv": argv, "timeout_s": 120})
    (sent,) = world.sent
    base = f"vm/AROAEXAMPLE:{iid}/lease/{lid}"
    assert sent["document"] == "canopy-cap-ace-android-emulator-exec"
    assert sent["output_prefix"] == f"{base}/exec"
    p = sent["parameters"]
    assert base64.b64decode(p["Argv"][0]).split(b"\0")[:-1] == [a.encode() for a in argv]
    assert p["LeaseId"] == [lid] and p["ExecutionTimeout"] == ["120"]
    assert p["LeaseIn"] == [f"s3://cap-bucket/{base}/in/"] and p["LeaseOut"] == [f"s3://cap-bucket/{base}/out/"]


def test_a_v09_upload_before_the_machine_launches_is_409(tenancy, world):
    world.v09()
    c = _as(tenancy["owner"])
    _publish(c)
    _grant(c, max_concurrent=1)
    _acquire(c)
    queued = _acquire(c).json()
    assert queued["status"] == "queued" and queued["out_prefix"] == ""
    r = _json(c, "post", f"/api/capabilities/leases/{queued['lease_id']}/uploads", {"names": ["a.yaml"]})
    assert r.status_code == 409 and "no machine yet" in r.json()["detail"]


def test_a_lease_keeps_its_layout_when_the_stack_is_upgraded_under_it(ready_to_lease, world):
    lease = _ready(ready_to_lease, world)
    lid = lease["lease_id"]
    world.v09()
    services.refresh_outputs(AgentCapability.objects.get(), why="test")
    assert _lease(lid).s3_base == f"leases/{lid}"
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}").json()
    assert got["out_prefix"] == f"s3://cap-bucket/leases/{lid}/out/"
    # The NEXT lease takes the new layout.
    nxt = _acquire(ready_to_lease).json()
    assert nxt["out_prefix"].startswith(f"s3://cap-bucket/vm/AROAEXAMPLE:{nxt['instance_id']}/lease/")


def test_publish_refuses_an_exec_the_stacks_document_will_not_run(tenancy, world):
    world.v09()
    r = _publish(_as(tenancy["owner"]), {**MANIFEST, "exec": {"allow": ["ace-mobile", "adb"]}})
    assert r.status_code == 422 and "adb" in r.json()["detail"] and "ExecAllow" in r.json()["detail"]


def test_publish_refuses_a_lease_longer_than_the_backstop(tenancy, world):
    world.v09()
    long = {**MANIFEST, "limits": {**MANIFEST["limits"], "max_lease_s": 20000}}
    r = _publish(_as(tenancy["owner"]), long)
    assert r.status_code == 422 and "MaxInstanceLifetimeSeconds" in r.json()["detail"]


# ---- stack outputs are re-read ------------------------------------------------

def test_the_sweep_re_reads_stale_outputs(ready_to_lease, world):
    cap = AgentCapability.objects.get()
    AgentCapability.objects.filter(pk=cap.pk).update(
        outputs_refreshed_at=timezone.now() - dt.timedelta(seconds=services.OUTPUTS_REFRESH_S + 1))
    world.v09()
    assert services.sweep()["refreshed"] == 1
    cap.refresh_from_db()
    assert cap.stack_outputs["LeaseObjectScope"] == "instance"
    assert Event.objects.filter(kind="capability.outputs.refreshed").exists()
    # Fresh now: the next pass does not ask again.
    assert services.sweep()["refreshed"] == 0


def test_a_ready_timeout_re_reads_the_outputs(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    world.stacks["canopy-cap-ace-android-emulator"]["ArtifactsBucketName"] = "moved-bucket"
    CapabilityLease.objects.filter(pk=lease["lease_id"]).update(
        provisioning_at=timezone.now() - dt.timedelta(seconds=901))
    _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}")
    assert _lease(lease["lease_id"]).status == "failed"
    assert AgentCapability.objects.get().stack_outputs["ArtifactsBucketName"] == "moved-bucket"


def test_a_stack_that_breaks_the_contract_keeps_its_old_outputs(ready_to_lease, world):
    del world.stacks["canopy-cap-ace-android-emulator"]["LaunchTemplateId"]
    cap = AgentCapability.objects.get()
    assert services.refresh_outputs(cap, why="test") is False
    cap.refresh_from_db()
    assert cap.stack_outputs["LaunchTemplateId"] == "lt-0abc"
    assert Event.objects.filter(kind="capability.outputs.unreadable", level="warn").exists()


def test_the_refresh_route(ready_to_lease, tenancy, world):
    world.v09()
    r = _json(_as(tenancy["owner"]), "post", "/api/agents/ace/capabilities/android-emulator/refresh")
    assert r.status_code == 200, r.content
    assert r.json()["stack_outputs"]["ExecDocumentName"] == "canopy-cap-ace-android-emulator-exec"
    assert _json(_pat(tenancy["bot"]), "post",
                 "/api/agents/ace/capabilities/android-emulator/refresh").status_code == 200
    assert _json(_as(tenancy["editor"]), "post",
                 "/api/agents/ace/capabilities/android-emulator/refresh").status_code == 403
    del world.stacks["canopy-cap-ace-android-emulator"]["Owner"]
    bad = _json(_as(tenancy["owner"]), "post", "/api/agents/ace/capabilities/android-emulator/refresh")
    assert bad.status_code == 422 and "Owner" in bad.json()["detail"]


# ---- long-poll ----------------------------------------------------------------

def test_a_lease_long_poll_returns_the_moment_the_machine_is_ready(ready_to_lease, world, no_sleep):
    lease = _acquire(ready_to_lease).json()
    iid = lease["instance_id"]
    world.read_hook = lambda n: world.ready(iid) if n == 4 else None
    describes = world.describe_calls
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}?wait_s=20").json()
    assert got["status"] == "ready"
    assert no_sleep["t"] - 1000.0 == 3 * services.POLL_S  # three 1 s waits, not twenty
    assert world.describe_calls - describes == 1  # once up front, never in the loop


def test_a_lease_long_poll_gives_up_after_wait_s(ready_to_lease, world, no_sleep):
    lease = _acquire(ready_to_lease).json()
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}?wait_s=5").json()
    assert got["status"] == "provisioning"
    assert no_sleep["t"] - 1000.0 == 5 * services.POLL_S


def test_a_long_poll_is_bounded(ready_to_lease):
    lease = _acquire(ready_to_lease).json()
    r = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}?wait_s=60")
    assert r.status_code == 422


def test_a_ready_lease_long_poll_answers_at_once(ready_to_lease, ready_lease, no_sleep):
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{ready_lease['lease_id']}?wait_s=20").json()
    assert got["status"] == "ready" and no_sleep["t"] == 1000.0


def test_an_exec_long_poll_returns_when_it_is_done(ready_to_lease, ready_lease, world, no_sleep):
    from canopy_sdk.ondemand import Invocation

    lid, iid = ready_lease["lease_id"], ready_lease["instance_id"]
    exec_id = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
                    {"argv": ["ace-mobile", "diagnose"]}).json()["exec_id"]
    start = world.invocation_reads

    def finish(n):
        if n - start == 3:
            world.invocations[(exec_id, iid)] = Invocation(exec_id, iid, "Success", 0, "fine", "")

    world.invocation_hook = finish
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{exec_id}?wait_s=20").json()
    assert (got["status"], got["done"], got["stdout_tail"]) == ("Success", True, "fine")
    assert no_sleep["t"] - 1000.0 == 2 * services.POLL_S


# ---- presigned URLs never outlive the signing credentials ---------------------

def test_presigned_urls_are_bounded_by_the_credentials(ready_to_lease, ready_lease, world):
    world.credential_ttl_s = 600
    r = _json(ready_to_lease, "post", f"/api/capabilities/leases/{ready_lease['lease_id']}/uploads",
              {"names": ["a.yaml"]}).json()
    assert r["expires_in_s"] == 600 and "ttl=600" in r["uploads"][0]["put_url"]


# ---- the operator surface -----------------------------------------------------

def test_the_lease_list_is_workspace_scoped_and_filters(ready_to_lease, tenancy, world):
    a = _acquire(ready_to_lease).json()
    b = _acquire(ready_to_lease).json()
    ready_to_lease.delete(f"/api/capabilities/leases/{a['lease_id']}")
    every = _json(_as(tenancy["viewer"]), "get", "/api/capabilities/leases").json()
    assert [x["lease_id"] for x in every["leases"]] == [b["lease_id"], a["lease_id"]]
    live = _json(ready_to_lease, "get", "/api/capabilities/leases?status=live").json()["leases"]
    assert [x["lease_id"] for x in live] == [b["lease_id"]]
    assert _json(ready_to_lease, "get", "/api/capabilities/leases?status=nope").status_code == 422
    assert _json(ready_to_lease, "get", "/api/capabilities/leases?limit=1").json()["truncated"] is True
    assert _json(_as(tenancy["outsider"]), "get", "/api/capabilities/leases").json()["leases"] == []
    # The agent's own PAT sees its leases without being a member.
    assert len(_json(_pat(tenancy["bot"]), "get", "/api/capabilities/leases?agent=ace").json()["leases"]) == 2


def test_spend_rolls_up_machine_seconds_in_the_window(ready_to_lease, tenancy):
    now = timezone.now()
    done = _acquire(ready_to_lease).json()
    live = _acquire(ready_to_lease).json()
    CapabilityLease.objects.filter(pk=done["lease_id"]).update(
        status="released", provisioning_at=now - dt.timedelta(hours=3), ended_at=now - dt.timedelta(hours=1))
    CapabilityLease.objects.filter(pk=live["lease_id"]).update(provisioning_at=now - dt.timedelta(minutes=30))
    since = (now - dt.timedelta(hours=2)).isoformat()
    body = _json(ready_to_lease, "get", f"/api/capabilities/spend?since={since.replace('+', '%2B')}").json()
    (row,) = body["rows"]
    assert (row["agent"], row["capability"], row["leases"], row["live"]) == ("ace", "android-emulator", 2, 1)
    # 1 h of the ended lease inside the window, plus the live one's 30 min so far.
    assert abs(row["machine_seconds"] - (3600 + 1800)) < 5
    assert body["total_machine_seconds"] == row["machine_seconds"]
    assert _json(_as(tenancy["outsider"]), "get", "/api/capabilities/spend").json()["rows"] == []


def test_force_release_is_for_admins_and_terminates_even_an_ended_lease(ready_to_lease, tenancy, world):
    lease = _acquire(ready_to_lease).json()
    lid = lease["lease_id"]
    path = f"/api/capabilities/leases/{lid}/force-release"
    assert _json(_as(tenancy["editor"]), "post", path, {}).status_code == 403
    assert _json(_as(tenancy["outsider"]), "post", path, {}).status_code == 404
    r = _json(ready_to_lease, "post", path, {"reason": "stuck"})
    assert r.status_code == 200 and r.json()["status"] == "released"
    assert r.json()["reason"] == "force-released by jj@dimagi.com: stuck"
    # Again, on the ended lease: the machine is terminated again (a failed first terminate).
    world.terminated.clear()
    assert _json(ready_to_lease, "post", path, {}).status_code == 200
    assert world.terminated == [lease["instance_id"]]
    assert Event.objects.filter(kind="capability.lease.force_terminated").exists()


def test_terminate_orphans_now(ready_to_lease, tenancy, world):
    keep = _acquire(ready_to_lease).json()["instance_id"]
    orphan = world.add_instance(lease_id="6f1c0000-0000-4000-8000-00000000dead")
    path = "/api/agents/ace/capabilities/android-emulator/terminate-orphans"
    assert _json(_as(tenancy["editor"]), "post", path).status_code == 403
    r = _json(ready_to_lease, "post", path)
    assert r.status_code == 200 and r.json() == {"agent": "ace", "capability": "android-emulator",
                                                 "orphans": 1, "gone": 0}
    assert orphan in world.terminated and keep not in world.terminated
    from apps.capabilities import aws

    world.list_error = aws.AwsError("throttled")
    assert _json(ready_to_lease, "post", path).status_code == 502
    assert _json(ready_to_lease, "post", "/api/agents/ace/capabilities/gpu/terminate-orphans").status_code == 404
