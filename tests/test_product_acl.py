"""The product surfaces' write gate: mutating is the EDITOR tier.

Before this, any member of a workspace — a viewer included — could mutate every
product surface: edit and delete projects, wipe the insights feed with `{}`,
approve a DDD gate, delete a narrative (and its Drive files), rewrite a
storyboard, forge the fleet event log. And rows with no workspace were readable
and writable by ANY signed-in user. The rules pinned here, per surface:

- a viewer READS (200) and is refused a write with 403 — they can already see
  the thing, so the refusal leaks nothing;
- a non-member gets 404 for the same write — existence must not leak;
- an editor writes;
- a NULL-workspace row is visible to nobody;
- the anonymous, token-gated public reads are exactly what they were.
"""
from __future__ import annotations

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.events.models import Event
from apps.feedback import services as feedback_services
from apps.issues.models import OriginIssue
from apps.projects.models import Project, ProjectContext
from apps.reviews.models import ReviewRequest
from apps.shareouts.models import Shareout
from apps.storyboards.models import Storyboard
from apps.walkthroughs import storage
from apps.walkthroughs.models import Walkthrough
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import remove_member, set_member_role
from apps.workspaces.testing import a_member, a_workspace
from tests.fixtures.fake_drive import FakeDriveClient

pytestmark = pytest.mark.django_db

WS = "pa-ws"
OTHER = "pa-other"


@pytest.fixture
def acl():
    ws = a_workspace(WS)
    other = a_workspace(OTHER)
    return {
        "ws": ws,
        "other": other,
        "owner": a_member(ws, email="pa-owner@dimagi.com", role=WorkspaceMembership.OWNER),
        "editor": a_member(ws, email="pa-editor@dimagi.com", role=WorkspaceMembership.EDITOR),
        "viewer": a_member(ws, email="pa-viewer@dimagi.com", role=WorkspaceMembership.VIEWER),
        "outsider": a_member(other, email="pa-outsider@dimagi.com", role=WorkspaceMembership.OWNER),
    }


@pytest.fixture
def fake_drive(monkeypatch):
    inst = FakeDriveClient()
    monkeypatch.setattr(storage, "get_drive_client", lambda: inst)
    return inst


