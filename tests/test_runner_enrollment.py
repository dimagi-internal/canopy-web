"""A runner serves only the workspaces it is ENROLLED in (2026-10-11).

Before this, a box served every workspace its owner could work in (editor and
above). The day Jonathan became owner of `marshellis` — his family's own root
workspace — every Dimagi box he owns (his laptop, cloud-ec2-1) would have
started claiming its repo turns and unbound chats, and a family box would have
claimed Dimagi's. The rule now: enrolled AND the owner still editor+ there.
Enrollment never grants more than membership; it only narrows it.
"""
from __future__ import annotations

import importlib
import inspect

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import Session
from apps.harness import services
from apps.harness.models import Runner, RunnerAdmin, RunnerEnrollment, Turn
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

OWNER, ADMIN, EDITOR, VIEWER = (WorkspaceMembership.OWNER, WorkspaceMembership.ADMIN,
                                WorkspaceMembership.EDITOR, WorkspaceMembership.VIEWER)


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug, creator, parent=None):
    return Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=creator,
                                    parent=parent)


def _member(ws, user, role):
    return WorkspaceMembership.objects.create(workspace=ws, user=user, role=role)


def _box(owner, ws, **kw):
    defaults = dict(name="jj-mbp", kind=Runner.CLOUD, host="jj-mac", owner=owner, workspace=ws,
                    status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                    capabilities={"projects": ["canopy-web"], "sessions": True})
    defaults.update(kw)
    return Runner.objects.create(**defaults)


def _repo_turn(ws, key):
    return Turn.objects.create(project="canopy-web", workspace=ws, origin=Turn.ORIGIN_API,
                               idempotency_key=key, prompt="fix it")


def _chat_turn(ws, user, key):
    session = Session.objects.create(workspace=ws, created_by=user)
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=key, prompt="hi")


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _backfill(**kw):
    return services.backfill_runner_enrollments(
        runner_model=Runner, enrollment_model=RunnerEnrollment, membership_model=WorkspaceMembership,
        workspace_model=Workspace, **kw)


@pytest.fixture()
def family():
    """Jonathan owns `dimagi` and the separate root `marshellis`."""
    jj = _user("jj")
    dimagi, marshellis = _ws("dimagi", jj), _ws("marshellis", jj)
    _member(dimagi, jj, OWNER)
    _member(marshellis, jj, OWNER)
    return {"jj": jj, "dimagi": dimagi, "marshellis": marshellis}


# --- the migration's backfill ------------------------------------------------
def test_backfill_preserves_what_every_box_served_except_marshellis():
    jj, other = _user("jj"), _user("other")
    dimagi = _ws("dimagi", jj)
    _ws("connect", jj, parent=dimagi)  # reached only as dimagi's inherited owner
    labs = _ws("labs", jj)
    readonly = _ws("readonly", jj)
    marshellis = _ws("marshellis", jj)
    kids = _ws("marshellis-kids", jj, parent=marshellis)
    _member(dimagi, jj, OWNER)
    _member(labs, jj, EDITOR)
    _member(readonly, jj, VIEWER)
    _member(marshellis, jj, OWNER)
    _member(labs, other, ADMIN)

    laptop = _box(jj, dimagi, name="laptop")
    cloud = _box(jj, labs, name="cloud-ec2-1")
    theirs = _box(other, labs, name="theirs")
    orphan = _box(None, dimagi, name="orphan")
    # What the old rule said each box served, minus the family tree.
    before = {r.pk: perms.slugs_with(r.owner, perms.AGENT_WORK) - {"marshellis", "marshellis-kids"}
              for r in (laptop, cloud) if r.owner_id}
    RunnerEnrollment.objects.all().delete()  # as if these boxes predated enrollment

    _backfill(exclude=("marshellis",))

    expect = {"dimagi", "connect", "labs"}
    assert services.enrolled_slugs(laptop) == expect
    assert services.enrolled_slugs(cloud) == expect
    assert services.enrolled_slugs(theirs) == {"labs"}
    assert services.enrolled_slugs(orphan) == set()
    for r in (laptop, cloud):
        assert services.runner_tenant_slugs(r) == before[r.pk] == expect
    assert not RunnerEnrollment.objects.filter(
        workspace__in=[marshellis, kids, readonly]).exists()


def test_backfill_is_idempotent_and_safe_without_marshellis():
    jj = _user("jj")
    dimagi = _ws("dimagi", jj)
    _member(dimagi, jj, OWNER)
    box = _box(jj, dimagi)  # already enrolled at pairing
    assert _backfill(exclude=("marshellis",)) == 0
    assert _backfill(exclude=("marshellis",)) == 0
    assert list(RunnerEnrollment.objects.filter(runner=box)
                .values_list("workspace_id", flat=True)) == ["dimagi"]


