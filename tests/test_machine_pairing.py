"""One approval pairs every runner on a machine
(`docs/architecture/runners.md § Several runners on one machine`).

A machine running several runners makes ONE `pair-by-role` call with its
instance-role proof and every runner's public keys. canopy-web makes each runner,
each with an ACTIVE software device key; a replaced instance re-attaches keeping
every runner id; and a box paired before it ran several runners (one runner, an aws-iam key, a KMS ECDH key)
is ADOPTED: its runner id, its bindings and its zero-knowledge wraps survive.
"""
from __future__ import annotations

import pytest
from django.test import Client

from apps.harness import aws_iam_proof, device_keys
from apps.harness.models import Runner, RunnerDeviceKey, RunnerInvitation
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.aws_proof import ROLE
from tests.fixtures.runner_invitations import INVITES, _pat_client
from tests.fixtures.runner_invitations import make_org
from tests.fixtures.machine_runners import BOX, RUNNERS, _approve, _invite, _pair_runners

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("sts", "fresh_cache")]


@pytest.fixture
def org():
    return make_org()

MACHINE = ROLE  # canonical role ARN == the machine id


def _jwk() -> dict:
    from canopy_sdk.keys import header_jwk
    from cryptography.hazmat.primitives.asymmetric import ec

    return device_keys.normalize_public_jwk(header_jwk(ec.generate_private_key(ec.SECP256R1())))


@pytest.fixture
def two_trees(org):
    """`connect` (org's, owner jj) and a SEPARATE root `marsh`, jj an editor there,
    with an agent `eva` in connect."""
    from apps.agents.models import Agent

    marsh = Workspace.objects.create(slug="marsh", display_name="Marsh", created_by=org["user"])
    WorkspaceMembership.objects.create(workspace=marsh, user=org["user"], role=WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="eva", name="Eva", workspace=org["ws"])
    return org


def _keys(names) -> dict[str, dict]:
    return {n: {"public_jwk": _jwk(), "ecdh_public_jwk": _jwk()} for n in names}


# ---- the invitation lists the runners ----------------------------------------------------

def test_an_invitation_lists_its_runners(two_trees):
    res = _invite(two_trees)
    assert res.status_code == 201, res.content
    body = res.json()
    assert [(u["name"], u["workspace"], u["kind"], u["slug"]) for u in body["runners"]] == [
        (BOX, "connect", "tree", ""), (f"{BOX}-marsh", "marsh", "tree", ""),
        (f"{BOX}-agent-eva", "connect", "agent", "eva")]
    assert all(u["runner_id"] is None for u in body["runners"])


def test_a_runner_in_a_workspace_the_inviter_cannot_run_work_in_is_refused(two_trees):
    WorkspaceMembership.objects.filter(workspace_id="marsh").update(role=WorkspaceMembership.VIEWER)
    assert _invite(two_trees).status_code == 403
    assert not RunnerInvitation.objects.exists()


def test_an_agent_runner_must_name_an_agent_of_its_workspace(two_trees):
    bad = [*RUNNERS[:2], {"name": f"{BOX}-agent-zed", "workspace": "connect", "kind": "agent", "slug": "zed"}]
    assert _invite(two_trees, runners=bad).status_code == 422
    twice = [RUNNERS[0], {**RUNNERS[1], "name": BOX}]
    assert _invite(two_trees, runners=twice).status_code == 422


# ---- one approval pairs every runner -----------------------------------------------------

