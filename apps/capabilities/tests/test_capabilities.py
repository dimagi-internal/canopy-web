"""Cloud capabilities end to end over the REST routes, with AWS replaced by a fake.

Covers the lease state machine, cap → FIFO queue → promotion, the idempotent
ClientToken launch, the orphan sweep, deploy safety (an exec read back from SSM
by a fresh process), turn binding, and who may call what.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.agents.models import Agent
from apps.capabilities import aws, services
from apps.capabilities.models import AgentCapability, CapabilityGrant, CapabilityLease
from apps.events.models import Event
from apps.harness.models import Turn
from apps.workspaces.models import WorkspaceMembership as M

from .fakes import OUTPUTS, transient
from .helpers import MANIFEST, _acquire, _as, _grant, _json, _kinds, _pat, _publish

pytestmark = pytest.mark.django_db

# ---- publishing --------------------------------------------------------------

def test_publish_resolves_the_stack_outputs(tenancy):
    r = _publish(_as(tenancy["owner"]))
    assert r.status_code == 200, r.content
    body = r.json()
    (cap,) = body["capabilities"]
    assert cap["name"] == "android-emulator" and cap["stack"] == "canopy-cap-ace-android-emulator"
    assert cap["stack_outputs"] == OUTPUTS
    assert cap["grant"] is None and cap["limits"]["max_concurrent"] == 4
    assert body["retired"] == []


def test_the_agents_own_pat_may_publish(tenancy):
    """Publishing is the agent's deploy: its own login, whatever its workspace role."""
    assert _publish(_pat(tenancy["bot"])).status_code == 200


def test_publish_names_the_missing_contract_outputs(tenancy, world):
    del world.stacks["canopy-cap-ace-android-emulator"]["LaunchTemplateId"]
    r = _publish(_as(tenancy["owner"]))
    assert r.status_code == 422
    assert "LaunchTemplateId" in r.json()["detail"]
    assert not AgentCapability.objects.exists()


def test_publish_refuses_another_agents_stack(tenancy, world):
    world.stacks["canopy-cap-ace-android-emulator"]["Owner"] = "echo"
    r = _publish(_as(tenancy["owner"]))
    assert r.status_code == 422 and "belongs to 'echo'" in r.json()["detail"]


def test_publish_says_why_a_stack_cannot_be_read(tenancy, world):
    world.stack_error = aws.AwsError("AccessDenied: not authorized to cloudformation:DescribeStacks")
    r = _publish(_as(tenancy["owner"]))
    assert r.status_code == 422 and "BrokerRoleName" in r.json()["detail"]


def test_an_invalid_manifest_is_refused_by_the_shared_schema(tenancy):
    bad = {**MANIFEST, "exec": {"allow": ["/bin/sh"]}}
    assert _publish(_as(tenancy["owner"]), bad).status_code == 422


def test_publishing_is_for_admins_or_the_agent(tenancy):
    assert _publish(_as(tenancy["editor"])).status_code == 403
    assert _publish(_as(tenancy["outsider"])).status_code == 404


