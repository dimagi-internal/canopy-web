"""A new cloud box pairs by invitation: one passkey touch, and no PAT on the box.

The shared pairing PAT a cloud box used to carry existed only to get it through
pairing, and when it expired nobody was told. Now the operator invites the box
(`up.sh`, with their own token), approves the invitation in a browser behind a
passkey step-up, and the box proves its EC2 instance role to pair. Nothing the
box holds can approve its own pairing.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.harness import aws_iam_proof
from apps.harness.models import Runner, RunnerInvitation
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.aws_proof import ROLE
from tests.fixtures.runner_invitations import (
    INVITES,
    PAIR_HTU,
    _approved,
    _create,
    _pair,
    _pat_client,
    _person,
    _proof,
    make_org,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("fresh_cache")]


@pytest.fixture
def org():
    return make_org()


# ---- creating one ---------------------------------------------------------------------

def test_creating_an_invitation_returns_its_approve_link_and_tells_the_creator(
        org, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        res = _create(_pat_client(org["pat"]))
    assert res.status_code == 201, res.content
    body = res.json()
    inv = RunnerInvitation.objects.get(pk=body["id"])
    assert body["status"] == "pending" and body["runner_id"] is None
    assert body["name"] == "cloud-ec2-fork" and body["workspace"] == "connect"
    assert body["role_arn"] == ROLE
    assert body["approve_url"].endswith(f"/w/connect/runner-invitations/{inv.pk}")
    assert inv.created_by == org["user"] and inv.role_jkt == aws_iam_proof.role_jkt(ROLE)
    assert (inv.expires_at - inv.created_at) == timedelta(hours=1)
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["jj@dimagi.com"]
    assert body["approve_url"] in mail.outbox[0].body


def test_create_needs_agent_work_in_the_workspace(org):
    viewer = _person("vi")
    WorkspaceMembership.objects.create(workspace=org["ws"], user=viewer, role=WorkspaceMembership.VIEWER)
    raw, _ = PersonalToken.create_for_user(user=viewer, label="v")
    res = _create(_pat_client(raw))
    assert res.status_code == 403
    assert not RunnerInvitation.objects.exists()


def test_non_member_gets_404(org):
    stranger = _person("st")
    raw, _ = PersonalToken.create_for_user(user=stranger, label="s")
    assert _create(_pat_client(raw)).status_code == 404
    assert _create(_pat_client(raw), workspace="no-such-ws").status_code == 404
    assert not RunnerInvitation.objects.exists()


@pytest.mark.parametrize("name", ["Cloud EC2", "-bad", "x" * 64, ""])
def test_a_name_that_cannot_be_a_runner_name_is_refused(org, name):
    assert _create(_pat_client(org["pat"]), name=name).status_code == 422


def test_a_role_arn_that_is_not_a_role_is_refused(org):
    assert _create(_pat_client(org["pat"]), role_arn="arn:aws:iam::123456789012:user/jj").status_code == 422


def test_recreate_replaces_the_live_one(org):
    first = _create(_pat_client(org["pat"])).json()
    second = _create(_pat_client(org["pat"])).json()
    assert first["id"] != second["id"]
    assert list(RunnerInvitation.objects.values_list("pk", flat=True)) == [
        RunnerInvitation.objects.get(pk=second["id"]).pk]
    # A different name for the same role is a different invitation.
    _create(_pat_client(org["pat"]), name="cloud-ec2-other")
    assert RunnerInvitation.objects.count() == 2


def _teammate(org, username="mallory"):
    person = _person(username)
    WorkspaceMembership.objects.create(workspace=org["ws"], user=person, role=WorkspaceMembership.OWNER)
    raw, _ = PersonalToken.create_for_user(user=person, label=username)
    return person, raw


def test_someone_elses_live_invitation_cannot_be_replaced(org):
    mine = _create(_pat_client(org["pat"])).json()
    _, raw = _teammate(org)
    res = _create(_pat_client(raw))
    assert res.status_code == 409
    assert "mallory" not in res.content.decode() and "jj" not in res.content.decode()
    assert list(RunnerInvitation.objects.values_list("pk", flat=True)) == [
        RunnerInvitation.objects.get(pk=mine["id"]).pk]


def test_someone_elses_expired_invitation_is_replaced(org):
    mine = _create(_pat_client(org["pat"])).json()
    RunnerInvitation.objects.filter(pk=mine["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    teammate, raw = _teammate(org)
    res = _create(_pat_client(raw))
    assert res.status_code == 201
    assert RunnerInvitation.objects.get().created_by == teammate


def test_a_concurrent_create_is_a_409_not_a_500(org, monkeypatch):
    from django.db import IntegrityError

    def boom(**kw):
        raise IntegrityError("one_live_runner_invitation_per_role_and_name")

    monkeypatch.setattr(RunnerInvitation.objects, "create", boom)
    assert _create(_pat_client(org["pat"])).status_code == 409


def test_the_invited_activity_names_no_role(org):
    from apps.events.models import Activity

    _create(_pat_client(org["pat"]))
    assert "role_arn" not in Activity.objects.get(kind="runner.invited").meta


# ---- the pre-check up.sh makes before touching AWS -------------------------------------

def test_check_says_yes_and_creates_nothing(org):
    res = _pat_client(org["pat"]).get(f"{INVITES}check?workspace=connect")
    assert res.status_code == 200 and res.json() == {"workspace": "connect"}
    assert not RunnerInvitation.objects.exists()


def test_check_applies_the_create_gates(org):
    viewer = _person("vi")
    WorkspaceMembership.objects.create(workspace=org["ws"], user=viewer, role=WorkspaceMembership.VIEWER)
    raw, _ = PersonalToken.create_for_user(user=viewer, label="v")
    assert _pat_client(raw).get(f"{INVITES}check?workspace=connect").status_code == 403
    stranger = _person("st")
    raw, _ = PersonalToken.create_for_user(user=stranger, label="s")
    assert _pat_client(raw).get(f"{INVITES}check?workspace=connect").status_code == 404


# ---- reading one ----------------------------------------------------------------------

def test_only_the_creator_reads_it(org):
    inv = _create(_pat_client(org["pat"])).json()
    assert _pat_client(org["pat"]).get(f"{INVITES}{inv['id']}").json()["status"] == "pending"
    teammate = _person("tm")
    WorkspaceMembership.objects.create(workspace=org["ws"], user=teammate, role=WorkspaceMembership.OWNER)
    raw, _ = PersonalToken.create_for_user(user=teammate, label="t")
    assert _pat_client(raw).get(f"{INVITES}{inv['id']}").status_code == 404


# ---- approving it ---------------------------------------------------------------------

def test_approve_needs_step_up_and_a_token_cannot(org):
    inv = _create(_pat_client(org["pat"])).json()
    # A token — what up.sh and the box hold — is refused outright.
    res = _pat_client(org["pat"]).post(f"{INVITES}{inv['id']}/approve")
    assert res.status_code == 403
    assert RunnerInvitation.objects.get(pk=inv["id"]).approved_at is None
    # A browser without a fresh passkey touch is asked for one.
    grant_step_up(org["browser"], seconds=-1)
    res = org["browser"].post(f"{INVITES}{inv['id']}/approve")
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
    assert RunnerInvitation.objects.get(pk=inv["id"]).approved_at is None
    grant_step_up(org["browser"])
    res = org["browser"].post(f"{INVITES}{inv['id']}/approve")
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "approved"
    row = RunnerInvitation.objects.get(pk=inv["id"])
    assert row.approved_by == org["user"] and row.approved_at is not None


def test_only_the_creator_may_approve(org):
    inv = _create(_pat_client(org["pat"])).json()
    other_owner = _person("oo")
    WorkspaceMembership.objects.create(workspace=org["ws"], user=other_owner, role=WorkspaceMembership.OWNER)
    browser = Client()
    browser.force_login(other_owner)
    grant_step_up(browser)
    assert browser.post(f"{INVITES}{inv['id']}/approve").status_code == 404
    assert RunnerInvitation.objects.get(pk=inv["id"]).approved_at is None


def test_expired_cannot_be_approved(org):
    inv = _create(_pat_client(org["pat"])).json()
    RunnerInvitation.objects.filter(pk=inv["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    res = org["browser"].post(f"{INVITES}{inv['id']}/approve")
    assert res.status_code == 409
    assert RunnerInvitation.objects.get(pk=inv["id"]).approved_at is None
    assert _pat_client(org["pat"]).get(f"{INVITES}{inv['id']}").json()["status"] == "expired"


def test_creating_and_approving_are_recorded_as_activity(org):
    from apps.events.models import Activity

    inv = _create(_pat_client(org["pat"])).json()
    org["browser"].post(f"{INVITES}{inv['id']}/approve")
    kinds = list(Activity.objects.filter(workspace_id="connect").order_by("at").values_list("kind", flat=True))
    assert kinds == ["runner.invited", "runner.invitation.approved"]


# ---- the box pairs by its instance role ------------------------------------------------

_REFUSED = "no approved pairing invitation matches this box's instance role and name"
SESSIONS = "/api/runner-sessions/"


def _ecdh_jwk() -> dict:
    from canopy_sdk.keys import header_jwk
    from cryptography.hazmat.primitives.asymmetric import ec

    from apps.harness import device_keys

    return device_keys.normalize_public_jwk(header_jwk(ec.generate_private_key(ec.SECP256R1())))


ECDH = _ecdh_jwk()
OTHER_ECDH = _ecdh_jwk()


@pytest.mark.usefixtures("sts")
def test_approved_invitation_pairs_once(org):
    from apps.harness.models import RunnerDeviceKey, RunnerEnrollment

    inv = _approved(org)
    res = _pair()
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["status"] == "paired"
    runner = Runner.objects.get(pk=body["runner_id"])
    assert runner.owner == org["user"] and runner.workspace_id == "connect"
    assert runner.kind == Runner.CLOUD and runner.name == "cloud-ec2-fork"
    # The server decides what a box reports; pairing drops the reported keys.
    assert runner.capabilities == {"projects": ["canopy-web"], "sessions": True}
    assert list(RunnerEnrollment.objects.filter(runner=runner).values_list("workspace_id", flat=True)) == [
        "connect"]
    key = RunnerDeviceKey.objects.get(runner=runner)
    assert key.kind == RunnerDeviceKey.AWS_IAM and key.status == RunnerDeviceKey.ACTIVE
    assert key.jkt == aws_iam_proof.role_jkt(ROLE) and key.approved_by == org["user"]
    row = RunnerInvitation.objects.get(pk=inv["id"])
    assert row.status == "consumed" and row.runner_id == runner.pk
    got = _pat_client(org["pat"]).get(f"{INVITES}{inv['id']}").json()
    assert got["status"] == "consumed" and got["runner_id"] == str(runner.pk)
    # The same box asking again (a restart before its state file landed) gets
    # the same runner back; nothing new is made.
    again = _pair()
    assert again.status_code == 200 and again.json() == {"runner_id": str(runner.pk), "status": "reattached", "runners": {}, "device_keys": {}}
    assert Runner.objects.count() == 1 and RunnerDeviceKey.objects.count() == 1


@pytest.mark.usefixtures("sts")
def test_a_renamed_runner_is_still_reattached_but_another_name_is_refused(org):
    _approved(org)
    runner = Runner.objects.get(pk=_pair().json()["runner_id"])
    # A person renames it; the box still asks as the name it paired as.
    Runner.objects.filter(pk=runner.pk).update(name="cloud-ec2-live")
    again = _pair()
    assert again.status_code == 200 and again.json()["runner_id"] == str(runner.pk)
    assert Runner.objects.get(pk=runner.pk).name == "cloud-ec2-live"
    # The same role as some other box is still refused.
    assert _pair(name="cloud-ec2-other").status_code == 401
    assert Runner.objects.count() == 1


@pytest.mark.usefixtures("sts")
def test_pending_invitation_waits(org):
    _create(_pat_client(org["pat"]))
    res = _pair()
    assert res.status_code == 202 and res.json() == {"runner_id": None, "status": "waiting", "runners": {}, "device_keys": {}}
    assert not Runner.objects.exists()


def _no_invitation(org):
    return None


def _expired(org):
    inv = _approved(org)
    RunnerInvitation.objects.filter(pk=inv["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))


def _consumed_by_other_name(org):
    _approved(org, name="cloud-ec2-other")
    assert _pair(name="cloud-ec2-other").status_code == 201


def _for_another_name(org):
    _approved(org, name="cloud-ec2-other")


def _ecdh_mismatch(org):
    _approved(org, ecdh_public_jwk=ECDH)
    return {"ecdh_public_jwk": OTHER_ECDH}


def _bad_proof(org):
    _approved(org)
    return {"proof": "bm90LWEtcHJvb2Y="}


def _proof_for_another_url(org):
    _approved(org)
    return {"proof": _proof(f"http://testserver{SESSIONS}")}


def _creator_lost_agent_work(org):
    _approved(org)
    WorkspaceMembership.objects.filter(user=org["user"]).update(role=WorkspaceMembership.VIEWER)


@pytest.mark.usefixtures("sts")
@pytest.mark.parametrize("setup", [_no_invitation, _expired, _consumed_by_other_name, _for_another_name,
                                   _ecdh_mismatch, _bad_proof, _proof_for_another_url,
                                   _creator_lost_agent_work])
def test_refusals_are_uniform_and_create_nothing(org, setup):
    from apps.harness.models import RunnerDeviceKey

    extra = setup(org) or {}
    runners, keys = Runner.objects.count(), RunnerDeviceKey.objects.count()
    res = _pair(**extra)
    assert res.status_code == 401
    assert res.json()["title"] == "Pairing refused"
    assert res.json()["detail"] == "no approved pairing invitation matches this box's instance role and name"
    assert Runner.objects.count() == runners and RunnerDeviceKey.objects.count() == keys


@pytest.mark.usefixtures("sts")
def test_a_matching_ecdh_key_is_approved_with_the_invitation(org):
    from apps.harness.models import RunnerDeviceKey

    _approved(org, ecdh_public_jwk=ECDH)
    res = _pair(ecdh_public_jwk=ECDH, ecdh_store="aws-kms")
    assert res.status_code == 201, res.content
    key = RunnerDeviceKey.objects.get()
    assert key.ecdh_ready and key.ecdh_store == "aws-kms" and key.ecdh_approved_by == org["user"]


@pytest.mark.usefixtures("sts")
def test_the_invitations_ecdh_key_is_kept_when_a_fresh_box_reports_none(org):
    from apps.harness import device_keys
    from apps.harness.models import RunnerDeviceKey

    _approved(org, ecdh_public_jwk=ECDH)
    assert _pair().status_code == 201
    key = RunnerDeviceKey.objects.get()
    assert key.ecdh_ready and key.ecdh_jkt == device_keys.thumbprint(ECDH) and key.ecdh_store == "aws-kms"


@pytest.mark.usefixtures("sts")
def test_an_ecdh_key_the_invitation_did_not_name_waits_for_a_person(org):
    from apps.harness.models import RunnerDeviceKey

    _approved(org)
    assert _pair(ecdh_public_jwk=ECDH).status_code == 201
    key = RunnerDeviceKey.objects.get()
    assert key.status == RunnerDeviceKey.ACTIVE and key.ecdh_waiting


@pytest.mark.usefixtures("sts")
def test_replaced_instance_reattaches_without_an_invitation(org):
    _approved(org)
    rid = _pair().json()["runner_id"]
    # up.sh --allow-instance-change: a new instance, the same role, no state file.
    assert not RunnerInvitation.objects.filter(consumed_at__isnull=True).exists()
    res = _pair()
    assert res.status_code == 200 and res.json()["runner_id"] == rid
    # …but the role's runner has that name; the role cannot pair as another.
    assert _pair(name="cloud-ec2-impostor").status_code == 401
    assert Runner.objects.count() == 1


@pytest.mark.usefixtures("sts")
def test_pairing_and_reattaching_record_the_machine(org):
    """The verified role is the runner's machine (`Runner.machine_id`), the key
    the rules for several runners on one machine group them by."""
    canonical = aws_iam_proof.canonical_role_arn(ROLE)
    _approved(org)
    rid = _pair().json()["runner_id"]
    assert Runner.objects.get(pk=rid).machine_id == canonical
    # A runner paired before the field existed learns it when it reattaches.
    Runner.objects.filter(pk=rid).update(machine_id="")
    assert _pair().status_code == 200
    assert Runner.objects.get(pk=rid).machine_id == canonical


@pytest.mark.usefixtures("sts")
def test_retired_runner_role_needs_a_new_invitation_and_replaces_the_key_row(org):
    from apps.harness.models import RunnerDeviceKey

    _approved(org)
    old = Runner.objects.get(pk=_pair().json()["runner_id"])
    Runner.objects.filter(pk=old.pk).update(status=Runner.RETIRED)
    assert _pair().status_code == 401          # no reattach, and no invitation yet
    _approved(org)
    res = _pair()
    assert res.status_code == 201, res.content
    new = Runner.objects.get(pk=res.json()["runner_id"])
    assert new.pk != old.pk
    key = RunnerDeviceKey.objects.get(jkt=aws_iam_proof.role_jkt(ROLE))
    assert key.runner == new and key.status == RunnerDeviceKey.ACTIVE


@pytest.mark.usefixtures("sts")
def test_a_revoked_key_on_a_live_runner_is_not_laundered_by_reinviting(org):
    """Revoking was a deliberate act; its owner undoes it, not a new invitation."""
    from apps.harness.models import RunnerDeviceKey

    _approved(org)
    old = Runner.objects.get(pk=_pair().json()["runner_id"])
    RunnerDeviceKey.objects.filter(runner=old).update(status=RunnerDeviceKey.REVOKED)
    _approved(org)
    res = _pair()
    assert res.status_code == 401 and res.json()["detail"] == _REFUSED
    key = RunnerDeviceKey.objects.get(jkt=aws_iam_proof.role_jkt(ROLE))
    assert key.runner == old and key.status == RunnerDeviceKey.REVOKED
    assert Runner.objects.count() == 1


@pytest.mark.usefixtures("sts")
@pytest.mark.parametrize("old_status", ["online", "retired"])
@pytest.mark.parametrize("key_status", ["active", "revoked"])
def test_another_owners_key_for_the_role_is_never_erased(org, old_status, key_status):
    from apps.harness.models import RunnerDeviceKey

    # Mallory's runner already holds this role (live or retired, key live or revoked).
    mallory, _ = _teammate(org)
    theirs = Runner.objects.create(name="cloud-ec2-fork", kind=Runner.CLOUD, owner=mallory,
                                   workspace=org["ws"], status=old_status, capabilities={})
    RunnerDeviceKey.objects.create(runner=theirs, kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE,
                                   jkt=aws_iam_proof.role_jkt(ROLE), status=key_status)
    _approved(org)
    res = _pair()
    if old_status == "online" and key_status == "active":
        # That is Mallory's own box re-attaching to Mallory's runner; nothing of jj's.
        assert res.json() == {"runner_id": str(theirs.pk), "status": "reattached", "runners": {}, "device_keys": {}}
        return
    assert res.status_code == 401 and res.json()["detail"] == _REFUSED
    key = RunnerDeviceKey.objects.get(jkt=aws_iam_proof.role_jkt(ROLE))
    assert key.runner == theirs and key.status == key_status
    assert Runner.objects.count() == 1


@pytest.mark.usefixtures("sts")
def test_a_concurrent_pairing_is_a_uniform_401_not_a_500(org, monkeypatch):
    from django.db import IntegrityError

    from apps.harness.models import RunnerDeviceKey

    _approved(org)

    def boom(**kw):
        raise IntegrityError("duplicate key value violates unique constraint (jkt)")

    monkeypatch.setattr(RunnerDeviceKey.objects, "create", boom)
    res = _pair()
    assert res.status_code == 401 and res.json()["detail"] == _REFUSED
    assert not Runner.objects.exists()
    assert RunnerInvitation.objects.get().consumed_at is None


@pytest.mark.usefixtures("sts")
def test_hijacking_someone_elses_invitation_fails_end_to_end(org):
    """Alice (jj) invites and approves her box. Mallory, a co-owner of the
    workspace, tries to replace the invitation with her own for the same role and
    name. She is refused; Alice's invitation survives and her box pairs to her."""
    alice_inv = _approved(org)
    mallory, raw = _teammate(org)
    assert _create(_pat_client(raw)).status_code == 409
    browser = Client()
    browser.force_login(mallory)
    grant_step_up(browser)
    assert browser.post(f"{INVITES}{alice_inv['id']}/approve").status_code == 404
    res = _pair()
    assert res.status_code == 201, res.content
    runner = Runner.objects.get(pk=res.json()["runner_id"])
    assert runner.owner == org["user"] and runner.owner != mallory
    assert RunnerInvitation.objects.get(pk=alice_inv["id"]).runner_id == runner.pk