def _c(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _json(client, method, url, body=None):
    return getattr(client, method)(
        url, data=json.dumps(body if body is not None else {}), content_type="application/json"
    )


# --- projects + insights -----------------------------------------------------


def _project(slug, workspace_id=WS):
    return Project.objects.create(name=slug, slug=slug, workspace_id=workspace_id)


def _insight(project, content="[a] x"):
    return ProjectContext.objects.create(project=project, context_type="insight", content=content)


def test_project_writes_are_editor_reads_are_membership(acl):
    _project("pa-proj")
    viewer = _c(acl["viewer"])
    assert viewer.get("/api/projects/pa-proj/").status_code == 200
    assert _json(viewer, "patch", "/api/projects/pa-proj/", {"name": "x"}).status_code == 403
    assert viewer.delete("/api/projects/pa-proj/").status_code == 403
    assert _json(viewer, "post", "/api/projects/pa-proj/context/",
                 {"context_type": "note", "content": "x", "source": "t"}).status_code == 403
    assert _json(_c(acl["outsider"]), "patch", "/api/projects/pa-proj/", {"name": "x"}).status_code == 404

    res = _json(_c(acl["editor"]), "patch", "/api/projects/pa-proj/", {"name": "renamed"})
    assert res.status_code == 200, res.content
    assert Project.objects.get(slug="pa-proj").name == "renamed"


def test_a_viewer_cannot_create_or_batch_write_projects(acl):
    viewer = _c(acl["viewer"])
    body = {"name": "New", "slug": "pa-new"}
    assert _json(viewer, "post", f"/api/w/{WS}/projects/", body).status_code == 403
    assert not Project.objects.filter(slug="pa-new").exists()

    _project("pa-proj")
    res = _json(viewer, "post", "/api/projects/batch-context/",
                {"updates": {"pa-proj": [{"context_type": "note", "content": "x", "source": "t"}]}})
    assert res.status_code == 201
    assert res.json() == {"pa-proj": 0}
    assert not ProjectContext.objects.exists()

    assert _json(_c(acl["editor"]), "post", f"/api/w/{WS}/projects/", body).status_code == 201


def test_a_viewers_empty_clear_deletes_nothing(acl):
    """`{}` used to clear every insight in every workspace the caller could READ."""
    _insight(_project("pa-proj"))
    res = _json(_c(acl["viewer"]), "post", "/api/insights/clear/", {})
    assert res.status_code == 200
    assert res.json() == {"cleared": 0}
    assert ProjectContext.objects.count() == 1


def test_an_editors_empty_clear_stays_in_their_editor_workspaces(acl):
    """An editor of one workspace who only views another clears the first."""
    mine = _insight(_project("pa-proj"))
    a_member(acl["other"], email="pa-editor@dimagi.com", role=WorkspaceMembership.VIEWER)
    theirs = _insight(_project("pa-theirs", OTHER))

    res = _json(_c(acl["editor"]), "post", "/api/insights/clear/", {})
    assert res.json() == {"cleared": 1}
    assert not ProjectContext.objects.filter(pk=mine.pk).exists()
    assert ProjectContext.objects.filter(pk=theirs.pk).exists()


def test_dismissing_an_insight_is_editor(acl):
    ins = _insight(_project("pa-proj"))
    assert _c(acl["viewer"]).delete(f"/api/insights/{ins.pk}/").status_code == 403
    assert _c(acl["outsider"]).delete(f"/api/insights/{ins.pk}/").status_code == 404
    res = _json(_c(acl["viewer"]), "post", "/api/insights/dismiss", {"ids": [ins.pk]})
    assert res.json() == {"dismissed": []}
    assert _c(acl["editor"]).delete(f"/api/insights/{ins.pk}/").status_code == 200


def test_a_null_workspace_project_is_visible_to_nobody(acl):
    orphan = Project.objects.create(name="orphan", slug="pa-orphan")
    _insight(orphan)
    owner = _c(acl["owner"])
    assert owner.get("/api/projects/pa-orphan/").status_code == 404
    assert _json(owner, "patch", "/api/projects/pa-orphan/", {"name": "x"}).status_code == 404
    assert "pa-orphan" not in [p["slug"] for p in owner.get("/api/projects/").json()["items"]]
    assert owner.get("/api/insights/").json()["items"] == []
    assert _json(owner, "post", "/api/insights/clear/", {}).json() == {"cleared": 0}


# --- walkthroughs -------------------------------------------------------------


def _walkthrough(owner, workspace_id=WS, **kw):
    defaults = dict(
        title="w", kind="html", drive_file_id="f", drive_folder_id="d",
        content_type="text/html", size_bytes=1, owner=owner, workspace_id=workspace_id,
    )
    defaults.update(kw)
    return Walkthrough.objects.create(**defaults)


def _upload(client, url="/api/walkthroughs/", **fields):
    data = {"file": SimpleUploadedFile("s.html", b"<html/>", content_type="text/html"),
            "kind": "html", **fields}
    return client.post(url, data=data)


@override_settings(CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder")
def test_uploading_a_walkthrough_is_editor(acl, fake_drive):
    assert _upload(_c(acl["viewer"]), f"/api/w/{WS}/walkthroughs/").status_code == 403
    assert not Walkthrough.objects.exists()
    assert _upload(_c(acl["editor"]), f"/api/w/{WS}/walkthroughs/").status_code == 201


@override_settings(CANOPY_DRIVE_ROOT_FOLDER_ID="root-folder")
def test_a_reupload_replaces_only_this_workspaces_artifact(acl, fake_drive):
    """run_id is client-supplied: an upload in one tenant must not delete
    another tenant's artifact by naming the same run and role."""
    theirs = _walkthrough(acl["outsider"], OTHER, run_id="pa-run-1", role="deck")
    res = _upload(_c(acl["editor"]), f"/api/w/{WS}/walkthroughs/", run_id="pa-run-1", role="deck")
    assert res.status_code == 201, res.content
    assert Walkthrough.objects.filter(pk=theirs.pk).exists()


def test_a_member_who_did_not_upload_it_is_refused_403(acl):
    w = _walkthrough(acl["editor"])
    other_editor = a_member(acl["ws"], email="pa-ed2@dimagi.com", role=WorkspaceMembership.EDITOR)
    c = _c(other_editor)
    assert c.get(f"/api/walkthroughs/{w.id}/").status_code == 200
    assert _json(c, "patch", f"/api/walkthroughs/{w.id}/", {"title": "x"}).status_code == 403
    assert c.post(f"/api/walkthroughs/{w.id}/rotate-token").status_code == 403
    assert c.delete(f"/api/walkthroughs/{w.id}/").status_code == 403


def test_an_ex_member_uploader_loses_their_walkthroughs(acl):
    """PATCH / rotate-token / DELETE were uploader-only with NO membership check."""
    w = _walkthrough(acl["editor"], visibility="link")
    w.ensure_share_token()
    remove_member(workspace=acl["ws"], user_id=acl["editor"].pk)
    c = _c(acl["editor"])
    assert _json(c, "patch", f"/api/walkthroughs/{w.id}/", {"visibility": "private"}).status_code == 404
    assert c.post(f"/api/walkthroughs/{w.id}/rotate-token").status_code == 404
    assert c.delete(f"/api/walkthroughs/{w.id}/").status_code == 404
    assert Walkthrough.objects.get(pk=w.pk).visibility == "link"


def test_a_demoted_uploader_reads_but_no_longer_writes(acl):
    w = _walkthrough(acl["editor"])
    set_member_role(workspace=acl["ws"], user_id=acl["editor"].pk, role=WorkspaceMembership.VIEWER)
    c = _c(acl["editor"])
    body = c.get(f"/api/walkthroughs/{w.id}/").json()
    assert body["is_owner"] is False  # the edit controls follow the write gate
    assert _json(c, "patch", f"/api/walkthroughs/{w.id}/", {"title": "x"}).status_code == 403


def test_a_workspace_owner_may_take_down_anyones_walkthrough(acl):
    w = _walkthrough(acl["editor"])
    c = _c(acl["owner"])
    assert c.get(f"/api/walkthroughs/{w.id}/").json()["is_owner"] is True
    assert c.delete(f"/api/walkthroughs/{w.id}/").status_code == 204


def test_a_null_workspace_walkthrough_is_token_only(acl):
    w = _walkthrough(acl["owner"], workspace_id=None, visibility="link")
    token = w.ensure_share_token()
    owner = _c(acl["owner"])
    assert owner.get(f"/api/walkthroughs/{w.id}/").status_code == 404
    assert str(w.id) not in [x["id"] for x in owner.get("/api/walkthroughs/").json()]
    assert owner.delete(f"/api/walkthroughs/{w.id}/").status_code == 404
    # The public token read is a property of the link, not the tenant.
    assert Client().get(f"/api/walkthroughs/{w.id}/?t={token}").status_code == 200


def test_the_public_walkthrough_token_read_is_unchanged(acl):
    w = _walkthrough(acl["editor"], visibility="link")
    token = w.ensure_share_token()
    assert Client().get(f"/api/walkthroughs/{w.id}/?t={token}").status_code == 200
    assert Client().get(f"/api/walkthroughs/{w.id}/?t=wrong").status_code == 404


# --- shareouts ----------------------------------------------------------------


def _shareout_body(title="t", source="canopy:shareout"):
    return {"shareouts": [{
        "period_start": "2026-10-01T00:00:00Z", "period_end": "2026-10-02T00:00:00Z",
        "title": title, "content": "c", "source": source,
    }]}


def test_posting_a_shareout_is_editor(acl):
    assert _json(_c(acl["viewer"]), "post", f"/api/w/{WS}/shareouts/", _shareout_body()).status_code == 403
    res = _json(_c(acl["editor"]), "post", f"/api/w/{WS}/shareouts/", _shareout_body())
    assert res.status_code == 201
    assert Shareout.objects.get().created_by == acl["editor"]


def test_a_repost_replaces_only_the_posters_own_rows(acl):
    other_editor = a_member(acl["ws"], email="pa-ed2@dimagi.com", role=WorkspaceMembership.EDITOR)
    _json(_c(other_editor), "post", f"/api/w/{WS}/shareouts/", _shareout_body("theirs"))
    _json(_c(acl["editor"]), "post", f"/api/w/{WS}/shareouts/", _shareout_body("mine v1"))
    res = _json(_c(acl["editor"]), "post", f"/api/w/{WS}/shareouts/", _shareout_body("mine v2"))
    assert res.json()["replaced"] == 1
    assert sorted(Shareout.objects.values_list("title", flat=True)) == ["mine v2", "theirs"]


def test_clearing_shareouts_is_your_own_unless_you_own_the_workspace(acl):
    other_editor = a_member(acl["ws"], email="pa-ed2@dimagi.com", role=WorkspaceMembership.EDITOR)
    _json(_c(other_editor), "post", f"/api/w/{WS}/shareouts/", _shareout_body("theirs"))
    _json(_c(acl["editor"]), "post", f"/api/w/{WS}/shareouts/", _shareout_body("mine"))

    assert _json(_c(acl["viewer"]), "post", "/api/shareouts/clear/", {}).json() == {"cleared": 0}
    assert _json(_c(acl["editor"]), "post", "/api/shareouts/clear/", {}).json() == {"cleared": 1}
    assert list(Shareout.objects.values_list("title", flat=True)) == ["theirs"]
    assert _json(_c(acl["owner"]), "post", "/api/shareouts/clear/", {}).json() == {"cleared": 1}
    assert not Shareout.objects.exists()


# --- reviews ------------------------------------------------------------------


def _review(workspace_id=WS, visibility="private", **kw):
    defaults = dict(run_id="pa-run-1", gate="pre_ship", request_json={"narrative": "s"},
                    visibility=visibility, workspace_id=workspace_id)
    defaults.update(kw)
    return ReviewRequest.objects.create(**defaults)


def test_opening_a_review_is_editor(acl):
    body = {"request_json": {"run_id": "pa-run-1", "gate": "pre_ship"}}
    assert _json(_c(acl["viewer"]), "post", f"/api/w/{WS}/reviews/", body).status_code == 403
    assert _json(_c(acl["editor"]), "post", f"/api/w/{WS}/reviews/", body).status_code == 201


def test_approving_a_gate_is_editor(acl):
    r = _review()
    viewer = _c(acl["viewer"])
    assert viewer.get(f"/api/reviews/{r.id}/").status_code == 200
    assert _json(viewer, "post", f"/api/reviews/{r.id}/submit/", {"response_json": {}}).status_code == 403
    assert _json(_c(acl["outsider"]), "post", f"/api/reviews/{r.id}/submit/",
                 {"response_json": {}}).status_code == 404
    res = _json(_c(acl["editor"]), "post", f"/api/reviews/{r.id}/submit/", {"response_json": {}})
    assert res.status_code == 200, res.content
    r.refresh_from_db()
    assert r.status == ReviewRequest.STATUS_RESOLVED


def test_deleting_a_review_is_editor(acl):
    r = _review()
    assert _c(acl["viewer"]).delete(f"/api/reviews/{r.id}/").status_code == 403
    assert _c(acl["outsider"]).delete(f"/api/reviews/{r.id}/").status_code == 404
    assert _c(acl["editor"]).delete(f"/api/reviews/{r.id}/").status_code == 204


@override_settings(REQUIRE_AUTH=True)
def test_the_public_review_read_is_unchanged(acl):
    r = _review(visibility="link")
    assert Client().get(f"/api/reviews/{r.id}/").status_code == 200
    # ...and an anonymous reader still cannot resolve it.
    assert _json(Client(), "post", f"/api/reviews/{r.id}/submit/", {"response_json": {}}).status_code == 403


def test_a_null_workspace_review_is_visible_to_no_member(acl):
    r = _review(workspace_id=None)
    owner = _c(acl["owner"])
    assert owner.get(f"/api/reviews/{r.id}/").status_code == 404
    assert owner.get("/api/reviews/").json() == []
    assert owner.delete(f"/api/reviews/{r.id}/").status_code == 404


# --- DDD runs + narratives ---------------------------------------------------


def _narrative(workspace_id=WS):
    return _review(
        workspace_id=workspace_id, gate="concept_change", narrative_slug="pa-story",
        run_id="pa-story-2026-10-02-001", version=1, visibility="link",
        request_json={"gate": "concept_change", "narrative_slug": "pa-story",
                      "narration": [{"id": "goal", "title": "T", "text": "v1"}]},
    )


def test_deleting_ddd_rows_is_editor(acl):
    _narrative()
    viewer = _c(acl["viewer"])
    assert viewer.get("/api/ddd/narratives/pa-story/").status_code == 200
    assert viewer.delete("/api/ddd/runs/pa-story-2026-10-02-001/").status_code == 403
    assert viewer.delete("/api/ddd/narratives/pa-story/versions/1/").status_code == 403
    assert viewer.delete("/api/ddd/narratives/pa-story/").status_code == 403
    assert _c(acl["outsider"]).delete("/api/ddd/narratives/pa-story/").status_code == 404
    assert ReviewRequest.objects.count() == 1
    assert _c(acl["editor"]).delete("/api/ddd/narratives/pa-story/").status_code == 204
    assert not ReviewRequest.objects.exists()


def test_setting_narrative_visibility_is_editor(acl):
    _narrative()
    url = "/api/ddd/narratives/pa-story/visibility/"
    assert _json(_c(acl["viewer"]), "patch", url, {"visibility": "private"}).status_code == 403
    assert ReviewRequest.objects.get().visibility == "link"
    assert _json(_c(acl["editor"]), "patch", url, {"visibility": "private"}).status_code == 200
    assert ReviewRequest.objects.get().visibility == "private"


def test_moving_a_narrative_needs_editor_on_both_sides(acl):
    _narrative()
    url = "/api/ddd/narratives/pa-story/move/"
    # Editor of the source, only a viewer of the destination.
    a_member(acl["other"], email="pa-editor@dimagi.com", role=WorkspaceMembership.VIEWER)
    res = _json(_c(acl["editor"]), "post", url, {"to_workspace": OTHER, "dry_run": False})
    assert res.status_code == 403
    # Editor of the destination, only a viewer of the source.
    a_member(acl["other"], email="pa-viewer@dimagi.com", role=WorkspaceMembership.EDITOR)
    res = _json(_c(acl["viewer"]), "post", url, {"to_workspace": OTHER, "dry_run": False})
    assert res.status_code == 403
    assert ReviewRequest.objects.get().workspace_id == WS


def test_a_move_does_not_carry_an_unseen_tenants_walkthrough(acl):
    """source_workspaces counted only reviews, so a walkthrough sharing the slug
    in a workspace the caller cannot see rode along into theirs."""
    _narrative()
    a_workspace("pa-third")
    _walkthrough(acl["outsider"], workspace_id="pa-third", narrative_slug="pa-story")
    a_member(acl["other"], email="pa-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    res = _json(_c(acl["editor"]), "post", "/api/ddd/narratives/pa-story/move/",
                {"to_workspace": OTHER, "dry_run": False})
    assert res.status_code == 403
    assert "pa-third" not in res.json()["title"]  # a tenant the caller is not in is never named
    assert Walkthrough.objects.get().workspace_id == "pa-third"


# --- storyboards ---------------------------------------------------------------


def _board():
    return Storyboard.objects.create(slug="pa-board", title="B", workspace_id=WS)


def test_storyboard_writes_are_editor(acl):
    board = _board()
    viewer = _c(acl["viewer"])
    assert viewer.get("/api/storyboards/pa-board").status_code == 200
    assert viewer.get("/api/storyboards/pa-board/notes").status_code == 200
    assert _json(viewer, "patch", "/api/storyboards/pa-board", {"title": "x"}).status_code == 403
    assert viewer.post("/api/storyboards/pa-board/share").status_code == 403
    assert viewer.post("/api/storyboards/pa-board/rotate-token").status_code == 403
    assert _json(_c(acl["outsider"]), "patch", "/api/storyboards/pa-board", {"title": "x"}).status_code == 404
    board.refresh_from_db()
    assert board.title == "B" and not board.share_token

    assert _json(_c(acl["editor"]), "patch", "/api/storyboards/pa-board", {"title": "y"}).status_code == 200


def test_creating_a_storyboard_is_editor(acl):
    body = {"slug": "pa-new", "title": "N"}
    assert _json(_c(acl["viewer"]), "post", f"/api/w/{WS}/storyboards/", body).status_code == 403
    assert _json(_c(acl["editor"]), "post", f"/api/w/{WS}/storyboards/", body).status_code == 200


@override_settings(REQUIRE_AUTH=True)
def test_the_public_storyboard_read_and_feedback_are_unchanged(acl):
    board = _board()
    board.capability = Storyboard.CAP_COMMENT
    board.save()
    token = board.ensure_share_token()
    assert Client().get(f"/api/storyboards/pa-board?t={token}").status_code == 200
    assert Client().get("/api/storyboards/pa-board?t=wrong").status_code == 404
    res = _json(Client(), "post", f"/api/storyboards/pa-board/feedback?t={token}",
                {"kind": "comment", "body": "nice"})
    assert res.status_code == 200, res.content


def test_disposing_of_a_note_is_editor(acl):
    _board()
    feedback_services.ingest(
        [{"target_kind": "storyboard", "target_ref": "pa-board", "kind": "comment",
          "body": "hi", "channel": "web", "source_ref": ""}],
        workspace=acl["ws"],
    )
    from apps.feedback.models import Feedback

    fb = Feedback.objects.get()
    url = f"/api/feedback/{fb.pk}/resolve"
    assert _json(_c(acl["viewer"]), "post", url, {"state": "declined"}).status_code == 403
    assert _json(_c(acl["outsider"]), "post", url, {"state": "declined"}).status_code == 404
    assert _json(_c(acl["editor"]), "post", url, {"state": "declined"}).status_code == 200


# --- issues -----------------------------------------------------------------


def test_origin_records_are_editor_to_write(acl):
    body = {"repo": "dimagi/x", "number": 7, "title": "T", "agent": "nobody"}
    assert _json(_c(acl["viewer"]), "post", f"/api/w/{WS}/issues/", body).status_code == 403
    assert not OriginIssue.objects.exists()
    assert _json(_c(acl["editor"]), "post", f"/api/w/{WS}/issues/", body).status_code == 201

    viewer = _c(acl["viewer"])
    assert viewer.get("/api/issues/dimagi__x/7/").status_code == 200
    assert _json(viewer, "post", f"/api/w/{WS}/issues/", {**body, "title": "U"}).status_code == 403
    assert viewer.delete("/api/issues/dimagi__x/7/").status_code == 403
    assert _c(acl["outsider"]).delete("/api/issues/dimagi__x/7/").status_code == 404
    assert _c(acl["editor"]).delete("/api/issues/dimagi__x/7/").status_code == 204


def test_a_null_workspace_origin_record_is_visible_to_nobody(acl):
    OriginIssue.objects.create(repo="dimagi/x", number=8, title="T")
    owner = _c(acl["owner"])
    assert owner.get("/api/issues/dimagi__x/8/").status_code == 404
    assert owner.get("/api/issues/").json()["items"] == []
    # Nor is it the one row anybody may overwrite.
    res = _json(owner, "post", "/api/issues/", {"repo": "dimagi/x", "number": 8, "title": "mine"})
    assert res.status_code == 404
    assert OriginIssue.objects.get(number=8).title == "T"


# --- events -----------------------------------------------------------------


def test_recording_events_is_editor(acl):
    body = {"items": [{"source": "runner.credential", "kind": "claude_auth_required",
                       "level": "error", "key": "k"}]}
    assert _json(_c(acl["viewer"]), "post", f"/api/w/{WS}/events/", body).status_code == 403
    assert not Event.objects.exists()
    assert _json(_c(acl["editor"]), "post", f"/api/w/{WS}/events/", body).status_code == 200
    assert Event.objects.get().workspace_id == WS
    # Reading the log is the admin tier (permissions.LOGS_READ): a viewer and
    # an editor see no rows, an owner sees it.
    assert _c(acl["viewer"]).get("/api/events/").json()["items"] == []
    assert _c(acl["editor"]).get("/api/events/").json()["items"] == []
    assert len(_c(acl["owner"]).get("/api/events/").json()["items"]) == 1


def test_a_pinned_event_list_is_that_workspace_only(acl):
    Event.objects.create(workspace=acl["ws"], source="s", key="a")
    Event.objects.create(workspace=acl["other"], source="s", key="b")
    admin = a_member(acl["ws"], email="pa-admin@dimagi.com", role=WorkspaceMembership.ADMIN)
    a_member(acl["other"], email="pa-admin@dimagi.com", role=WorkspaceMembership.ADMIN)
    viewer = _c(admin)
    assert len(viewer.get("/api/events/").json()["items"]) == 2
    pinned = viewer.get(f"/api/w/{WS}/events/").json()["items"]
    assert [e["workspace"] for e in pinned] == [WS]
