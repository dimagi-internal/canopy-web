"""One approval pairs every isolation unit on a box (spec
2026-10-19-runner-isolation-units §2, plan Task 6.1 and 6.5).

A box of units makes ONE `pair-by-role` call with its instance-role proof and
every unit's public keys. canopy-web makes one runner per unit, each with an
ACTIVE software device key; a replaced instance re-attaches keeping every runner
id; and a box paired before units (one runner, an aws-iam key, a KMS ECDH key)
is ADOPTED: its runner id, its bindings and its zero-knowledge wraps survive.
"""
from __future__ import annotations

import pytest
from django.test import Client

from apps.harness import aws_iam_proof, device_keys
from apps.harness.models import Runner, RunnerDeviceKey, RunnerInvitation
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.test_canopy_vault_phase1b import ROLE, sts  # noqa: F401 — sts is a fixture
from tests.test_runner_invitations import (INVITES, PAIR, PAIR_HTU, _fresh_cache, _pat_client,  # noqa: F401
                                           _person, _proof, org)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("sts")]

MACHINE = ROLE  # canonical role ARN == the machine id
BOX = "cloud-ec2-9"


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


UNITS = [
    {"name": BOX, "workspace": "connect", "kind": "tree"},
    {"name": f"{BOX}-marsh", "workspace": "marsh", "kind": "tree"},
    {"name": f"{BOX}-agent-eva", "workspace": "connect", "kind": "agent", "slug": "eva"},
]


def _invite(org, units=UNITS, name=BOX, **extra):
    return _pat_client(org["pat"]).post(
        INVITES, data={"name": name, "workspace": "connect", "role_arn": ROLE, "units": units, **extra},
        content_type="application/json")


def _approve(org, inv_id):
    assert org["browser"].post(f"{INVITES}{inv_id}/approve").status_code == 200


def _pair_units(units: dict[str, dict], *, name=BOX):
    """What canopy-pair sends: the box's name, ONE role proof, every unit's keys."""
    body = {"name": name, "kind": "cloud", "proof": _proof(PAIR_HTU), "units": [
        {"name": n, "public_jwk": k["public_jwk"], "ecdh_public_jwk": k.get("ecdh_public_jwk"),
         "ecdh_store": "kms-broker", "host": "ip-10-0-0-9", "capabilities": {"sessions": True}}
        for n, k in units.items()]}
    return Client().post(PAIR, data=body, content_type="application/json")


def _keys(names) -> dict[str, dict]:
    return {n: {"public_jwk": _jwk(), "ecdh_public_jwk": _jwk()} for n in names}


# ---- the invitation lists the units ------------------------------------------------------

def test_an_invitation_lists_its_units(two_trees):
    res = _invite(two_trees)
    assert res.status_code == 201, res.content
    body = res.json()
    assert [(u["name"], u["workspace"], u["kind"], u["slug"]) for u in body["units"]] == [
        (BOX, "connect", "tree", ""), (f"{BOX}-marsh", "marsh", "tree", ""),
        (f"{BOX}-agent-eva", "connect", "agent", "eva")]
    assert all(u["runner_id"] is None for u in body["units"])


def test_a_unit_in_a_workspace_the_inviter_cannot_run_work_in_is_refused(two_trees):
    WorkspaceMembership.objects.filter(workspace_id="marsh").update(role=WorkspaceMembership.VIEWER)
    assert _invite(two_trees).status_code == 403
    assert not RunnerInvitation.objects.exists()


def test_an_agent_unit_must_name_an_agent_of_its_workspace(two_trees):
    bad = [*UNITS[:2], {"name": f"{BOX}-agent-zed", "workspace": "connect", "kind": "agent", "slug": "zed"}]
    assert _invite(two_trees, units=bad).status_code == 422
    twice = [UNITS[0], {**UNITS[1], "name": BOX}]
    assert _invite(two_trees, units=twice).status_code == 422