def test_three_runners_paired_by_one_approval(two_trees):
    from apps.harness.models import RunnerEnrollment

    inv = _invite(two_trees).json()
    keys = _keys([u["name"] for u in RUNNERS])
    assert _pair_runners(keys).status_code == 202  # not approved yet: nothing made
    assert not Runner.objects.exists()
    _approve(two_trees, inv["id"])
    res = _pair_runners(keys)
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["status"] == "paired" and set(body["runners"]) == {u["name"] for u in RUNNERS}
    for invited in RUNNERS:
        runner = Runner.objects.get(pk=body["runners"][invited["name"]])
        assert runner.owner == two_trees["user"] and runner.workspace_id == invited["workspace"]
        assert runner.machine_id == MACHINE and runner.kind == Runner.CLOUD
        assert list(RunnerEnrollment.objects.filter(runner=runner).values_list("workspace_id", flat=True)) \
            == [invited["workspace"]]
        key = RunnerDeviceKey.objects.get(runner=runner)
        assert key.kind == RunnerDeviceKey.SOFTWARE and key.status == RunnerDeviceKey.ACTIVE
        assert key.jkt == device_keys.thumbprint(keys[invited["name"]]["public_jwk"])
        assert key.approved_by == two_trees["user"]
        # The ECDH key up.sh did not name waits for a person.
        assert key.ecdh_store == "kms-broker" and key.ecdh_waiting
    # What it serves is canopy-web's, from the approved invitation — not the box's word.
    assert Runner.objects.get(name=f"{BOX}-agent-eva").serves() == {"kind": "agent", "slug": "eva"}
    assert Runner.objects.get(name=f"{BOX}-marsh").serves() == {"kind": "tree", "root": "marsh"}
    got = _pat_client(two_trees["pat"]).get(f"{INVITES}{inv['id']}").json()
    assert got["status"] == "consumed"
    assert {u["name"]: u["runner_id"] for u in got["runners"]} == body["runners"]
    # No role key: the instance role is root's and is never a runner's device key.
    assert not RunnerDeviceKey.objects.filter(kind=RunnerDeviceKey.AWS_IAM).exists()


def test_a_named_runner_ecdh_key_is_approved_with_the_invitation(two_trees):
    keys = _keys([u["name"] for u in RUNNERS])
    runners = [{**u, "ecdh_public_jwk": keys[u["name"]]["ecdh_public_jwk"]} for u in RUNNERS]
    _approve(two_trees, _invite(two_trees, runners=runners).json()["id"])
    assert _pair_runners(keys).status_code == 201
    assert all(k.ecdh_ready for k in RunnerDeviceKey.objects.all())


def test_reattach_keeps_the_runner_ids(two_trees):
    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_runners(_keys([u["name"] for u in RUNNERS])).json()["runners"]
    wraps_on = {n: RunnerDeviceKey.objects.get(runner_id=rid).pk for n, rid in first.items()}
    # A replaced instance: new software keys in new homes, the SAME KMS ECDH keys.
    ecdh = {n: RunnerDeviceKey.objects.get(runner_id=rid).ecdh_public_jwk for n, rid in first.items()}
    fresh = {n: {"public_jwk": _jwk(), "ecdh_public_jwk": ecdh[n]} for n in first}
    res = _pair_runners(fresh)
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "reattached" and res.json()["runners"] == first
    assert Runner.objects.count() == 3 and RunnerDeviceKey.objects.count() == 3
    for n, rid in first.items():
        key = RunnerDeviceKey.objects.get(runner_id=rid)
        assert key.pk == wraps_on[n]  # the row (and every wrap on it) is kept
        assert key.jkt == device_keys.thumbprint(fresh[n]["public_jwk"])


def test_the_old_instances_tokens_end_with_its_key(two_trees):
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_runners(_keys([u["name"] for u in RUNNERS])).json()["runners"]
    key = RunnerDeviceKey.objects.get(runner_id=first[BOX])
    RunnerAccessToken.mint(key)
    _pair_runners(_keys(list(first)))
    assert not RunnerAccessToken.objects.filter(device_key=key).exists()


def test_a_runner_whose_creator_lacks_agent_work_refuses_the_whole_pairing(two_trees):
    _approve(two_trees, _invite(two_trees).json()["id"])
    WorkspaceMembership.objects.filter(workspace_id="marsh").update(role=WorkspaceMembership.VIEWER)
    res = _pair_runners(_keys([u["name"] for u in RUNNERS]))
    assert res.status_code == 401
    assert not Runner.objects.exists() and not RunnerDeviceKey.objects.exists()


def _uninvited_runner(org):
    _approve(org, _invite(org).json()["id"])
    return {**_keys([u["name"] for u in RUNNERS]), f"{BOX}-stowaway": {"public_jwk": _jwk()}}


def _missing_runner(org):
    _approve(org, _invite(org).json()["id"])
    return _keys([u["name"] for u in RUNNERS[:2]])


def _no_invitation(org):
    return _keys([u["name"] for u in RUNNERS])