def test_republishing_without_a_capability_retires_it_and_releases_its_leases(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    r = _json(ready_to_lease, "put", "/api/agents/ace/capabilities", {"capabilities": []})
    assert r.status_code == 200 and r.json()["retired"] == ["android-emulator"]
    assert CapabilityLease.objects.get(pk=lease["lease_id"]).status == "released"
    assert lease["instance_id"] in world.terminated
    got = _json(ready_to_lease, "get", "/api/agents/ace/capabilities").json()
    assert got["capabilities"] == [] and got["retired"] == ["android-emulator"]


def test_publishing_is_a_setting_in_the_audit(tenancy):
    from apps.events.models import SettingChange

    _publish(_as(tenancy["owner"]))
    row = SettingChange.objects.get(operation="put_agent_capabilities")
    assert row.workspace_id == "connect" and row.changes[0]["scope"] == "capabilities:ace"


# ---- grants ------------------------------------------------------------------

def test_without_a_grant_acquire_is_403_naming_the_grant(tenancy):
    c = _as(tenancy["owner"])
    _publish(c)
    r = _acquire(c)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "no grant for capability 'android-emulator'" in detail
    assert "PUT /api/capabilities/grants" in detail and '"agent": "ace"' in detail


def test_granting_is_for_a_workspace_admin(tenancy):
    _publish(_as(tenancy["owner"]))
    assert _grant(_as(tenancy["editor"])).status_code == 403
    assert _grant(_as(tenancy["outsider"])).status_code == 404
    assert _grant(_as(tenancy["owner"])).status_code == 200


def test_a_grant_may_only_lower_the_manifest(tenancy):
    c = _as(tenancy["owner"])
    _publish(c)
    assert _grant(c, max_concurrent=9).status_code == 422
    r = _grant(c, max_concurrent=2, max_lease_s=3600)
    assert r.status_code == 200
    limits = _json(c, "get", "/api/agents/ace/capabilities").json()["capabilities"][0]["limits"]
    assert limits["max_concurrent"] == 2 and limits["max_lease_s"] == 3600


def test_revoking_a_grant_releases_its_leases(ready_to_lease):
    lease = _acquire(ready_to_lease).json()
    grant_id = CapabilityGrant.objects.get().pk
    assert ready_to_lease.delete(f"/api/capabilities/grants/{grant_id}").status_code == 204
    assert CapabilityLease.objects.get(pk=lease["lease_id"]).status == "released"


# ---- acquire + the state machine --------------------------------------------

def test_acquire_launches_with_the_lease_as_client_token(ready_to_lease, world):
    r = _acquire(ready_to_lease, params={"state": "cc-2.62.0"})
    assert r.status_code == 200, r.content
    lease = r.json()
    assert lease["status"] == "provisioning" and lease["ready"] is False and lease["position"] is None
    (call,) = world.run_calls
    assert call["lease_id"] == lease["lease_id"]
    assert call["user_data"] == {"lease_id": lease["lease_id"], "params": {"state": "cc-2.62.0"},
                                 "max_lifetime_s": 14400, "bucket": "cap-bucket"}
    assert lease["instance_id"] == world.tokens[lease["lease_id"]]
    assert lease["expires_at"] is not None
    assert lease["out_prefix"] == f"s3://cap-bucket/leases/{lease['lease_id']}/out/"


def test_acquire_refuses_an_undeclared_capability_and_bad_params(ready_to_lease):
    r = _json(ready_to_lease, "post", "/api/capabilities/leases", {"agent": "ace", "capability": "gpu-box"})
    assert r.status_code == 422 and "declares no capability 'gpu-box'" in r.json()["detail"]
    r = _acquire(ready_to_lease, params={"state": "cc-1.0"})
    assert r.status_code == 422 and "'state'" in r.json()["detail"]


def test_queued_to_ready_to_released(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    lid, iid = lease["lease_id"], lease["instance_id"]
    assert _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}").json()["status"] == "provisioning"
    world.ready(iid, timings={"boot_s": 230}, cli_version="1.0.0")
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}").json()
    assert got["status"] == "ready" and got["ready"] is True
    assert got["ready_info"]["cli_version"] == "1.0.0" and got["ready_at"]
    gone = ready_to_lease.delete(f"/api/capabilities/leases/{lid}").json()
    assert gone["status"] == "released" and iid in world.terminated
    # Releasing again is a no-op that answers the same.
    assert ready_to_lease.delete(f"/api/capabilities/leases/{lid}").json()["status"] == "released"
    assert _kinds(lid) == ["capability.lease.queued", "capability.lease.provisioning",
                           "capability.lease.launched", "capability.lease.ready", "capability.lease.released"]


def test_a_machine_that_reports_not_ready_fails_and_is_terminated(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    world.objects[("cap-bucket", f"vm/AROAEXAMPLE:{lease['instance_id']}/ready.json")] = {
        "ok": False, "reason": "emulator did not boot"}
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}").json()
    assert got["status"] == "failed" and "emulator did not boot" in got["reason"]
    assert lease["instance_id"] in world.terminated


def test_a_machine_not_ready_by_the_deadline_fails(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    CapabilityLease.objects.filter(pk=lease["lease_id"]).update(
        provisioning_at=timezone.now() - dt.timedelta(seconds=901))
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}").json()
    assert got["status"] == "failed" and "not ready after 900s" in got["reason"]
    assert lease["instance_id"] in world.terminated