def test_the_migration_backfill_is_wired_to_exclude_marshellis():
    mig = importlib.import_module("apps.harness.migrations.0071_runner_enrollment")
    assert 'exclude=("marshellis",)' in inspect.getsource(mig.backfill)


# --- claiming -----------------------------------------------------------------
def test_a_new_box_serves_only_its_pairing_workspace(family):
    box = _box(family["jj"], family["dimagi"])
    assert services.enrolled_slugs(box) == {"dimagi"}
    assert services.runner_tenant_slugs(box) == {"dimagi"}


def test_an_unenrolled_workspace_is_not_claimable_even_for_its_owner(family):
    jj, dimagi, marshellis = family["jj"], family["dimagi"], family["marshellis"]
    box = _box(jj, dimagi)
    _repo_turn(marshellis, "family-repo")
    _chat_turn(marshellis, jj, "family-chat")

    assert services.claim_next_turn(box) is None

    mine = _repo_turn(dimagi, "dimagi-repo")
    assert services.claim_next_turn(box).pk == mine.pk


def test_a_family_box_does_not_claim_dimagi_work(family):
    jj, dimagi, marshellis = family["jj"], family["dimagi"], family["marshellis"]
    family_box = _box(jj, marshellis, name="family-mini")
    _repo_turn(dimagi, "dimagi-repo")
    assert services.claim_next_turn(family_box) is None
    t = _chat_turn(marshellis, jj, "family-chat")
    assert services.claim_next_turn(family_box).pk == t.pk


def test_enrolled_but_owner_demoted_below_editor_is_not_claimable(family):
    jj = family["jj"]
    boss = _user("boss")
    team = _ws("team", boss)
    _member(team, boss, OWNER)
    m = _member(team, jj, EDITOR)
    box = _box(jj, family["dimagi"])
    services.enroll_runner(box, "team")
    _repo_turn(team, "team-repo")

    m.role = VIEWER
    m.save()
    assert services.claim_next_turn(box) is None
    assert "team" in services.enrolled_slugs(box)  # kept, but serving nothing

    m.role = EDITOR
    m.save()
    assert services.claim_next_turn(box) is not None


def test_joining_a_workspace_does_not_enroll_existing_boxes(family):
    jj = family["jj"]
    box = _box(jj, family["dimagi"])
    boss = _user("boss")
    later = _ws("later", boss)
    wsvc.ensure_member(later, jj, EDITOR)
    _repo_turn(later, "later-repo")
    assert "later" not in services.enrolled_slugs(box)
    assert services.claim_next_turn(box) is None


def test_leaving_a_workspace_drops_the_enrollment(family):
    jj = family["jj"]
    boss = _user("boss")
    team = _ws("team", boss)
    _member(team, boss, OWNER)
    _member(team, jj, EDITOR)
    box = _box(jj, family["dimagi"])
    services.enroll_runner(box, "team")

    wsvc.remove_member(workspace=team, user_id=jj.pk, by=boss)

    assert not RunnerEnrollment.objects.filter(runner=box, workspace=team).exists()