# ---- one approval pairs every unit --------------------------------------------------------

def test_three_units_paired_by_one_approval(two_trees):
    from apps.harness.models import RunnerEnrollment

    inv = _invite(two_trees).json()
    keys = _keys([u["name"] for u in UNITS])
    assert _pair_units(keys).status_code == 202  # not approved yet: nothing made
    assert not Runner.objects.exists()
    _approve(two_trees, inv["id"])
    res = _pair_units(keys)
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["status"] == "paired" and set(body["units"]) == {u["name"] for u in UNITS}
    for unit in UNITS:
        runner = Runner.objects.get(pk=body["units"][unit["name"]])
        assert runner.owner == two_trees["user"] and runner.workspace_id == unit["workspace"]
        assert runner.machine_id == MACHINE and runner.kind == Runner.CLOUD
        assert list(RunnerEnrollment.objects.filter(runner=runner).values_list("workspace_id", flat=True)) \
            == [unit["workspace"]]
        key = RunnerDeviceKey.objects.get(runner=runner)
        assert key.kind == RunnerDeviceKey.SOFTWARE and key.status == RunnerDeviceKey.ACTIVE
        assert key.jkt == device_keys.thumbprint(keys[unit["name"]]["public_jwk"])
        assert key.approved_by == two_trees["user"]
        # The ECDH key up.sh did not name waits for a person.
        assert key.ecdh_store == "kms-broker" and key.ecdh_waiting
    # The unit is canopy-web's, from the approved invitation — not the box's word.
    assert Runner.objects.get(name=f"{BOX}-agent-eva").unit() == {"kind": "agent", "slug": "eva"}
    assert Runner.objects.get(name=f"{BOX}-marsh").unit() == {"kind": "tree", "root": "marsh"}
    got = _pat_client(two_trees["pat"]).get(f"{INVITES}{inv['id']}").json()
    assert got["status"] == "consumed"
    assert {u["name"]: u["runner_id"] for u in got["units"]} == body["units"]
    # No role key: the instance role is root's and is never a unit's device key.
    assert not RunnerDeviceKey.objects.filter(kind=RunnerDeviceKey.AWS_IAM).exists()


def test_a_named_unit_ecdh_key_is_approved_with_the_invitation(two_trees):
    keys = _keys([u["name"] for u in UNITS])
    units = [{**u, "ecdh_public_jwk": keys[u["name"]]["ecdh_public_jwk"]} for u in UNITS]
    _approve(two_trees, _invite(two_trees, units=units).json()["id"])
    assert _pair_units(keys).status_code == 201
    assert all(k.ecdh_ready for k in RunnerDeviceKey.objects.all())


def test_reattach_keeps_the_runner_ids(two_trees):
    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_units(_keys([u["name"] for u in UNITS])).json()["units"]
    wraps_on = {n: RunnerDeviceKey.objects.get(runner_id=rid).pk for n, rid in first.items()}
    # A replaced instance: new software keys in new homes, the SAME KMS ECDH keys.
    ecdh = {n: RunnerDeviceKey.objects.get(runner_id=rid).ecdh_public_jwk for n, rid in first.items()}
    fresh = {n: {"public_jwk": _jwk(), "ecdh_public_jwk": ecdh[n]} for n in first}
    res = _pair_units(fresh)
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "reattached" and res.json()["units"] == first
    assert Runner.objects.count() == 3 and RunnerDeviceKey.objects.count() == 3
    for n, rid in first.items():
        key = RunnerDeviceKey.objects.get(runner_id=rid)
        assert key.pk == wraps_on[n]  # the row (and every wrap on it) is kept
        assert key.jkt == device_keys.thumbprint(fresh[n]["public_jwk"])