def test_a_ready_lease_never_returns_to_provisioning(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    world.ready(lease["instance_id"])
    lid = lease["lease_id"]
    _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}")
    # The machine dies: the sweep ends the lease; it is never re-provisioned.
    world.instances[lease["instance_id"]].state = "terminated"
    services.sweep()
    got = CapabilityLease.objects.get(pk=lid)
    assert got.status == "failed" and "no longer running" in got.reason
    assert len(world.run_calls) == 1


# ---- cap → queue → promotion -------------------------------------------------

def test_over_the_cap_leases_queue_fifo_and_are_promoted_on_release(tenancy, world):
    c = _as(tenancy["owner"])
    _publish(c)
    _grant(c, max_concurrent=1)
    first, second, third = (_acquire(c).json() for _ in range(3))
    assert first["status"] == "provisioning"
    assert (second["status"], second["position"]) == ("queued", 1)
    assert (third["status"], third["position"]) == ("queued", 2)
    assert len(world.run_calls) == 1
    c.delete(f"/api/capabilities/leases/{first['lease_id']}")
    assert CapabilityLease.objects.get(pk=second["lease_id"]).status == "provisioning"
    t = _json(c, "get", f"/api/capabilities/leases/{third['lease_id']}").json()
    assert (t["status"], t["position"]) == ("queued", 1)


def test_a_tagged_instance_with_no_lease_counts_against_the_cap(tenancy, world):
    c = _as(tenancy["owner"])
    _publish(c)
    _grant(c, max_concurrent=1)
    world.add_instance()  # an orphan: running, tagged, no lease
    assert _acquire(c).json()["status"] == "queued"


def test_when_ec2_cannot_be_counted_nothing_launches(ready_to_lease, world):
    world.list_error = aws.AwsError("throttled")
    assert _acquire(ready_to_lease).json()["status"] == "queued"
    assert world.run_calls == []
    world.list_error = None
    services.sweep()  # the sweep promotes the queue once EC2 answers again
    assert CapabilityLease.objects.get().status == "provisioning"


# ---- the ClientToken: a retry never launches twice ---------------------------

def test_a_transient_launch_failure_is_retried_with_the_same_token(ready_to_lease, world):
    world.launch_error = transient()
    lease = _acquire(ready_to_lease).json()
    assert lease["status"] == "provisioning" and lease["instance_id"] is None
    assert "launch retrying" in lease["reason"]
    world.launch_error = None
    got = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lease['lease_id']}").json()
    assert got["instance_id"] and got["reason"] == ""
    assert [c["lease_id"] for c in world.run_calls] == [lease["lease_id"]] * 2
    # A third call — the same ClientToken — gets the same instance back.
    services._launch(CapabilityLease.objects.get(pk=lease["lease_id"]))
    assert len(world.live()) == 1
    assert CapabilityLease.objects.get(pk=lease["lease_id"]).instance_id == got["instance_id"]


def test_a_launch_the_db_never_recorded_is_adopted_not_killed(ready_to_lease, world):
    """A crash between RunInstances and the DB write: the instance carries the
    lease's tag, so the sweep adopts it rather than terminating it as an orphan."""
    lease = _acquire(ready_to_lease).json()
    CapabilityLease.objects.filter(pk=lease["lease_id"]).update(instance_id="")
    services.sweep()
    assert CapabilityLease.objects.get(pk=lease["lease_id"]).instance_id == lease["instance_id"]
    assert world.terminated == []


def test_a_refused_launch_fails_the_lease(ready_to_lease, world):
    from canopy_sdk.ondemand import LaunchFailed

    world.launch_error = LaunchFailed("UnauthorizedOperation", code="UnauthorizedOperation")
    lease = _acquire(ready_to_lease).json()
    assert lease["status"] == "failed" and "launch refused" in lease["reason"]


# ---- the sweep ---------------------------------------------------------------