@pytest.mark.usefixtures("sts")
def test_the_new_runner_can_get_a_runner_session(org):
    _approved(org)
    rid = _pair().json()["runner_id"]
    res = Client().post(SESSIONS, data={"runner": rid}, content_type="application/json",
                        HTTP_X_CANOPY_AWS_PROOF=_proof(f"http://testserver{SESSIONS}"))
    assert res.status_code == 201, res.content
    assert res.json()["runner"] == rid


@pytest.mark.usefixtures("sts")
def test_no_token_can_approve_its_own_pairing_end_to_end(org):
    """All a box (or up.sh) holds — a token and its role — gets it no further
    than `waiting`."""
    inv = _create(_pat_client(org["pat"])).json()
    assert _pat_client(org["pat"]).post(f"{INVITES}{inv['id']}/approve").status_code == 403
    assert _pair().status_code == 202
    assert not Runner.objects.exists()


@pytest.mark.usefixtures("sts")
def test_a_replayed_proof_is_refused(org):
    _create(_pat_client(org["pat"]))
    proof = _proof(PAIR_HTU)
    assert _pair(proof=proof).status_code == 202
    assert _pair(proof=proof).status_code == 401


def test_a_runner_token_cannot_reach_the_invitation_routes():
    from apps.harness import runner_auth

    rid = "6f1c0d3e-0000-4000-8000-000000000000"
    for method, path in (("POST", INVITES), ("GET", f"{INVITES}x"), ("POST", f"{INVITES}x/approve")):
        assert not runner_auth.reaches(method, path, rid)