def test_the_old_instances_tokens_end_with_its_key(two_trees):
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_units(_keys([u["name"] for u in UNITS])).json()["units"]
    key = RunnerDeviceKey.objects.get(runner_id=first[BOX])
    RunnerAccessToken.mint(key)
    _pair_units(_keys(list(first)))
    assert not RunnerAccessToken.objects.filter(device_key=key).exists()


def test_a_unit_whose_creator_lacks_agent_work_refuses_the_whole_pairing(two_trees):
    _approve(two_trees, _invite(two_trees).json()["id"])
    WorkspaceMembership.objects.filter(workspace_id="marsh").update(role=WorkspaceMembership.VIEWER)
    res = _pair_units(_keys([u["name"] for u in UNITS]))
    assert res.status_code == 401
    assert not Runner.objects.exists() and not RunnerDeviceKey.objects.exists()


def _uninvited_unit(org):
    _approve(org, _invite(org).json()["id"])
    return {**_keys([u["name"] for u in UNITS]), f"{BOX}-stowaway": {"public_jwk": _jwk()}}


def _missing_unit(org):
    _approve(org, _invite(org).json()["id"])
    return _keys([u["name"] for u in UNITS[:2]])


def _no_invitation(org):
    return _keys([u["name"] for u in UNITS])


def _same_key_twice(org):
    _approve(org, _invite(org).json()["id"])
    one = _jwk()
    return {u["name"]: {"public_jwk": one} for u in UNITS}


def _bad_key(org):
    _approve(org, _invite(org).json()["id"])
    keys = _keys([u["name"] for u in UNITS])
    keys[BOX]["public_jwk"] = {"kty": "EC", "crv": "P-256", "x": "AAAA", "y": "AAAA"}
    return keys


def _name_on_another_machine(org):
    Runner.objects.create(name=f"{BOX}-marsh", kind=Runner.CLOUD, owner=org["user"], workspace_id="connect",
                          machine_id="arn:aws:iam::123456789012:role/another-box")
    _approve(org, _invite(org).json()["id"])
    return _keys([u["name"] for u in UNITS])


@pytest.mark.parametrize("setup", [_uninvited_unit, _missing_unit, _no_invitation, _same_key_twice, _bad_key,
                                   _name_on_another_machine])
def test_unit_refusals_are_uniform_and_create_nothing(two_trees, setup):
    keys = setup(two_trees)
    runners, dkeys = Runner.objects.count(), RunnerDeviceKey.objects.count()
    res = _pair_units(keys)
    assert res.status_code == 401
    assert res.json()["detail"] == "no approved pairing invitation matches this box's instance role and name"
    assert Runner.objects.count() == runners and RunnerDeviceKey.objects.count() == dkeys


def test_a_revoked_unit_key_is_not_laundered_by_reattaching(two_trees):
    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_units(_keys([u["name"] for u in UNITS])).json()["units"]
    RunnerDeviceKey.objects.filter(runner_id=first[f"{BOX}-marsh"]).update(status=RunnerDeviceKey.REVOKED)
    res = _pair_units(_keys(list(first)))
    assert res.status_code == 401
    assert RunnerDeviceKey.objects.get(runner_id=first[f"{BOX}-marsh"]).status == RunnerDeviceKey.REVOKED


# ---- adopting a box paired before units (Review Focus 1, plan Task 6.5) --------------------

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


LIVE_UNIT = [{"name": "cloud-ec2-2", "workspace": "connect", "kind": "tree"}]