# --- the API: list / add / remove -----------------------------------------------
def test_owner_lists_adds_and_removes_enrollments(family):
    jj, box = family["jj"], _box(family["jj"], family["dimagi"])
    c = _client(jj)
    url = f"/api/harness/runners/{box.pk}/workspaces"

    assert [(r["workspace"], r["serving"]) for r in c.get(url).json()] == [("dimagi", True)]
    resp = c.post(url, {"workspace": "marshellis"}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["workspace"] == "marshellis" and resp.json()["serving"] is True
    assert {r["workspace"] for r in c.get(url).json()} == {"dimagi", "marshellis"}

    assert c.delete(f"{url}/marshellis").status_code == 204
    assert c.delete(f"{url}/marshellis").status_code == 404
    assert services.enrolled_slugs(box) == {"dimagi"}


def test_adding_requires_the_owner_be_an_editor_there(family):
    jj = family["jj"]
    boss = _user("boss")
    readonly = _ws("readonly", boss)
    _member(readonly, jj, VIEWER)
    nowhere = _ws("nowhere", boss)
    box = _box(jj, family["dimagi"])
    url = f"/api/harness/runners/{box.pk}/workspaces"
    c = _client(jj)

    assert c.post(url, {"workspace": "readonly"}, content_type="application/json").status_code == 422
    assert c.post(url, {"workspace": nowhere.slug},
                  content_type="application/json").status_code == 404
    assert c.post(url, {"workspace": "no-such"}, content_type="application/json").status_code == 404
    assert services.enrolled_slugs(box) == {"dimagi"}


def test_a_runner_admin_may_manage_enrollments_where_they_are_a_member(family):
    jj, dimagi = family["jj"], family["dimagi"]
    ada = _user("ada")
    _member(dimagi, ada, EDITOR)
    team = _ws("team", jj)
    _member(team, jj, OWNER)
    _member(team, ada, EDITOR)
    box = _box(jj, dimagi)
    services.enroll_runner(box, "marshellis")
    RunnerAdmin.objects.create(runner=box, user=ada, granted_by=jj)
    c = _client(ada)
    url = f"/api/harness/runners/{box.pk}/workspaces"

    # Sees only the workspaces it is in — not the owner's family workspace.
    assert {r["workspace"] for r in c.get(url).json()} == {"dimagi"}
    assert c.post(url, {"workspace": "team"}, content_type="application/json").status_code == 200
    # May not name, nor remove, a workspace it is not a member of.
    assert c.post(url, {"workspace": "marshellis"},
                  content_type="application/json").status_code == 404
    assert c.delete(f"{url}/marshellis").status_code == 404
    assert c.delete(f"{url}/team").status_code == 204
    assert services.enrolled_slugs(box) == {"dimagi", "marshellis"}


def test_a_member_who_is_not_an_administrator_cannot_touch_enrollments(family):
    jj, dimagi = family["jj"], family["dimagi"]
    bystander = _user("bystander")
    _member(dimagi, bystander, EDITOR)
    box = _box(jj, dimagi)
    c = _client(bystander)
    url = f"/api/harness/runners/{box.pk}/workspaces"

    assert c.get(url).status_code == 404
    assert c.post(url, {"workspace": "dimagi"}, content_type="application/json").status_code == 404
    assert c.delete(f"{url}/dimagi").status_code == 404
    assert services.enrolled_slugs(box) == {"dimagi"}


def test_a_workspace_runner_order_refuses_an_unenrolled_box(family):
    jj = family["jj"]
    box = _box(jj, family["dimagi"])
    resp = _client(jj).put("/api/workspaces/marshellis/runner-order",
                           {"runners": [{"runner_id": str(box.pk)}]},
                           content_type="application/json")
    assert resp.status_code == 422 and "enrolled" in resp.json()["detail"]


# --- enrollment covers the subtree ------------------------------------------------
def test_a_division_created_after_enrolling_is_served(family):
    jj, dimagi = family["jj"], family["dimagi"]
    box = _box(jj, dimagi)
    later = _ws("dimagi-later", jj, parent=dimagi)  # jj owns it by inheritance
    t = _repo_turn(later, "later-repo")

    assert "dimagi-later" in services.runner_tenant_slugs(box)
    assert services.enrolled_slugs(box) == {"dimagi"}  # no row was added
    assert services.claim_next_turn(box).pk == t.pk


def test_a_sibling_root_is_never_covered(family):
    jj, dimagi, marshellis = family["jj"], family["dimagi"], family["marshellis"]
    box = _box(jj, dimagi)
    _ws("marshellis-kids", jj, parent=marshellis)
    _repo_turn(marshellis, "family-repo")
    _chat_turn(marshellis, jj, "family-chat")

    assert services.runner_tenant_slugs(box) == {"dimagi"}
    assert services.claim_next_turn(box) is None


def test_coverage_still_needs_the_owner_to_be_an_editor_in_the_division():
    boss, jj = _user("boss"), _user("jj")
    org = _ws("org", boss)
    _member(org, boss, OWNER)
    _member(org, jj, EDITOR)  # an editor's role does not flow down the tree
    box = _box(jj, org)
    division = _ws("division", boss, parent=org)
    _repo_turn(division, "division-repo")
    assert services.claim_next_turn(box) is None

    _member(division, jj, EDITOR)
    assert services.claim_next_turn(box) is not None


def test_removing_a_divisions_row_does_not_stop_coverage_from_above(family):
    jj, dimagi = family["jj"], family["dimagi"]
    box = _box(jj, dimagi)
    child = _ws("dimagi-child", jj, parent=dimagi)
    services.enroll_runner(box, child.slug)
    services.unenroll_runner(box, child.slug)
    assert "dimagi-child" in services.runner_tenant_slugs(box)


def test_the_listing_says_what_each_enrollment_covers(family):
    jj, dimagi = family["jj"], family["dimagi"]
    box = _box(jj, dimagi)
    _ws("dimagi-a", jj, parent=dimagi)
    _ws("dimagi-b", jj, parent=dimagi)
    rows = _client(jj).get(f"/api/harness/runners/{box.pk}/workspaces").json()
    assert [(r["workspace"], r["covers"]) for r in rows] == [("dimagi", ["dimagi-a", "dimagi-b"])]
