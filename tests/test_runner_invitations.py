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
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.harness import aws_iam_proof
from apps.harness.models import Runner, RunnerInvitation
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

INVITES = "/api/harness/runner-invitations/"
ROLE = "arn:aws:iam::123456789012:role/canopy-runner-fork-RunnerRole-ABC"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _person(username: str):
    return get_user_model().objects.create_user(username=username, email=f"{username}@dimagi.com")


@pytest.fixture
def org():
    jj = _person("jj")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    browser = Client()
    browser.force_login(jj)
    grant_step_up(browser)
    raw, _ = PersonalToken.create_for_user(user=jj, label="workbench")
    return {"user": jj, "ws": ws, "browser": browser, "pat": raw}


def _pat_client(raw: str) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _create(client, *, name="cloud-ec2-fork", workspace="connect", role_arn=ROLE, **extra):
    return client.post(INVITES, data={"name": name, "workspace": workspace, "role_arn": role_arn, **extra},
                       content_type="application/json")


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
    assert body["role_arn"] == "arn:aws:iam::123456789012:role/canopy-runner-fork-RunnerRole-ABC"
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