def test_the_live_single_unit_box_is_adopted_keeping_its_id_bindings_and_wraps(
        live_box, django_capture_on_commit_callbacks):
    """Moving cloud-ec2-2 to units: its default unit is `tree:connect`, named as the
    box, with its existing KMS key (now served by the broker). Adopting turns its
    instance-role key into a unit key, so it takes the approved invitation that
    `up.sh --in-place` makes, listing that unit."""
    from django.core import mail

    from apps.agents.models import AgentVaultWrap
    from apps.canopy_sessions.models import RunnerBinding
    from apps.events.models import Activity

    inv = _invite(live_box, units=LIVE_UNIT, name="cloud-ec2-2").json()
    _approve(live_box, inv["id"])
    mail.outbox.clear()
    unit_key = _jwk()
    with django_capture_on_commit_callbacks(execute=True):
        res = _pair_units({"cloud-ec2-2": {"public_jwk": unit_key, "ecdh_public_jwk": live_box["kms"]}},
                          name="cloud-ec2-2")
    assert res.status_code == 200, res.content
    assert RunnerInvitation.objects.get(pk=inv["id"]).status == "consumed"
    # Every re-key is seen: an activity, and the runner's approvers are told.
    rekey = Activity.objects.get(kind="runner.rekeyed")
    assert rekey.meta["adopted"] is True and rekey.ref == str(live_box["runner"].pk)
    assert [m.to for m in mail.outbox] == [["jj@dimagi.com"]] and "new key" in mail.outbox[0].subject
    assert res.json() == {"status": "reattached", "runner_id": str(live_box["runner"].pk),
                          "units": {"cloud-ec2-2": str(live_box["runner"].pk)},
                          "device_keys": {"cloud-ec2-2": str(live_box["key"].pk)}}
    key = RunnerDeviceKey.objects.get()
    assert key.pk == live_box["key"].pk and key.runner_id == live_box["runner"].pk
    assert key.kind == RunnerDeviceKey.SOFTWARE and key.role_arn == ""
    assert key.jkt == device_keys.thumbprint(unit_key) and key.status == RunnerDeviceKey.ACTIVE
    # The ECDH key is the same KMS key: same jkt, still approved, wraps intact.
    assert key.ecdh_jkt == device_keys.thumbprint(live_box["kms"]) and key.ecdh_ready
    assert key.ecdh_store == "kms-broker"
    assert AgentVaultWrap.objects.filter(pk=live_box["wrap"].pk, device_key=key).exists()
    assert RunnerBinding.objects.filter(pk=live_box["binding"].pk, runner=live_box["runner"]).exists()
    assert Runner.objects.get(pk=live_box["runner"].pk).machine_id == MACHINE
    # It now signs in with the unit key, not the role.
    assert aws_iam_proof.known_key(ROLE) is None


def test_adopting_the_live_box_and_adding_units_takes_one_approval(live_box):
    from apps.agents.models import AgentVaultWrap

    Workspace.objects.create(slug="marsh", display_name="Marsh", created_by=live_box["user"])
    WorkspaceMembership.objects.create(workspace_id="marsh", user=live_box["user"],
                                       role=WorkspaceMembership.EDITOR)
    units = [{"name": "cloud-ec2-2", "workspace": "connect", "kind": "tree"},
             {"name": "cloud-ec2-2-marsh", "workspace": "marsh", "kind": "tree"}]
    inv = _invite(live_box, units=units, name="cloud-ec2-2").json()
    keys = {"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]},
            "cloud-ec2-2-marsh": {"public_jwk": _jwk(), "ecdh_public_jwk": _jwk()}}
    assert _pair_units(keys, name="cloud-ec2-2").status_code == 202
    assert RunnerDeviceKey.objects.get().kind == RunnerDeviceKey.AWS_IAM  # nothing touched while waiting
    _approve(live_box, inv["id"])
    res = _pair_units(keys, name="cloud-ec2-2")
    assert res.status_code == 201, res.content
    assert res.json()["units"]["cloud-ec2-2"] == str(live_box["runner"].pk)
    assert Runner.objects.count() == 2
    assert AgentVaultWrap.objects.filter(pk=live_box["wrap"].pk).exists()


