"""A runner's egress lock, set in canopy-web (`PUT /api/harness/runners/{id}/egress`)
and read by root on the box by its instance role (`POST …/box-egress`).

The rules that matter: an administrator may lock a box and name hosts from any
client (UI or MCP); a sensitive-data box is always locked, and adding a host to one is a
person's act in a browser behind a passkey; every change is an audited setting; and
the box reads only its own runners, by a proof its runners cannot make.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.events.models import SettingChange
from apps.harness import aws_iam_proof
from apps.harness.models import Runner, RunnerAdmin, RunnerFlag
from apps.passkeys import stepup
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
ROLE = "arn:aws:iam::858923557655:role/canopy-cloud-runner-zdr-1-RunnerRole"


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    admin = User.objects.create_user("adm", "adm@dimagi.com", "pw")
    stranger = User.objects.create_user("str", "str@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    for u in (admin, stranger):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    machine = aws_iam_proof.canonical_role_arn(ROLE)
    box = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=ws,
                                machine_id=machine)
    RunnerAdmin.objects.create(runner=box, user=admin, granted_by=owner)
    return {"owner": owner, "admin": admin, "stranger": stranger, "ws": ws, "box": box,
            "machine": machine}


def _browser(user, stepped_up=True) -> Client:
    c = Client()
    c.force_login(user)
    if stepped_up:
        s = c.session
        s[stepup.SESSION_KEY] = {"user": user.pk, "until": time.time() + 60}
        s.save()
    return c


def _token(user) -> Client:
    raw, _ = PersonalToken.create_for_user(user=user, label="mcp")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _put(c, runner, **body):
    return c.put(f"/api/harness/runners/{runner.pk}/egress", data=body, content_type="application/json")


def _sensitive(runner):
    RunnerFlag.objects.create(runner=runner, flag="sensitive_data")


def test_an_admin_locks_a_box_from_a_token_and_hosts_are_normalised(w):
    r = _put(_token(w["admin"]), w["box"], locked=True, allow=["Slack.com", ".googleapis.com", "slack.com."])
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["egress_locked"] is True and body["egress_forced"] is False
    assert body["egress_allow"] == [".googleapis.com", "slack.com"]
    w["box"].refresh_from_db()
    assert w["box"].egress_locked and w["box"].egress_allow == [".googleapis.com", "slack.com"]


def test_every_change_is_an_audited_setting(w):
    _put(_browser(w["owner"]), w["box"], locked=True, allow=["slack.com"])
    [row] = SettingChange.objects.filter(operation="set_runner_egress")
    [change] = [c for c in row.changes if c.get("scope") == "runner-egress:cloud-1"]
    assert change["before"] == {"locked": False, "allow": []}
    assert change["after"] == {"locked": True, "allow": ["slack.com"]}


@pytest.mark.parametrize("bad", ["evil.com/x", "*.example.com", "1.2.3.4:443", "localhost", "a b"])
def test_a_host_that_is_not_a_hostname_is_refused(w, bad):
    r = _put(_browser(w["owner"]), w["box"], locked=True, allow=[bad])
    assert r.status_code == 422, r.content


def test_someone_who_does_not_administer_the_box_cannot(w):
    assert _put(_browser(w["stranger"]), w["box"], locked=True).status_code == 404


def test_a_sensitive_box_cannot_be_unlocked(w):
    _sensitive(w["box"])
    r = _put(_browser(w["owner"]), w["box"], locked=False)
    assert r.status_code == 422 and "always locked" in r.content.decode()


def test_adding_a_host_to_a_sensitive_box_is_a_person_behind_a_passkey(w):
    _sensitive(w["box"])
    assert _put(_token(w["owner"]), w["box"], locked=True, allow=["slack.com"]).status_code == 403
    no_step = _put(_browser(w["owner"], stepped_up=False), w["box"], locked=True, allow=["slack.com"])
    assert no_step.status_code == 403 and "step_up" in no_step.content.decode()
    ok = _put(_browser(w["owner"]), w["box"], locked=True, allow=["slack.com"])
    assert ok.status_code == 200 and ok.json()["egress_forced"] is True


def test_tightening_a_sensitive_box_needs_no_passkey(w):
    _sensitive(w["box"])
    Runner.objects.filter(pk=w["box"].pk).update(egress_locked=True, egress_allow=["slack.com"])
    w["box"].refresh_from_db()
    r = _put(_token(w["owner"]), w["box"], locked=True, allow=[])
    assert r.status_code == 200, r.content
    assert r.json()["egress_allow"] == []


def test_a_box_reporting_sensitive_mode_counts_as_sensitive(w):
    Runner.objects.filter(pk=w["box"].pk).update(capabilities={"data_mode": "sensitive_data"})
    assert _put(_browser(w["owner"]), w["box"], locked=False).status_code == 422


# ---- the box reads it ---------------------------------------------------------------

def _box_read(monkeypatch, role=ROLE, fail=False):
    def verify(proof, request):
        if fail:
            raise aws_iam_proof.AwsProofError("bad signature")
        return role
    monkeypatch.setattr(aws_iam_proof, "verify_caller", verify)
    return Client().post("/api/harness/runners/box-egress", data={"proof": "p"},
                         content_type="application/json")


def test_the_box_reads_its_own_runners_by_its_role(w, monkeypatch):
    Runner.objects.filter(pk=w["box"].pk).update(egress_locked=True, egress_allow=["slack.com"])
    Runner.objects.create(name="cloud-1-eva", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                          machine_id=w["machine"])
    Runner.objects.create(name="other-box", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                          machine_id="arn:aws:iam::858923557655:role/other")
    Runner.objects.create(name="cloud-1-old", kind=Runner.CLOUD, owner=w["owner"], workspace=w["ws"],
                          machine_id=w["machine"], status=Runner.RETIRED)
    r = _box_read(monkeypatch)
    assert r.status_code == 200, r.content
    rows = {x["name"]: x for x in r.json()["runners"]}
    assert set(rows) == {"cloud-1", "cloud-1-eva"}
    assert rows["cloud-1"]["locked"] is True and rows["cloud-1"]["allow"] == ["slack.com"]
    assert rows["cloud-1-eva"]["locked"] is False


def test_the_box_is_told_a_sensitive_runner_is_locked_whatever_is_set(w, monkeypatch):
    _sensitive(w["box"])
    [row] = _box_read(monkeypatch).json()["runners"]
    assert row["locked"] is True


def test_a_bad_proof_is_one_uniform_401(w, monkeypatch):
    r = _box_read(monkeypatch, fail=True)
    assert r.status_code == 401
    assert "cloud-1" not in r.content.decode()


def test_a_role_with_no_runners_reads_nothing(w, monkeypatch):
    r = _box_read(monkeypatch, role="arn:aws:iam::858923557655:role/nobody")
    assert r.status_code == 200 and r.json() == {"runners": []}