def _same_key_twice(org):
    _approve(org, _invite(org).json()["id"])
    one = _jwk()
    return {u["name"]: {"public_jwk": one} for u in RUNNERS}


def _bad_key(org):
    _approve(org, _invite(org).json()["id"])
    keys = _keys([u["name"] for u in RUNNERS])
    keys[BOX]["public_jwk"] = {"kty": "EC", "crv": "P-256", "x": "AAAA", "y": "AAAA"}
    return keys


def _name_on_another_machine(org):
    Runner.objects.create(name=f"{BOX}-marsh", kind=Runner.CLOUD, owner=org["user"], workspace_id="connect",
                          machine_id="arn:aws:iam::123456789012:role/another-box")
    _approve(org, _invite(org).json()["id"])
    return _keys([u["name"] for u in RUNNERS])


@pytest.mark.parametrize("setup", [_uninvited_runner, _missing_runner, _no_invitation, _same_key_twice, _bad_key,
                                   _name_on_another_machine])
def test_pairing_refusals_are_uniform_and_create_nothing(two_trees, setup):
    keys = setup(two_trees)
    runners, dkeys = Runner.objects.count(), RunnerDeviceKey.objects.count()
    res = _pair_runners(keys)
    assert res.status_code == 401
    assert res.json()["detail"] == "no approved pairing invitation matches this box's instance role and name"
    assert Runner.objects.count() == runners and RunnerDeviceKey.objects.count() == dkeys


def test_a_revoked_runner_key_is_not_laundered_by_reattaching(two_trees):
    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_runners(_keys([u["name"] for u in RUNNERS])).json()["runners"]
    RunnerDeviceKey.objects.filter(runner_id=first[f"{BOX}-marsh"]).update(status=RunnerDeviceKey.REVOKED)
    res = _pair_runners(_keys(list(first)))
    assert res.status_code == 401
    assert RunnerDeviceKey.objects.get(runner_id=first[f"{BOX}-marsh"]).status == RunnerDeviceKey.REVOKED


# ---- adopting a box paired before several runners (Review Focus 1, plan Task 6.5) --------

@pytest.fixture
def live_box(org):
    """cloud-ec2-2 as it is today: one runner paired by role (an ACTIVE aws-iam key
    whose ECDH key is the `<stack>-ecdh` KMS key, approved), a binding, and a
    zero-knowledge wrap of an agent's vault key on that device key."""
    from django.utils import timezone

    from apps.agents.models import Agent, AgentVaultWrap
    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.harness.runners import backfill_machine_ids

    runner = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=org["user"],
                                   workspace_id="connect", capabilities={"sessions": True})
    kms = _jwk()
    key = RunnerDeviceKey.objects.create(
        runner=runner, kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE, jkt=aws_iam_proof.role_jkt(ROLE),
        status=RunnerDeviceKey.ACTIVE, approved_by=org["user"], approved_at=timezone.now(),
        ecdh_public_jwk=kms, ecdh_jkt=device_keys.thumbprint(kms), ecdh_store="aws-kms",
        ecdh_approved_at=timezone.now(), ecdh_approved_by=org["user"])
    backfill_machine_ids(Runner, RunnerDeviceKey)  # the migration PR 4 shipped
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=org["ws"])
    wrap = AgentVaultWrap.objects.create(agent=agent, kv=1, kind=AgentVaultWrap.DEVICE, device_key=key,
                                         wrap={"alg": "ECDH-ES", "epk": {}, "ct": "x"})
    session = Session.objects.create(workspace=org["ws"], created_by=org["user"], title="chat")
    binding = RunnerBinding.objects.create(session=session, runner=runner, session_key="sess-1")
    return {"runner": runner, "key": key, "kms": kms, "wrap": wrap, "binding": binding, **org}


LIVE_RUNNER = [{"name": "cloud-ec2-2", "workspace": "connect", "kind": "tree"}]