def test_adoption_with_a_different_ecdh_key_waits_for_a_person_and_drops_stale_wraps(live_box):
    from apps.agents.models import AgentVaultWrap

    _approve(live_box, _invite(live_box, units=LIVE_UNIT, name="cloud-ec2-2").json()["id"])
    res = _pair_units({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": _jwk()}}, name="cloud-ec2-2")
    assert res.status_code == 200
    key = RunnerDeviceKey.objects.get()
    assert key.ecdh_waiting and not AgentVaultWrap.objects.exists()


def test_a_single_runner_pairing_is_unchanged_by_units(org):
    """A box on older runner code (no `units`) pairs by role exactly as before."""
    from tests.test_runner_invitations import _approved, _pair

    _approved(org)
    res = _pair()
    assert res.status_code == 201
    assert res.json()["units"] == {}
    assert RunnerDeviceKey.objects.get().kind == RunnerDeviceKey.AWS_IAM


def test_pat_is_never_needed(two_trees):
    """Pairing a box of units sends no Authorization header at all."""
    PersonalToken.objects.all().delete()
    inv = RunnerInvitation.objects.create(
        name=BOX, workspace=two_trees["ws"], created_by=two_trees["user"], role_arn=ROLE,
        role_jkt=aws_iam_proof.role_jkt(ROLE), approved_by=two_trees["user"],
        approved_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
        units=[{"name": BOX, "workspace": "connect", "kind": "tree", "slug": ""}])
    assert _pair_units(_keys([BOX])).status_code == 201
    assert RunnerInvitation.objects.get(pk=inv.pk).status == "consumed"


def test_a_capabilities_write_that_omits_the_unit_keeps_it(two_trees):
    """The box re-declares its capabilities on every start; the unit canopy-web
    recorded from the invitation is not lost when it omits it."""
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    rid = _pair_units(_keys([u["name"] for u in UNITS])).json()["units"][f"{BOX}-agent-eva"]
    raw, _ = RunnerAccessToken.mint(RunnerDeviceKey.objects.get(runner_id=rid))
    res = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").patch(
        f"/api/harness/runners/{rid}", data={"capabilities": {"sessions": True}},
        content_type="application/json")
    assert res.status_code == 200, res.content
    assert Runner.objects.get(pk=rid).unit() == {"kind": "agent", "slug": "eva"}



# ---- a bare role proof never takes a runner's identity (review: HIGH) -------------------

def test_a_role_proof_alone_cannot_rekey_the_live_box(live_box):
    """The live box has no IMDS rule: any agent turn on it can mint the instance
    role's proof. Without an approved invitation that proof must not turn the
    runner's role key into a key the attacker holds."""
    from apps.events.models import Activity

    before = RunnerDeviceKey.objects.get()
    res = _pair_units({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]}},
                      name="cloud-ec2-2")
    assert res.status_code == 401
    after = RunnerDeviceKey.objects.get()
    assert (after.kind, after.jkt, after.role_arn) == (RunnerDeviceKey.AWS_IAM, before.jkt, ROLE)
    assert aws_iam_proof.known_key(ROLE) is not None
    assert not Activity.objects.filter(kind="runner.rekeyed").exists()
    # An invitation not yet approved changes nothing either.
    _invite(live_box, units=LIVE_UNIT, name="cloud-ec2-2")
    assert _pair_units({"cloud-ec2-2": {"public_jwk": _jwk()}}, name="cloud-ec2-2").status_code == 202
    assert RunnerDeviceKey.objects.get().kind == RunnerDeviceKey.AWS_IAM


def test_an_invitation_that_does_not_list_the_unit_cannot_adopt_it(live_box):
    Workspace.objects.create(slug="marsh", display_name="Marsh", created_by=live_box["user"])
    WorkspaceMembership.objects.create(workspace_id="marsh", user=live_box["user"],
                                       role=WorkspaceMembership.EDITOR)
    other = [{"name": "cloud-ec2-2-marsh", "workspace": "marsh", "kind": "tree"}]
    _approve(live_box, _invite(live_box, units=other, name="cloud-ec2-2").json()["id"])
    res = _pair_units({"cloud-ec2-2": {"public_jwk": _jwk()}, "cloud-ec2-2-marsh": {"public_jwk": _jwk()}},
                      name="cloud-ec2-2")
    assert res.status_code == 401
    assert RunnerDeviceKey.objects.filter(kind=RunnerDeviceKey.AWS_IAM).count() == 1