def test_the_sweep_terminates_tagged_instances_with_no_live_lease(ready_to_lease, world):
    keep = _acquire(ready_to_lease).json()["instance_id"]
    orphan = world.add_instance(lease_id="6f1c0000-0000-4000-8000-00000000dead")
    other_agents = world.add_instance(owner="echo")
    counts = services.sweep()
    assert counts["orphans"] == 1
    assert orphan in world.terminated and keep not in world.terminated
    assert other_agents not in world.terminated  # never another owner's machine
    assert Event.objects.filter(kind="capability.orphans.terminated").exists()


def test_the_sweep_expires_and_idles_leases(ready_to_lease, world):
    old = _acquire(ready_to_lease).json()
    idle = _acquire(ready_to_lease).json()
    now = timezone.now()
    CapabilityLease.objects.filter(pk=old["lease_id"]).update(expires_at=now - dt.timedelta(seconds=1))
    CapabilityLease.objects.filter(pk=idle["lease_id"]).update(last_heartbeat_at=now - dt.timedelta(seconds=901))
    counts = services.sweep()
    assert (counts["expired"], counts["idle"]) == (1, 1)
    assert CapabilityLease.objects.get(pk=old["lease_id"]).status == "expired"
    assert CapabilityLease.objects.get(pk=idle["lease_id"]).status == "released"
    assert {old["instance_id"], idle["instance_id"]} <= set(world.terminated)


def test_the_claim_tick_sweep_is_throttled_and_never_raises(ready_to_lease, world, monkeypatch):
    calls = []
    monkeypatch.setattr(services, "sweep", lambda: calls.append(1))
    services.sweep_tick()
    services.sweep_tick()
    assert calls == [1]  # once per SWEEP_EVERY_S

    cache.delete(services.SWEEP_CACHE_KEY)

    def boom():
        raise RuntimeError("AWS is down")

    monkeypatch.setattr(services, "sweep", boom)
    services.sweep_tick()  # swallowed
    from apps.harness import claim

    monkeypatch.setattr(services, "sweep_tick", boom)
    claim._capability_sweep()  # and the claim's own guard swallows even that


def test_an_aws_outage_in_the_sweep_skips_that_capability(ready_to_lease, world):
    _acquire(ready_to_lease)
    world.list_error = aws.AwsError("throttled")
    assert services.sweep()["orphans"] == 0


# ---- exec, uploads, artifacts ------------------------------------------------

def test_exec_quotes_argv_and_sends_output_to_the_lease_prefix(ready_to_lease, ready_lease, world):
    lid = ready_lease["lease_id"]
    r = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
              {"argv": ["ace-mobile", "run-recipe", "--recipe", "s3://b/x; rm -rf /"], "timeout_s": 120})
    assert r.status_code == 200, r.content
    body = r.json()
    (sent,) = world.sent
    assert body["exec_id"] == sent["command_id"] and body["timeout_s"] == 120
    assert sent["output_prefix"] == f"leases/{lid}/exec"
    assert sent["commands"][-1] == "exec ace-mobile run-recipe --recipe 's3://b/x; rm -rf /'"
    assert f"export CANOPY_LEASE_OUT=s3://cap-bucket/leases/{lid}/out/" in sent["commands"]


def test_exec_refuses_an_argv0_off_the_allowlist(ready_to_lease, ready_lease, world):
    r = _json(ready_to_lease, "post", f"/api/capabilities/leases/{ready_lease['lease_id']}/exec",
              {"argv": ["bash", "-c", "id"]})
    assert r.status_code == 422 and "its manifest allows: ace-mobile" in r.json()["detail"]
    assert world.sent == []


def test_exec_needs_a_ready_lease(ready_to_lease, world):
    lease = _acquire(ready_to_lease).json()
    r = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lease['lease_id']}/exec",
              {"argv": ["ace-mobile", "diagnose"]})
    assert r.status_code == 409