def test_the_live_single_runner_box_is_adopted_keeping_its_id_bindings_and_wraps(
        live_box, django_capture_on_commit_callbacks):
    """Moving cloud-ec2-2 to several runners: its default runner serves `tree:connect`,
    named as the box, with its existing KMS key (now served by the broker). Adopting
    turns its instance-role key into a runner key, so it takes the approved invitation
    that `up.sh --in-place` makes, listing that runner."""
    from django.core import mail

    from apps.agents.models import AgentVaultWrap
    from apps.canopy_sessions.models import RunnerBinding
    from apps.events.models import Activity

    inv = _invite(live_box, runners=LIVE_RUNNER, name="cloud-ec2-2").json()
    _approve(live_box, inv["id"])
    mail.outbox.clear()
    runner_key = _jwk()
    with django_capture_on_commit_callbacks(execute=True):
        res = _pair_runners({"cloud-ec2-2": {"public_jwk": runner_key, "ecdh_public_jwk": live_box["kms"]}},
                          name="cloud-ec2-2")
    assert res.status_code == 200, res.content
    assert RunnerInvitation.objects.get(pk=inv["id"]).status == "consumed"
    # Every re-key is seen: an activity, and the runner's approvers are told.
    rekey = Activity.objects.get(kind="runner.rekeyed")
    assert rekey.meta["adopted"] is True and rekey.ref == str(live_box["runner"].pk)
    assert [m.to for m in mail.outbox] == [["jj@dimagi.com"]] and "new key" in mail.outbox[0].subject
    assert res.json() == {"status": "reattached", "runner_id": str(live_box["runner"].pk),
                          "runners": {"cloud-ec2-2": str(live_box["runner"].pk)},
                          "units": {"cloud-ec2-2": str(live_box["runner"].pk)},
                          "device_keys": {"cloud-ec2-2": str(live_box["key"].pk)}}
    key = RunnerDeviceKey.objects.get()
    assert key.pk == live_box["key"].pk and key.runner_id == live_box["runner"].pk
    assert key.kind == RunnerDeviceKey.SOFTWARE and key.role_arn == ""
    assert key.jkt == device_keys.thumbprint(runner_key) and key.status == RunnerDeviceKey.ACTIVE
    # The ECDH key is the same KMS key: same jkt, still approved, wraps intact.
    assert key.ecdh_jkt == device_keys.thumbprint(live_box["kms"]) and key.ecdh_ready
    assert key.ecdh_store == "kms-broker"
    assert AgentVaultWrap.objects.filter(pk=live_box["wrap"].pk, device_key=key).exists()
    assert RunnerBinding.objects.filter(pk=live_box["binding"].pk, runner=live_box["runner"]).exists()
    assert Runner.objects.get(pk=live_box["runner"].pk).machine_id == MACHINE
    # It now signs in with the runner key, not the role.
    assert aws_iam_proof.known_key(ROLE) is None


def test_adopting_the_live_box_and_adding_runners_takes_one_approval(live_box):
    from apps.agents.models import AgentVaultWrap

    Workspace.objects.create(slug="marsh", display_name="Marsh", created_by=live_box["user"])
    WorkspaceMembership.objects.create(workspace_id="marsh", user=live_box["user"],
                                       role=WorkspaceMembership.EDITOR)
    runners = [{"name": "cloud-ec2-2", "workspace": "connect", "kind": "tree"},
             {"name": "cloud-ec2-2-marsh", "workspace": "marsh", "kind": "tree"}]
    inv = _invite(live_box, runners=runners, name="cloud-ec2-2").json()
    keys = {"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]},
            "cloud-ec2-2-marsh": {"public_jwk": _jwk(), "ecdh_public_jwk": _jwk()}}
    assert _pair_runners(keys, name="cloud-ec2-2").status_code == 202
    assert RunnerDeviceKey.objects.get().kind == RunnerDeviceKey.AWS_IAM  # nothing touched while waiting
    _approve(live_box, inv["id"])
    res = _pair_runners(keys, name="cloud-ec2-2")
    assert res.status_code == 201, res.content
    assert res.json()["runners"]["cloud-ec2-2"] == str(live_box["runner"].pk)
    assert Runner.objects.count() == 2
    assert AgentVaultWrap.objects.filter(pk=live_box["wrap"].pk).exists()