def test_a_unit_reattaching_on_its_machine_is_announced(two_trees, django_capture_on_commit_callbacks):
    from django.core import mail

    from apps.events.models import Activity

    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_units(_keys([u["name"] for u in UNITS])).json()["units"]
    mail.outbox.clear()
    same_ecdh = {n: {"public_jwk": _jwk(), "ecdh_public_jwk": RunnerDeviceKey.objects.get(runner_id=r).ecdh_public_jwk}
                 for n, r in first.items()}
    with django_capture_on_commit_callbacks(execute=True):
        assert _pair_units(same_ecdh).status_code == 200
    assert Activity.objects.filter(kind="runner.rekeyed").count() == 3
    assert all(not a.meta["adopted"] for a in Activity.objects.filter(kind="runner.rekeyed"))
    assert len(mail.outbox) == 3
    # The same keys again (a restart before the state file landed) is not a re-key.
    Activity.objects.filter(kind="runner.rekeyed").delete()


def test_a_unit_on_another_machine_id_needs_an_invitation(two_trees):
    """A software key alone is not enough: the runner must be on THIS machine."""
    _approve(two_trees, _invite(two_trees).json()["id"])
    first = _pair_units(_keys([u["name"] for u in UNITS])).json()["units"]
    Runner.objects.filter(pk=first[BOX]).update(machine_id="")
    res = _pair_units(_keys(list(first)))
    assert res.status_code == 401


def test_a_box_cannot_replace_or_clear_its_server_owned_unit(two_trees):
    from apps.harness.models import RunnerAccessToken

    _approve(two_trees, _invite(two_trees).json()["id"])
    rid = _pair_units(_keys([u["name"] for u in UNITS])).json()["units"][f"{BOX}-agent-eva"]
    raw, _ = RunnerAccessToken.mint(RunnerDeviceKey.objects.get(runner_id=rid))
    client = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    for sent in ({"kind": "tree", "root": "connect"}, {}, None):
        res = client.patch(f"/api/harness/runners/{rid}", data={"capabilities": {"unit": sent}},
                           content_type="application/json")
        assert res.status_code == 200, res.content
        assert Runner.objects.get(pk=rid).unit() == {"kind": "agent", "slug": "eva"}


def test_adopting_as_an_agent_unit_restricts_the_runner_at_once(live_box):
    """The unit an adopted runner becomes is the invitation's, set by canopy-web in
    the same call: adopted as the agent unit for eva, it runs nothing else."""
    from apps.harness import routing

    unit = [{"name": "cloud-ec2-2", "workspace": "connect", "kind": "agent", "slug": "eva"}]
    _approve(live_box, _invite(live_box, units=unit, name="cloud-ec2-2").json()["id"])
    res = _pair_units({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]}},
                      name="cloud-ec2-2")
    assert res.status_code == 200, res.content
    runner = Runner.objects.get(pk=live_box["runner"].pk)
    assert runner.unit() == {"kind": "agent", "slug": "eva"}
    assert routing.unit_refusal(runner, "ada") and not routing.unit_refusal(runner, "eva")


def test_adopting_as_a_tree_unit_records_its_root(live_box):
    _approve(live_box, _invite(live_box, units=LIVE_UNIT, name="cloud-ec2-2").json()["id"])
    _pair_units({"cloud-ec2-2": {"public_jwk": _jwk(), "ecdh_public_jwk": live_box["kms"]}}, name="cloud-ec2-2")
    assert Runner.objects.get(pk=live_box["runner"].pk).unit() == {"kind": "tree", "root": "connect"}