def test_exec_status_is_read_back_from_ssm_by_a_fresh_process(ready_to_lease, ready_lease, world, monkeypatch):
    """Deploy safety: nothing about an exec lives in the web process. A brand-new
    broker (a new canopy-web task) reads the same answer from SSM."""
    lid, iid = ready_lease["lease_id"], ready_lease["instance_id"]
    exec_id = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
                    {"argv": ["ace-mobile", "diagnose"]}).json()["exec_id"]
    running = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{exec_id}").json()
    assert (running["status"], running["done"], running["exit_code"], running["stdout_url"]) == (
        "InProgress", False, None, None)

    # "Deploy": a new process with a new broker; only AWS (the fake world) and
    # the DB persist. The exec id is all the caller kept.
    from canopy_sdk.ondemand import Invocation

    world.invocations[(exec_id, iid)] = Invocation(
        exec_id, iid, "Success", 0, "x" * 5000 + '{"ok": true}', "",
        stdout_url=f"https://s3.us-east-1.amazonaws.com/cap-bucket/leases/{lid}/exec/{exec_id}/{iid}/"
                   "awsrunShellScript/0.awsrunShellScript/stdout")
    monkeypatch.setattr(aws, "get", lambda region: world.broker())
    done = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{exec_id}").json()
    assert (done["status"], done["done"], done["exit_code"]) == ("Success", True, 0)
    assert done["stdout_tail"].endswith('{"ok": true}') and len(done["stdout_tail"]) == 4000
    assert done["stdout_url"].startswith(f"https://signed.example/cap-bucket/leases/{lid}/exec/{exec_id}/")
    assert done["stderr_url"].endswith("/0.awsrunShellScript/stderr?get&ttl=3600")


def test_an_exec_id_from_another_lease_is_not_found(ready_to_lease, ready_lease):
    r = _json(ready_to_lease, "get", f"/api/capabilities/leases/{ready_lease['lease_id']}/exec/"
                                     "00000000-0000-4000-8000-999999999999")
    assert r.status_code == 404


def test_an_exec_ssm_has_not_materialised_yet_is_pending_not_missing(ready_to_lease, ready_lease, world):
    """SSM answers InvocationDoesNotExist for a few seconds after SendCommand;
    the first cloud-runner turn polled inside that window (turn f51078e3)."""
    lid, iid = ready_lease["lease_id"], ready_lease["instance_id"]
    exec_id = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
                    {"argv": ["ace-mobile", "ui-dump"]}).json()["exec_id"]
    del world.invocations[(exec_id, iid)]  # SSM has not caught up
    r = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{exec_id}")
    assert r.status_code == 200, r.content
    assert (r.json()["status"], r.json()["done"]) == ("Pending", False)


def test_an_exec_ssm_never_materialises_is_lost_after_the_grace_window(ready_to_lease, ready_lease, world):
    lid, iid = ready_lease["lease_id"], ready_lease["instance_id"]
    exec_id = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/exec",
                    {"argv": ["ace-mobile", "ui-dump"]}).json()["exec_id"]
    del world.invocations[(exec_id, iid)]
    old = (timezone.now() - dt.timedelta(seconds=services.INVOCATION_GRACE_S + 5)).isoformat()
    CapabilityLease.objects.filter(pk=lid).update(execs={exec_id: old})
    r = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{exec_id}")
    assert r.status_code == 404 and "SSM has no invocation" in r.json()["detail"]


def test_an_exec_id_ssm_knows_but_this_lease_never_sent_is_not_found(ready_to_lease, ready_lease, world):
    lid, iid = ready_lease["lease_id"], ready_lease["instance_id"]
    from canopy_sdk.ondemand import Invocation
    foreign = "00000000-0000-4000-8000-777777777777"
    world.invocations[(foreign, iid)] = Invocation(foreign, iid, "Success", 0, "secret", "")
    r = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/exec/{foreign}")
    assert r.status_code == 404


def test_uploads_presign_under_in_and_artifacts_under_out(ready_to_lease, ready_lease, world):
    lid = ready_lease["lease_id"]
    r = _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/uploads",
              {"names": ["recipes/smoke.yaml", "palette.yaml"]})
    assert r.status_code == 200, r.content
    ups = r.json()["uploads"]
    assert ups[0]["s3_uri"] == f"s3://cap-bucket/leases/{lid}/in/recipes/smoke.yaml"
    assert "?put" in ups[0]["put_url"] and r.json()["expires_in_s"] == 3600
    for bad in (["../escape"], ["/abs"], ["a b"]):
        assert _json(ready_to_lease, "post", f"/api/capabilities/leases/{lid}/uploads",
                     {"names": bad}).status_code == 422

    world.objects[("cap-bucket", f"leases/{lid}/out/run/shot-1.png")] = 1234
    world.objects[("cap-bucket", f"leases/{lid}/in/recipes/smoke.yaml")] = 10
    arts = _json(ready_to_lease, "get", f"/api/capabilities/leases/{lid}/artifacts?prefix=run/").json()
    assert [(a["path"], a["size"]) for a in arts["artifacts"]] == [("run/shot-1.png", 1234)]
    assert _json(ready_to_lease, "get",
                 f"/api/capabilities/leases/{lid}/artifacts?prefix=../in").status_code == 422


