"""Retention as a workspace setting: any member reads the policy, an admin
changes it, every change lands in the event log, and the preview counts
without deleting."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.events.models import Event
from apps.harness.models import Turn
from apps.retention.models import RetentionRule
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BASE = "/api/workspaces/connect/retention"


@pytest.fixture
def world():
    owner = User.objects.create_user("own", "own@dimagi.com", "pw")
    org = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=org)
    people = {}
    for role in ("viewer", "editor", "admin", "owner"):
        u = User.objects.create_user(role, f"{role}@dimagi.com", "pw")
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=role)
        people[role] = u
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    stranger_agent = Agent.objects.create(slug="eve", name="Eve", workspace=other, owner=owner)
    return type("W", (), dict(ws=ws, org=org, hal=hal, people=people, stranger_agent=stranger_agent))


def as_(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def test_any_member_reads_the_policy_including_inherited(world):
    RetentionRule.objects.create(workspace=world.org, principal="contact", keep_days=7)
    RetentionRule.objects.create(kind="turn", keep_days=90)
    RetentionRule.objects.create(workspace=world.ws, kind="chat", keep_days=30)
    body = as_(world.people["viewer"]).get(BASE).json()
    assert body["can_manage"] is False and body["enforced"] is False
    assert [r["summary"] for r in body["rules"]] == ["Chats: 30 days"]
    assert [r["workspace"] for r in body["inherited"]] == ["dimagi", ""]
    assert "shared" not in {c["value"] for c in body["choices"]["kinds"]}


def test_only_admins_and_owners_change_rules(world):
    rule = {"kind": "chat", "principal": "contact", "agent": "hal", "keep_days": 7}
    for role in ("viewer", "editor"):
        assert as_(world.people[role]).post(f"{BASE}/rules", rule, content_type="application/json").status_code == 403
        assert as_(world.people[role]).get(f"{BASE}/preview").status_code == 403
    r = as_(world.people["admin"]).post(f"{BASE}/rules", rule, content_type="application/json")
    assert r.status_code == 201, r.content
    assert r.json()["summary"] == "Chats started by contacts with hal: 7 days"
    rid = r.json()["id"]

    r = as_(world.people["owner"]).put(f"{BASE}/rules/{rid}", {**rule, "keep_days": None},
                                       content_type="application/json")
    assert r.status_code == 200 and r.json()["keep_days"] is None
    assert as_(world.people["admin"]).delete(f"{BASE}/rules/{rid}").status_code == 204
    assert not RetentionRule.objects.exists()


def test_every_change_is_in_the_event_log(world):
    c = as_(world.people["admin"])
    rid = c.post(f"{BASE}/rules", {"kind": "turn", "keep_days": 30},
                 content_type="application/json").json()["id"]
    c.put(f"{BASE}/rules/{rid}", {"kind": "turn", "keep_days": 14}, content_type="application/json")
    c.delete(f"{BASE}/rules/{rid}")
    log = list(Event.objects.filter(workspace=world.ws, source="retention")
               .order_by("first_seen_at").values_list("kind", "summary"))
    assert [k for k, _ in log] == ["retention.rule_created", "retention.rule_updated",
                                   "retention.rule_deleted"]
    assert "admin@dimagi.com changed a retention rule: Turns: 14 days (was: Turns: 30 days)" in log[1][1]


def test_validation(world):
    c = as_(world.people["admin"])
    post = lambda body: c.post(f"{BASE}/rules", body, content_type="application/json")  # noqa: E731
    assert post({"kind": "chat", "keep_days": 30}).status_code == 201
    assert post({"kind": "chat", "keep_days": 7}).status_code == 422  # same filters
    assert post({"agent": "eve", "keep_days": 7}).status_code == 422  # another workspace's agent
    assert post({"kind": "shared", "keep_days": 7}).status_code == 422
    assert post({"keep_days": 0}).status_code == 422


def test_a_rule_elsewhere_cannot_be_touched_from_here(world):
    foreign = RetentionRule.objects.create(workspace=world.org, keep_days=7)
    c = as_(world.people["admin"])
    assert c.delete(f"{BASE}/rules/{foreign.pk}").status_code == 404
    assert c.put(f"{BASE}/rules/{foreign.pk}", {"keep_days": 1},
                 content_type="application/json").status_code == 404


def test_non_members_get_404(world):
    stranger = User.objects.create_user("x", "x@dimagi.com", "pw")
    assert as_(stranger).get(BASE).status_code == 404


def test_preview_counts_and_deletes_nothing(world):
    RetentionRule.objects.create(workspace=world.ws, kind="turn", keep_days=30)
    turn = Turn.objects.create(agent=world.hal, origin="email", status=Turn.DONE, prompt="body",
                               idempotency_key=uuid.uuid4().hex)
    old = timezone.now() - dt.timedelta(days=40)
    Turn.objects.filter(pk=turn.pk).update(created_at=old, finished_at=old)
    body = as_(world.people["admin"]).get(f"{BASE}/preview").json()
    assert body["totals"] == {"turns_scrubbed": 1}
    assert body["by_rule"][0]["summary"] == "Turns: 30 days"
    turn.refresh_from_db()
    assert turn.prompt == "body"