def test_adoption_with_a_different_ecdh_key_waits_for_a_person_and_drops_stale_wraps(live_box):
    from apps.agents.models import AgentVaultWrap

    _approve(live_box, _invite(live_box, runners=LIVE_RUNNER, name="cloud-ec2-2").json()["id"])
    res = _pair_runners({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": _jwk()}}, name="cloud-ec2-2")
    assert res.status_code == 200
    key = RunnerDeviceKey.objects.get()
    assert key.ecdh_waiting and not AgentVaultWrap.objects.exists()


def test_a_single_runner_pairing_is_unchanged_by_runners(org):
    """A box that pairs as one runner (no `runners` list) pairs by role exactly as before."""
    from tests.fixtures.runner_invitations import _approved, _pair

    _approved(org)
    res = _pair()
    assert res.status_code == 201
    assert res.json()["runners"] == {}
    assert RunnerDeviceKey.objects.get().kind == RunnerDeviceKey.AWS_IAM


def test_pat_is_never_needed(two_trees):
    """Pairing a machine of several runners sends no Authorization header at all."""
    PersonalToken.objects.all().delete()
    inv = RunnerInvitation.objects.create(
        name=BOX, workspace=two_trees["ws"], created_by=two_trees["user"], role_arn=ROLE,
        role_jkt=aws_iam_proof.role_jkt(ROLE), approved_by=two_trees["user"],
        approved_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
        runners=[{"name": BOX, "workspace": "connect", "kind": "tree", "slug": ""}])
    assert _pair_runners(_keys([BOX])).status_code == 201
    assert RunnerInvitation.objects.get(pk=inv.pk).status == "consumed"


def test_a_capabilities_write_that_omits_what_it_serves_keeps_it(two_trees):
    """The box re-declares its capabilities on every start; what it serves, as canopy-web
    recorded from the invitation is not lost when it omits it."""
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    rid = _pair_runners(_keys([u["name"] for u in RUNNERS])).json()["runners"][f"{BOX}-agent-eva"]
    raw, _ = RunnerAccessToken.mint(RunnerDeviceKey.objects.get(runner_id=rid))
    res = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").patch(
        f"/api/harness/runners/{rid}", data={"capabilities": {"sessions": True}},
        content_type="application/json")
    assert res.status_code == 200, res.content
    assert Runner.objects.get(pk=rid).serves() == {"kind": "agent", "slug": "eva"}


# ---- a bare role proof never takes a runner's identity (review: HIGH) -------------------

def test_a_role_proof_alone_cannot_rekey_the_live_box(live_box):
    """The live box has no IMDS rule: any agent turn on it can mint the instance
    role's proof. Without an approved invitation that proof must not turn the
    runner's role key into a key the attacker holds."""
    from apps.events.models import Activity

    before = RunnerDeviceKey.objects.get()
    res = _pair_runners({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]}},
                      name="cloud-ec2-2")
    assert res.status_code == 401
    after = RunnerDeviceKey.objects.get()
    assert (after.kind, after.jkt, after.role_arn) == (RunnerDeviceKey.AWS_IAM, before.jkt, ROLE)
    assert aws_iam_proof.known_key(ROLE) is not None
    assert not Activity.objects.filter(kind="runner.rekeyed").exists()
    # An invitation not yet approved changes nothing either.
    _invite(live_box, runners=LIVE_RUNNER, name="cloud-ec2-2")
    assert _pair_runners({"cloud-ec2-2": {"public_jwk": _jwk()}}, name="cloud-ec2-2").status_code == 202
    assert RunnerDeviceKey.objects.get().kind == RunnerDeviceKey.AWS_IAM


def test_an_invitation_that_does_not_list_the_runner_cannot_adopt_it(live_box):
    Workspace.objects.create(slug="marsh", display_name="Marsh", created_by=live_box["user"])
    WorkspaceMembership.objects.create(workspace_id="marsh", user=live_box["user"],
                                       role=WorkspaceMembership.EDITOR)
    other = [{"name": "cloud-ec2-2-marsh", "workspace": "marsh", "kind": "tree"}]
    _approve(live_box, _invite(live_box, runners=other, name="cloud-ec2-2").json()["id"])
    res = _pair_runners({"cloud-ec2-2": {"public_jwk": _jwk()}, "cloud-ec2-2-marsh": {"public_jwk": _jwk()}},
                      name="cloud-ec2-2")
    assert res.status_code == 401
    assert RunnerDeviceKey.objects.filter(kind=RunnerDeviceKey.AWS_IAM).count() == 1