# ---- turn binding ------------------------------------------------------------

def test_a_turn_bound_lease_is_released_when_its_turn_ends(ready_to_lease, tenancy, world,
                                                           django_capture_on_commit_callbacks):
    from apps.harness import services as hsvc

    turn = Turn.objects.create(agent=tenancy["agent"], prompt="phase 6", status=Turn.RUNNING)
    lease = _acquire(ready_to_lease, turn_id=str(turn.pk)).json()
    assert lease["turn_id"] == str(turn.pk)
    with django_capture_on_commit_callbacks(execute=True):
        hsvc.finish_turn(turn, status=Turn.DONE)
    got = CapabilityLease.objects.get(pk=lease["lease_id"])
    assert got.status == "released" and got.reason == "its turn ended"
    assert lease["instance_id"] in world.terminated


def test_a_turn_id_must_be_the_agents_own_live_turn(ready_to_lease, tenancy):
    other = Agent.objects.create(slug="echo", name="Echo", workspace=tenancy["ws"])
    theirs = Turn.objects.create(agent=other, prompt="x", status=Turn.RUNNING, idempotency_key="t-theirs")
    assert _acquire(ready_to_lease, turn_id=str(theirs.pk)).status_code == 422
    done = Turn.objects.create(agent=tenancy["agent"], prompt="x", status=Turn.DONE, idempotency_key="t-done")
    assert _acquire(ready_to_lease, turn_id=str(done.pk)).status_code == 422


# ---- who may call ------------------------------------------------------------

def test_a_non_member_cannot_see_or_lease(ready_to_lease, tenancy):
    lease = _acquire(ready_to_lease).json()
    out = _as(tenancy["outsider"])
    assert _json(out, "get", "/api/agents/ace/capabilities").status_code == 404
    assert _acquire(out).status_code == 404
    assert _json(out, "get", f"/api/capabilities/leases/{lease['lease_id']}").status_code == 404
    assert out.delete(f"/api/capabilities/leases/{lease['lease_id']}").status_code == 404
    assert CapabilityLease.objects.get(pk=lease["lease_id"]).status == "provisioning"


def test_a_viewer_may_read_the_declaration_but_not_lease(ready_to_lease, tenancy):
    v = _as(tenancy["viewer"])
    assert _json(v, "get", "/api/agents/ace/capabilities").status_code == 200
    assert _acquire(v).status_code == 403


def test_the_agents_own_pat_leases_below_the_editor_role(ready_to_lease, tenancy, world):
    bot = _pat(tenancy["bot"])
    lease = _acquire(bot)
    assert lease.status_code == 200, lease.content
    assert _json(bot, "get", f"/api/capabilities/leases/{lease.json()['lease_id']}").status_code == 200


def test_the_agents_own_login_is_a_stranger_once_it_is_not_a_member(ready_to_lease, tenancy, world):
    """No own-login door past membership: a login a workspace admin removed (or
    one a move left behind) gets the same 404 as anyone outside the workspace."""
    lease = _acquire(ready_to_lease).json()
    M.objects.filter(user=tenancy["bot"]).delete()
    bot = _pat(tenancy["bot"])
    assert _acquire(bot).status_code == 404
    assert _json(bot, "get", f"/api/capabilities/leases/{lease['lease_id']}").status_code == 404
    assert _json(bot, "get", "/api/capabilities/leases").json()["leases"] == []


def test_an_editor_leases_for_the_agent(ready_to_lease, tenancy):
    assert _acquire(_as(tenancy["editor"])).status_code == 200


def test_a_malformed_lease_id_is_not_found(ready_to_lease):
    assert _json(ready_to_lease, "get", "/api/capabilities/leases/not-a-uuid").status_code == 404
