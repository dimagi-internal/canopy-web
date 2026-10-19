"""A runner serves only the workspaces it is ENROLLED in (2026-10-11).

Before this, a box served every workspace its owner could work in (editor and
above). The day Jonathan became owner of `marshellis` — his family's own root
workspace — every Dimagi box he owns (his laptop, cloud-ec2-1) would have
started claiming its repo turns and unbound chats, and a family box would have
claimed Dimagi's. The rule now: enrolled AND the owner still editor+ there.
Enrollment never grants more than membership; it only narrows it.
"""
from __future__ import annotations


import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import Session
from apps.harness import services
from apps.harness.models import Runner, RunnerAdmin, RunnerEnrollment, Turn
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
    # A laptop by default: only a laptop may span workspace trees, and several
    # tests below enroll one box in two of them.
    defaults = dict(name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=owner, workspace=ws,
                    status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                    capabilities={"projects": ["canopy-web"], "sessions": True})
    defaults.update(kw)
    return Runner.objects.create(**defaults)


def _repo_turn(ws, key):
    return Turn.objects.create(project="canopy-web", workspace=ws, origin=Turn.ORIGIN_API,
                               idempotency_key=key, prompt="fix it")


def _chat_turn(ws, user, key):
    session = Session.objects.create(workspace=ws, created_by=user)
    # Sent by `user`: an unbound chat runs on a box its sender administers
    # (or one its workspace's runner order shares).
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=key, prompt="hi", enqueued_by=user)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def family():
    """Jonathan owns `dimagi` and the separate root `marshellis`."""
    jj = _user("jj")
    dimagi, marshellis = _ws("dimagi", jj), _ws("marshellis", jj)
    _member(dimagi, jj, OWNER)
    _member(marshellis, jj, OWNER)
    return {"jj": jj, "dimagi": dimagi, "marshellis": marshellis}


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


# --- a cloud runner serves one tree; a laptop may span them ------------------------
def test_cloud_runner_cannot_span_roots(family):
    jj = family["jj"]
    box = _box(jj, family["dimagi"], kind=Runner.CLOUD)
    with pytest.raises(services.EnrollmentRefused, match="one workspace tree"):
        services.enroll_runner(box, "marshellis")
    resp = _client(jj).post(f"/api/harness/runners/{box.pk}/workspaces",
                            {"workspace": "marshellis"}, content_type="application/json")
    assert resp.status_code == 422 and "one workspace tree" in resp.json()["detail"]
    assert services.enrolled_slugs(box) == {"dimagi"}


def test_cloud_runner_may_enroll_a_division_of_its_root(family):
    jj, dimagi = family["jj"], family["dimagi"]
    box = _box(jj, dimagi, kind=Runner.CLOUD)
    connect = _ws("connect", jj, parent=dimagi)
    deep = _ws("connect-labs", jj, parent=connect)
    services.enroll_runner(box, connect.slug)
    services.enroll_runner(box, deep.slug)
    assert services.enrolled_slugs(box) == {"dimagi", "connect", "connect-labs"}
    # Enrolled in a division only, the box's tree is that division's root.
    other = _box(jj, connect, name="cloud-connect", kind=Runner.CLOUD)
    with pytest.raises(services.EnrollmentRefused):
        services.enroll_runner(other, "marshellis")
    services.enroll_runner(other, "dimagi")


def test_laptop_may_span_roots(family):
    jj = family["jj"]
    laptop = _box(jj, family["dimagi"], kind=Runner.EMDASH)
    services.enroll_runner(laptop, "marshellis")
    assert services.enrolled_slugs(laptop) == {"dimagi", "marshellis"}
    assert services.runner_tenant_slugs(laptop) == {"dimagi", "marshellis"}


def test_runner_out_reports_roots(family):
    jj, dimagi = family["jj"], family["dimagi"]
    laptop = _box(jj, dimagi, kind=Runner.EMDASH)
    services.enroll_runner(laptop, "marshellis")
    cloud = _box(jj, dimagi, name="cloud-ec2-2", kind=Runner.CLOUD)
    _ws("connect", jj, parent=dimagi)
    services.enroll_runner(cloud, "connect")

    rows = {r["name"]: r for r in _client(jj).get("/api/harness/runners/").json()}
    assert rows["jj-mbp"]["roots"] == ["dimagi", "marshellis"]
    assert rows["jj-mbp"]["spans_trees"] is True
    assert rows["cloud-ec2-2"]["roots"] == ["dimagi"]
    assert rows["cloud-ec2-2"]["spans_trees"] is False

    # A teammate in dimagi learns the laptop spans trees, but not which other one.
    ada = _user("ada")
    _member(dimagi, ada, EDITOR)
    seen = {r["name"]: r for r in _client(ada).get("/api/harness/runners/").json()}
    assert seen["jj-mbp"]["roots"] == ["dimagi"]
    assert seen["jj-mbp"]["spans_trees"] is True


def test_re_enrolling_an_existing_enrollment_of_a_spanning_cloud_box_is_a_no_op(family):
    """A cloud box enrolled in two trees before the rule may re-send what it has."""
    from apps.harness.models import RunnerEnrollment

    jj = family["jj"]
    box = _box(jj, family["dimagi"], kind=Runner.CLOUD)
    RunnerEnrollment.objects.create(runner=box, workspace=family["marshellis"])
    assert services.enroll_runner(box, "marshellis").workspace_id == "marshellis"
    assert services.enroll_runner(box, "dimagi").workspace_id == "dimagi"