def test_a_runner_reattaching_on_its_machine_is_announced(two_trees, django_capture_on_commit_callbacks):
    from django.core import mail

    from apps.events.models import Activity

    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_runners(_keys([u["name"] for u in RUNNERS])).json()["runners"]
    mail.outbox.clear()
    same_ecdh = {n: {"public_jwk": _jwk(), "ecdh_public_jwk": RunnerDeviceKey.objects.get(runner_id=r).ecdh_public_jwk}
                 for n, r in first.items()}
    with django_capture_on_commit_callbacks(execute=True):
        assert _pair_runners(same_ecdh).status_code == 200
    assert Activity.objects.filter(kind="runner.rekeyed").count() == 3
    assert all(not a.meta["adopted"] for a in Activity.objects.filter(kind="runner.rekeyed"))
    assert len(mail.outbox) == 3
    # The same keys again (a restart before the state file landed) is not a re-key.
    Activity.objects.filter(kind="runner.rekeyed").delete()


def test_a_runner_on_another_machine_id_needs_an_invitation(two_trees):
    """A software key alone is not enough: the runner must be on THIS machine."""
    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_runners(_keys([u["name"] for u in RUNNERS])).json()["runners"]
    Runner.objects.filter(pk=first[BOX]).update(machine_id="")
    res = _pair_runners(_keys(list(first)))
    assert res.status_code == 401


def test_a_box_cannot_replace_or_clear_its_server_owned_serves(two_trees):
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    rid = _pair_runners(_keys([u["name"] for u in RUNNERS])).json()["runners"][f"{BOX}-agent-eva"]
    raw, _ = RunnerAccessToken.mint(RunnerDeviceKey.objects.get(runner_id=rid))
    client = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    for sent in ({"kind": "tree", "root": "connect"}, {}, None):
        res = client.patch(f"/api/harness/runners/{rid}", data={"capabilities": {"serves": sent}},
                           content_type="application/json")
        assert res.status_code == 200, res.content
        assert Runner.objects.get(pk=rid).serves() == {"kind": "agent", "slug": "eva"}


def test_adopting_as_an_agent_runner_restricts_the_runner_at_once(live_box):
    """What an adopted runner serves is the invitation's, set by canopy-web in
    the same call: adopted as the runner for agent eva, it runs nothing else."""
    from apps.harness import routing

    eva_only = [{"name": "cloud-ec2-2", "workspace": "connect", "kind": "agent", "slug": "eva"}]
    _approve(live_box, _invite(live_box, runners=eva_only, name="cloud-ec2-2").json()["id"])
    res = _pair_runners({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]}},
                      name="cloud-ec2-2")
    assert res.status_code == 200, res.content
    runner = Runner.objects.get(pk=live_box["runner"].pk)
    assert runner.serves() == {"kind": "agent", "slug": "eva"}
    assert routing.serves_refusal(runner, "ada") and not routing.serves_refusal(runner, "eva")


def test_adopting_as_a_tree_runner_records_its_root(live_box):
    _approve(live_box, _invite(live_box, runners=LIVE_RUNNER, name="cloud-ec2-2").json()["id"])
    _pair_runners({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]}}, name="cloud-ec2-2")
    assert Runner.objects.get(pk=live_box["runner"].pk).serves() == {"kind": "tree", "root": "connect"}


# ---- a test box is directed from the moment it pairs ------------------------------------

def test_an_invitation_for_a_test_box_pairs_it_directed(two_trees):
    inv = _invite(two_trees, runners=RUNNERS[:1], directed=True).json()
    assert inv["directed"] is True
    _approve(two_trees, inv["id"])
    # The box's own word is not read: it says nothing about being directed.
    res = _pair_runners(_keys([BOX]))
    assert res.status_code == 201, res.content
    runner = Runner.objects.get(pk=res.json()["runners"][BOX])
    assert runner.directed and runner.directed_only()


def test_an_ordinary_invitation_pairs_a_box_that_takes_routed_work(two_trees):
    inv = _invite(two_trees, runners=RUNNERS[:1]).json()
    _approve(two_trees, inv["id"])
    res = _pair_runners(_keys([BOX]))
    assert not Runner.objects.get(pk=res.json()["runners"][BOX]).directed_only()


