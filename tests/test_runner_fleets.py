"""Runner fleets (`apps/harness/fleets.py`): interchangeable cloud runners that join
on AWS's word under a fleet a person approved once, read one credential set, and
that canopy-web grows, shrinks and fails over.

The rules these hold:
- a fleet's approval is a passkey touch by its creator in a browser; a token
  cannot give it, and nothing joins before it;
- an instance joins only when STS says it is `assumed-role/<the fleet's role>/<i-…>`
  AND EC2 says that instance is pending/running in the fleet's group; each member
  is its own runner with its own key, and no member can sign in as another;
- a fleet's role is never one box's identity, and the reverse;
- every member reads the fleet's one credential list; a full login is refused;
- the scaler grows for turns that wait on capacity only, shrinks one idle member
  at a time through a drain, never below `min_members`, and never lets AWS pick a
  busy member; a member that is gone has its running turn queued again and its
  chats moved to a live member.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.events.models import Activity
from apps.harness import aws_iam_proof, claim, fleets
from apps.harness import runners as harness_runners
from apps.harness.models import (
    Runner,
    RunnerAssignment,
    RunnerCredential,
    RunnerDeviceKey,
    RunnerFleet,
    RunnerInvitation,
    RunnerLogin,
    Turn,
    WorkspaceRunnerOrder,
)
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.runner_invitations import PAIR, PAIR_HTU, _pat_client, _proof, make_org

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("fresh_cache")]

ROLE = "arn:aws:iam::123456789012:role/canopy-fleet-dimagi"
GROUP = "canopy-fleet-dimagi"
I1, I2 = "i-0123456789abcdef0", "i-0fedcba9876543210"
FLEETS = "/api/harness/runner-fleets/"
SESSIONS = "/api/runner-sessions/"
ECDH = {"kty": "EC", "crv": "P-256",
        "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
        "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0"}


def assumed(instance: str, role: str = "canopy-fleet-dimagi") -> str:
    return f"arn:aws:sts::123456789012:assumed-role/{role}/{instance}"


class FakeAws:
    """The group, its warm pool and EC2, as the scaler and a join read them."""

    def __init__(self):
        # instance id → {"state", "group", "lifecycle", "protected"}
        self.instances: dict[str, dict] = {}
        self.desired = 1
        self.calls: list[tuple] = []

    def add(self, iid, *, lifecycle="InService", state="running", group=GROUP):
        self.instances[iid] = {"state": state, "group": group, "lifecycle": lifecycle, "protected": True}

    def instance(self, iid):
        i = self.instances.get(iid)
        return None if i is None else {"state": i["state"], "group": i["group"], "lifecycle": i["lifecycle"]}

    def group(self, name):
        if name != GROUP:
            return None
        return {"desired": self.desired, "instances": [
            {"id": k, "lifecycle": v["lifecycle"], "protected": v["protected"]}
            for k, v in self.instances.items() if v["group"] == GROUP]}

    def set_desired(self, name, n):
        self.calls.append(("desired", n))
        self.desired = n

    def protect(self, name, iid, protected):
        self.calls.append(("protect", iid, protected))
        self.instances[iid]["protected"] = protected


@pytest.fixture
def aws(monkeypatch):
    fake = FakeAws()
    monkeypatch.setattr(fleets, "get", lambda region: fake)
    return fake


@pytest.fixture
def org():
    return make_org()


def _ask(org, **extra):
    body = {"name": "dimagi", "workspace": "connect", "role_arn": ROLE, "group_name": GROUP,
            "ecdh_public_jwk": ECDH, "min_members": 1, "max_members": 3, "member_turns": 2, **extra}
    return _pat_client(org["pat"]).post(FLEETS, data=body, content_type="application/json")


def _approved_fleet(org) -> RunnerFleet:
    res = _ask(org)
    assert res.status_code == 201, res.content
    assert org["browser"].post(f"/api/harness/runner-invitations/{res.json()['id']}/approve").status_code == 200
    return RunnerFleet.objects.get(name="dimagi")


def _join(sts, instance, *, name=None, role="canopy-fleet-dimagi"):
    sts.arn = assumed(instance, role)
    body = {"name": name or f"dimagi-{instance.removeprefix('i-')}", "kind": "cloud", "host": "ubuntu@ip",
            "capabilities": {"sessions": True}, "proof": _proof(PAIR_HTU)}
    return Client().post(PAIR, data=body, content_type="application/json")


# ---- asking for and approving a fleet ------------------------------------------------

def test_asking_for_a_fleet_makes_it_pending_with_an_invitation_to_approve(org):
    res = _ask(org)
    assert res.status_code == 201, res.content
    body = res.json()
    fleet = RunnerFleet.objects.get(name="dimagi")
    assert fleet.status == RunnerFleet.PENDING and fleet.owner == org["user"]
    assert fleet.credential_set.name == "dimagi credentials"
    assert body["fleet"]["name"] == "dimagi" and body["fleet"]["status"] == "pending"
    assert body["approve_url"].endswith(f"/w/connect/runner-invitations/{body['id']}")


def test_no_token_can_approve_a_fleet(org):
    inv = _ask(org).json()
    res = _pat_client(org["pat"]).post(f"/api/harness/runner-invitations/{inv['id']}/approve")
    assert res.status_code == 403
    assert RunnerFleet.objects.get(name="dimagi").status == RunnerFleet.PENDING


def test_the_creators_passkey_approval_activates_the_fleet_and_uses_the_invitation_up(org):
    fleet = _approved_fleet(org)
    assert fleet.status == RunnerFleet.ACTIVE and fleet.approved_by == org["user"]
    assert RunnerInvitation.objects.get(fleet=fleet).consumed_at is not None
    # Approving again is a no-op, not a 409.
    inv = RunnerInvitation.objects.get(fleet=fleet)
    assert org["browser"].post(f"/api/harness/runner-invitations/{inv.pk}/approve").status_code == 200


def test_a_role_that_is_one_boxs_cannot_be_a_fleets(org):
    box = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=org["user"], workspace=org["ws"])
    RunnerDeviceKey.objects.create(runner=box, kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE,
                                   jkt=aws_iam_proof.role_jkt(ROLE), status=RunnerDeviceKey.ACTIVE)
    assert _ask(org).status_code == 422


def test_a_fleets_role_cannot_be_invited_as_one_box(org):
    _ask(org)
    res = _pat_client(org["pat"]).post("/api/harness/runner-invitations/", data={
        "name": "cloud-ec2-9", "workspace": "connect", "role_arn": ROLE}, content_type="application/json")
    assert res.status_code == 422


def test_min_members_is_at_least_one_and_not_above_max(org):
    assert _ask(org, min_members=0).status_code == 422
    assert _ask(org, min_members=4, max_members=2).status_code == 422


# ---- joining --------------------------------------------------------------------------

def test_an_instance_of_the_group_joins_as_its_own_runner(org, sts, aws):
    fleet = _approved_fleet(org)
    aws.add(I1)
    res = _join(sts, I1)
    assert res.status_code == 201, res.content
    member = Runner.objects.get(pk=res.json()["runner_id"])
    assert member.name == "dimagi-0123456789abcdef0" and member.fleet == fleet
    assert member.instance_id == I1 and member.owner == org["user"] and member.workspace_id == "connect"
    assert member.credential_set == fleet.credential_set and member.power == Runner.POWER_ON
    assert member.machine_id == f"{ROLE}/{I1}"
    key = member.device_keys.get()
    assert key.jkt == aws_iam_proof.member_jkt(ROLE, I1) and key.status == RunnerDeviceKey.ACTIVE
    assert key.ecdh_approved_at is not None and key.approved_by == org["user"]
    assert Activity.objects.filter(kind="runner.paired", ref=str(member.pk)).exists()


def test_the_same_instance_again_reattaches(org, sts, aws):
    _approved_fleet(org)
    aws.add(I1)
    first = _join(sts, I1).json()["runner_id"]
    cache.clear()
    again = _join(sts, I1)
    assert again.status_code == 200 and again.json() == {**again.json(), "status": "reattached",
                                                           "runner_id": first}
    assert Runner.objects.filter(fleet__name="dimagi").count() == 1


def test_nothing_joins_a_fleet_before_its_approval(org, sts, aws):
    _ask(org)
    aws.add(I1)
    res = _join(sts, I1)
    assert res.status_code == 202 and res.json()["status"] == "waiting"
    assert not Runner.objects.exists()


@pytest.mark.parametrize("case", ["not-in-group", "stopped", "no-instance", "other-group", "wrong-name",
                                  "not-an-instance"])
def test_an_instance_aws_does_not_vouch_for_is_refused(org, sts, aws, case):
    _approved_fleet(org)
    name = None
    if case == "not-in-group":
        aws.add(I1, group="")
    elif case == "stopped":
        aws.add(I1, state="stopped")
    elif case == "other-group":
        aws.add(I1, group="someone-elses-group")
    elif case == "wrong-name":
        aws.add(I1)
        name = "dimagi-0fedcba9876543210"
    elif case == "not-an-instance":
        aws.add(I1)
    instance = "botocore-session-1" if case == "not-an-instance" else I1
    res = _join(sts, instance, name=name or f"dimagi-{I1.removeprefix('i-')}")
    assert res.status_code == 401
    assert res.json()["detail"] == "no approved pairing invitation matches this box's instance role and name"
    assert not Runner.objects.exists()


def test_a_retired_fleet_takes_no_one(org, sts, aws):
    fleet = _approved_fleet(org)
    fleets.retire(fleet)
    aws.add(I1)
    assert _join(sts, I1).status_code == 401


def test_an_owner_who_lost_the_workspace_loses_new_members(org, sts, aws):
    _approved_fleet(org)
    WorkspaceMembership.objects.filter(user=org["user"]).update(role=WorkspaceMembership.VIEWER)
    aws.add(I1)
    assert _join(sts, I1).status_code == 401


def test_each_member_signs_in_as_itself_and_only_itself(org, sts, aws):
    _approved_fleet(org)
    aws.add(I1)
    aws.add(I2)
    one = Runner.objects.get(pk=_join(sts, I1).json()["runner_id"])
    two = Runner.objects.get(pk=_join(sts, I2).json()["runner_id"])
    htu = f"http://testserver{SESSIONS}"

    def sign_in(instance, runner):
        sts.arn = assumed(instance)
        return Client().post(SESSIONS, data={"runner": str(runner.pk)}, content_type="application/json",
                             HTTP_X_CANOPY_AWS_PROOF=_proof(htu))

    assert sign_in(I1, one).status_code == 201
    assert sign_in(I2, two).status_code == 201
    assert sign_in(I1, two).status_code == 401  # one member is never another
    # The role on its own is nobody.
    sts.arn = ROLE.replace(":iam::", ":sts::").replace(":role/", ":assumed-role/") + "/not-an-instance"
    res = Client().post(SESSIONS, data={"runner": str(one.pk)}, content_type="application/json",
                        HTTP_X_CANOPY_AWS_PROOF=_proof(htu))
    assert res.status_code == 401


def test_a_fleets_role_cannot_enroll_a_device_key(org, sts, aws, monkeypatch):
    _approved_fleet(org)
    box = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=org["user"], workspace=org["ws"])
    monkeypatch.setattr(aws_iam_proof, "verify", lambda proof, request: ROLE)
    res = _pat_client(org["pat"]).post("/api/runner-device-keys/", data={
        "runner": str(box.pk), "kind": "aws-iam", "role_arn": ROLE, "proof": "p"},
        content_type="application/json")
    assert res.status_code == 422


# ---- the credential set ---------------------------------------------------------------

def _member(fleet, instance, *, power=Runner.POWER_ON, online=True, **kw):
    now = timezone.now()
    return Runner.objects.create(
        name=fleets.member_name(fleet, instance), kind=Runner.CLOUD, owner=fleet.owner,
        workspace=fleet.workspace, fleet=fleet, instance_id=instance, credential_set=fleet.credential_set,
        power=power, power_changed_at=now - dt.timedelta(hours=2),
        status=Runner.ONLINE if online else Runner.DISCONNECTED,
        last_heartbeat_at=now if online else now - dt.timedelta(hours=1),
        capabilities={"sessions": True}, **kw)


def test_every_member_reads_the_fleets_one_list(org):
    fleet = _approved_fleet(org)
    one, two = _member(fleet, I1), _member(fleet, I2)
    RunnerLogin.objects.create(credential_set=fleet.credential_set, key="aaaa1111", kind=RunnerLogin.TOKEN,
                               name="jj@dimagi.com")
    assert [e.key for e in harness_runners._entries(one)] == ["aaaa1111"]
    assert [e.key for e in harness_runners._entries(two)] == ["aaaa1111"]
    harness_runners._touch(one)
    # A change seen from one member moves every member's stamp: each re-reads.
    assert set(RunnerCredential.objects.values_list("runner_id", flat=True)) == {one.pk, two.pk}


def test_a_member_cannot_hold_a_full_login(org):
    fleet = _approved_fleet(org)
    one = _member(fleet, I1)
    res = org["browser"].post(f"/api/harness/runners/{one.pk}/logins")
    assert res.status_code == 409, res.content
    assert not RunnerLogin.objects.exists()
    # Nor does a login found on its disk join the shared list.
    harness_runners.report_runner_logins(one, [], adopt_default=True)
    assert not RunnerLogin.objects.exists()


def test_a_new_member_is_given_the_fleets_copy_of_the_vault_key(org, sts, aws):
    from apps.agents.credentials import workspace_vaults
    from apps.agents.models import VaultKeyWrap

    fleet = _approved_fleet(org)
    aws.add(I1)
    one = Runner.objects.get(pk=_join(sts, I1).json()["runner_id"])
    fleet.refresh_from_db()
    vault = fleet.credential_set.vaults.get()
    # The set's vault is made with the first member, and serves it.
    assert vault is not None and [r.pk for r in workspace_vaults.serving_runners(vault)] == [one.pk]
    # The owner's browser grants the first member (a person, once per fleet)…
    type(vault).objects.filter(pk=vault.pk).update(kv=1)
    VaultKeyWrap.objects.create(vault=vault, kv=1, kind=VaultKeyWrap.DEVICE,
                                device_key=one.device_keys.get(), wrap={"alg": "ecdh-es", "ct": "x"})
    fleets.refresh_wraps(fleet)
    fleet.refresh_from_db()
    assert fleet.vault_wraps == {str(vault.pk): {"kv": 1, "wrap": {"alg": "ecdh-es", "ct": "x"}}}
    # …and every member after it gets the same ciphertext, with no one.
    aws.add(I2)
    cache.clear()
    two = Runner.objects.get(pk=_join(sts, I2).json()["runner_id"])
    w = VaultKeyWrap.objects.get(vault=vault, device_key__runner=two)
    assert w.kv == 1 and w.wrap == {"alg": "ecdh-es", "ct": "x"}


# ---- the scaler -----------------------------------------------------------------------

@pytest.fixture
def world(org, aws):
    fleet = _approved_fleet(org)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=org["ws"], owner=org["user"])
    aws.add(I1)
    one = _member(fleet, I1)
    RunnerAssignment.objects.create(agent=agent, runner=one, rank=0)
    return {"fleet": fleet, "agent": agent, "one": one, "org": org}


def _turn(agent, key, *, age_seconds=0, **kw):
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key,
                               routing=Turn.ANY, **kw)
    if age_seconds:
        Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - dt.timedelta(seconds=age_seconds))
        turn.refresh_from_db()
    return turn


def test_turns_waiting_on_capacity_grow_the_fleet(world, aws):
    other = Agent.objects.create(slug="eva", name="Eva", workspace=world["org"]["ws"])
    third = Agent.objects.create(slug="ada", name="Ada", workspace=world["org"]["ws"])
    for a in (other, third):
        RunnerAssignment.objects.create(agent=a, runner=world["one"], rank=0)
    _turn(other, "k1", age_seconds=120)
    _turn(third, "k2", age_seconds=120)
    _turn(world["agent"], "k3", age_seconds=120)

    fleets.tick()
    assert aws.calls == [("desired", 3)]  # ceil(3 / 2 turns) = 2 more members
    assert Activity.objects.filter(kind="runner.fleet.scaled").exists()


def test_growth_stops_at_max_members(world, aws):
    RunnerFleet.objects.filter(pk=world["fleet"].pk).update(max_members=2)
    for i in range(6):
        a = Agent.objects.create(slug=f"a{i}", name=f"A{i}", workspace=world["org"]["ws"])
        RunnerAssignment.objects.create(agent=a, runner=world["one"], rank=0)
        _turn(a, f"k{i}", age_seconds=120)
    fleets.tick()
    assert aws.calls == [("desired", 2)]


def test_a_fresh_turn_or_one_behind_its_agent_grows_nothing(world, aws):
    _turn(world["agent"], "running", status=Turn.RUNNING, claimed_by=world["one"], claimed_at=timezone.now())
    _turn(world["agent"], "behind", age_seconds=300)
    other = Agent.objects.create(slug="eva", name="Eva", workspace=world["org"]["ws"])
    RunnerAssignment.objects.create(agent=other, runner=world["one"], rank=0)
    _turn(other, "fresh", age_seconds=5)
    fleets.tick()
    assert aws.calls == []


def test_members_coming_up_absorb_waiting_turns_before_more_are_asked_for(world, aws):
    aws.add(I2, lifecycle="Pending")
    aws.desired = 2
    other = Agent.objects.create(slug="eva", name="Eva", workspace=world["org"]["ws"])
    RunnerAssignment.objects.create(agent=other, runner=world["one"], rank=0)
    _turn(other, "k1", age_seconds=120)
    fleets.tick()
    assert aws.calls == []


def test_a_member_in_the_warm_pool_reads_stopped_and_on_its_way(world, aws):
    aws.instances[I1]["lifecycle"] = "Warmed:Stopped"
    fleets.tick()
    one = Runner.objects.get(pk=world["one"].pk)
    assert one.power == Runner.POWER_STOPPED and one.wakeable
    # The claim gives it nothing; the reach readers count it as on its way.
    turn = _turn(world["agent"], "k1")
    assert claim.claim_next_turn(one) is None
    assert claim.turn_reach(turn).kind == claim.LIVE


def test_an_idle_member_is_drained_then_put_back_in_the_warm_pool(world, aws):
    aws.add(I2)
    aws.desired = 2
    _member(world["fleet"], I2)
    fleets.tick()
    drained = [m for m in Runner.objects.filter(fleet=world["fleet"]) if m.power == Runner.POWER_DRAINING]
    assert len(drained) == 1 and aws.calls == []
    assert claim.claim_next_turn(drained[0]) is None
    fleets.tick()
    m = Runner.objects.get(pk=drained[0].pk)
    assert m.power == Runner.POWER_STOPPED
    assert aws.calls == [("protect", m.instance_id, False), ("desired", 1)]


def test_work_arriving_while_draining_puts_the_member_back(world, aws):
    aws.add(I2)
    aws.desired = 2
    _member(world["fleet"], I2)
    fleets.tick()
    m = Runner.objects.get(fleet=world["fleet"], power=Runner.POWER_DRAINING)
    RunnerAssignment.objects.get_or_create(agent=world["agent"], runner=m, defaults={"rank": 1})
    _turn(world["agent"], "k1")
    fleets.tick()
    assert Runner.objects.get(pk=m.pk).power == Runner.POWER_ON
    assert ("desired", 1) not in aws.calls


def test_a_member_back_from_the_warm_pool_is_protected_again(world, aws):
    aws.instances[I1]["protected"] = False
    fleets.tick()
    assert aws.calls == [("protect", I1, True)]


def test_the_fleet_never_shrinks_below_min_members(world, aws):
    fleets.tick()
    fleets.tick()
    assert Runner.objects.get(pk=world["one"].pk).power == Runner.POWER_ON
    assert aws.calls == []


def test_a_busy_member_is_never_drained(world, aws):
    aws.add(I2)
    aws.desired = 2
    two = _member(world["fleet"], I2)
    Runner.objects.filter(pk=world["one"].pk).update(power_changed_at=timezone.now())
    _turn(world["agent"], "running", status=Turn.RUNNING, claimed_by=two, claimed_at=timezone.now())
    fleets.tick()
    assert Runner.objects.get(pk=two.pk).power == Runner.POWER_ON


def test_a_member_whose_instance_left_the_group_is_retired_and_its_turn_queued_again(world, aws):
    running = _turn(world["agent"], "running", status=Turn.RUNNING, claimed_by=world["one"],
                    claimed_at=timezone.now())
    del aws.instances[I1]
    fleets.tick()
    one = Runner.objects.get(pk=world["one"].pk)
    assert one.status == Runner.RETIRED
    running.refresh_from_db()
    assert running.status == Turn.QUEUED and running.claimed_by_id is None and running.attempts == 1


def test_a_chat_on_a_gone_member_moves_to_a_live_one(world, aws):
    aws.add(I2)
    aws.desired = 2
    two = _member(world["fleet"], I2)
    RunnerAssignment.objects.create(agent=world["agent"], runner=two, rank=1)
    org = world["org"]
    session = Session.objects.create(workspace=org["ws"], created_by=org["user"], agent=world["agent"])
    RunnerBinding.objects.create(session=session, runner=world["one"], session_key="abc", host="h")
    queued = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                                 idempotency_key="chat-1", prompt="hello", enqueued_by=org["user"])
    aws.instances[I1]["lifecycle"] = "Warmed:Stopped"
    # Stopped: it has stopped heartbeating, so it cannot hand anything over.
    Runner.objects.filter(pk=world["one"].pk).update(
        status=Runner.DISCONNECTED, last_heartbeat_at=timezone.now() - dt.timedelta(minutes=5))
    fleets.tick()
    session.runner_binding.refresh_from_db()
    assert session.runner_binding.runner_id == two.pk
    queued.refresh_from_db()
    assert queued.pinned_runner_id == two.pk and queued.status == Turn.QUEUED
    # The transfer turn (NO SNAPSHOT: the old member was asleep) runs first.
    first = Turn.objects.filter(chat_session=session, status=Turn.QUEUED).order_by("created_at").first()
    assert first.pk != queued.pk and first.pinned_runner_id == two.pk


def test_a_chat_on_a_member_that_still_answers_is_left_where_it_is(world, aws):
    aws.add(I2)
    aws.desired = 2
    _member(world["fleet"], I2)
    org = world["org"]
    session = Session.objects.create(workspace=org["ws"], created_by=org["user"], agent=world["agent"])
    RunnerBinding.objects.create(session=session, runner=world["one"], session_key="abc", host="h")
    Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="chat-1",
                        prompt="hello", enqueued_by=org["user"])
    aws.instances[I1]["lifecycle"] = "Warmed:Stopped"  # heartbeating still: its chats wait
    fleets.tick()
    session.runner_binding.refresh_from_db()
    assert session.runner_binding.runner_id == world["one"].pk


def test_one_fleets_aws_error_does_not_stop_another(world, monkeypatch):
    def boom(region):
        raise fleets.AwsError("throttled")
    monkeypatch.setattr(fleets, "get", boom)
    assert fleets.tick() == {}


# ---- a member is routed as its fleet ---------------------------------------------------

I3 = "i-0aaaabbbbccccdddd"


def _routes(runner):
    return (sorted(RunnerAssignment.objects.filter(runner=runner).values_list("agent__slug", "source", "enabled")),
            sorted(WorkspaceRunnerOrder.objects.filter(runner=runner).values_list("workspace_id", "enabled")))


def test_a_member_the_scaler_adds_joins_routed_as_the_fleet(world, sts, aws):
    WorkspaceRunnerOrder.objects.create(workspace=world["org"]["ws"], runner=world["one"], rank=0)
    fleets.tick()  # the fleet keeps its routing
    aws.add(I2)
    res = _join(sts, I2)
    assert res.status_code == 201, res.content
    new = Runner.objects.get(pk=res.json()["runner_id"])
    assert _routes(new) == _routes(world["one"]) == ([("echo", "", True)], [(world["org"]["ws"].slug, True)])
    # So the work it was added for reaches it.
    turn = _turn(world["agent"], "k1")
    assert turn.pk in fleets._covered([new], [turn])[new.pk]


def test_a_change_to_one_members_routing_reaches_every_member(world, aws):
    aws.add(I2)
    two = _member(world["fleet"], I2)
    fleets.tick()
    assert _routes(two) == _routes(world["one"])
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=world["org"]["ws"], owner=world["org"]["user"])
    RunnerAssignment.objects.create(agent=eva, runner=two, rank=1)                    # added on one
    RunnerAssignment.objects.filter(runner=world["one"], agent=world["agent"]).update(enabled=False)  # switched off on one
    fleets.tick()
    assert _routes(world["one"]) == _routes(two) == ([("echo", "", False), ("eva", "", True)], [])
    RunnerAssignment.objects.filter(runner=world["one"], agent=eva).delete()          # taken off one
    fleets.tick()
    assert _routes(world["one"]) == _routes(two) == ([("echo", "", False)], [])


def test_the_fleets_routing_outlives_every_member(world, sts, aws):
    fleets.tick()
    del aws.instances[I1]
    fleets.tick()
    # Retired as the retire route retires: its rows go, so no Runners tab lists it.
    assert _routes(world["one"]) == ([], [])
    aws.add(I3)
    res = _join(sts, I3)
    new = Runner.objects.get(pk=res.json()["runner_id"])
    assert _routes(new) == ([("echo", "", True)], [])