# ---- the names from before the rename still work (remove in the follow-up) -------------
#
# A machine's runners were `units`, and what a runner serves was `capabilities["unit"]`.
# The server and the runners do not update at the same moment, so each side reads both
# names until every runner reports the new ones.

def test_an_invitation_and_a_pairing_sent_under_the_old_key_still_pair(two_trees):
    inv = _invite(two_trees, key="units")
    assert inv.status_code == 201, inv.content
    assert [r["name"] for r in inv.json()["runners"]] == [r["name"] for r in RUNNERS]
    _approve(two_trees, inv.json()["id"])
    res = _pair_runners(_keys([r["name"] for r in RUNNERS]), key="units")
    assert res.status_code == 201, res.content
    body = res.json()
    # A runner from before the rename reads `units` off the reply: it gets both.
    assert set(body["runners"]) == {r["name"] for r in RUNNERS} and body["units"] == body["runners"]
    assert Runner.objects.get(pk=body["runners"][f"{BOX}-agent-eva"]).serves() == {"kind": "agent", "slug": "eva"}


def test_a_runner_declaring_under_the_old_key_is_stored_under_the_new_one(two_trees):
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    rid = _pair_runners(_keys([r["name"] for r in RUNNERS])).json()["runners"][f"{BOX}-agent-eva"]
    raw, _ = RunnerAccessToken.mint(RunnerDeviceKey.objects.get(runner_id=rid))
    sent = {"sessions": True, "unit": {"kind": "agent", "slug": "eva"}}
    res = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").patch(
        f"/api/harness/runners/{rid}", data={"capabilities": sent}, content_type="application/json")
    assert res.status_code == 200, res.content
    caps = Runner.objects.get(pk=rid).capabilities
    assert caps["serves"] == {"kind": "agent", "slug": "eva"} and "unit" not in caps


def test_a_row_still_carrying_the_old_key_keeps_what_it_serves(two_trees):
    """A row written before the rename (or by a server still on the old code) says
    what it serves under `unit`: it is read, kept through a write, and moved to `serves`."""
    from apps.harness import routing
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    rid = _pair_runners(_keys([r["name"] for r in RUNNERS])).json()["runners"][f"{BOX}-agent-eva"]
    caps = dict(Runner.objects.get(pk=rid).capabilities)
    caps["unit"] = caps.pop("serves")
    Runner.objects.filter(pk=rid).update(capabilities=caps)
    runner = Runner.objects.get(pk=rid)
    assert runner.serves() == {"kind": "agent", "slug": "eva"}
    assert routing.serves_refusal(runner, "ada") and not routing.serves_refusal(runner, "eva")
    tree = Runner.objects.get(name=BOX)
    assert routing.serves_refusal(tree, "eva") == f"eva has its own runner on this machine ({BOX}-agent-eva)"

    raw, _ = RunnerAccessToken.mint(RunnerDeviceKey.objects.get(runner_id=rid))
    res = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").patch(
        f"/api/harness/runners/{rid}", data={"capabilities": {"serves": {}, "unit": None}},
        content_type="application/json")
    assert res.status_code == 200, res.content
    caps = Runner.objects.get(pk=rid).capabilities
    assert caps["serves"] == {"kind": "agent", "slug": "eva"} and "unit" not in caps


def test_the_migration_copies_the_old_key_and_keeps_it_for_a_server_mid_deploy(two_trees):
    import importlib

    from django.apps import apps as registry

    migration = importlib.import_module("apps.harness.migrations.0127_runner_serves_and_invited_runners")
    old = Runner.objects.create(name="old-row", kind=Runner.CLOUD, owner=two_trees["user"],
                                workspace=two_trees["ws"], capabilities={"unit": {"kind": "tree", "root": "connect"}})
    new = Runner.objects.create(name="new-row", kind=Runner.CLOUD, owner=two_trees["user"],
                                workspace=two_trees["ws"], capabilities={"serves": {"kind": "agent", "slug": "eva"}})
    migration.copy_serves(registry, None)
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.capabilities == {"unit": {"kind": "tree", "root": "connect"},
                                "serves": {"kind": "tree", "root": "connect"}}
    assert new.capabilities == {"serves": {"kind": "agent", "slug": "eva"}}
