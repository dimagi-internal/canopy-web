"""Nothing on canopy outside a workspace (canopy-web#1289).

Owner decision (Jonathan, 2026-10-08): every artifact URL lives under
`/w/<workspace>/…`, a share token still opens one artifact without a login, a
caller in several workspaces must say which one a write is for, and nobody
writes into a workspace where they are not an editor — including one that was
picked for them by default.

The live case this pins: ACE, an editor of both `connect` and `dimagi`, posted
a demo narrative meant for `connect` without naming a workspace. Every write
succeeded and landed in `dimagi`, where the `connect` reviewer could never find
it, and the flat token links hid the misfile because they open from anywhere.
"""
from __future__ import annotations

import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.reviews.models import ReviewRequest
from apps.session_sharing.models import Session, ShareToken
from apps.walkthroughs import storage
from apps.walkthroughs.models import Walkthrough
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_user, a_workspace

pytestmark = pytest.mark.django_db

PUBLIC = "https://canopy.example.org"


@pytest.fixture(autouse=True)
def _env(settings, monkeypatch):
    settings.CANOPY_PUBLIC_BASE_URL = PUBLIC
    settings.WALKTHROUGHS_ENABLED = True
    monkeypatch.setattr(
        storage, "store_upload", lambda **kw: storage.StoredFile(file_id="f", folder_id="d")
    )


@pytest.fixture
def dimagi():
    return a_workspace()  # the org default


@pytest.fixture
def connect(dimagi):
    return a_workspace("connect")


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _agent(email, roles: dict) -> Client:
    """A caller holding `roles` ({workspace: role})."""
    user = a_user(email)
    for ws, role in roles.items():
        a_member(ws, email=email, role=role)
    return _client(user)


def _upload_walkthrough(c, prefix="/api", **extra):
    return c.post(
        f"{prefix}/walkthroughs/",
        data={
            "file": SimpleUploadedFile("v.mp4", b"x", content_type="video/mp4"),
            "kind": "video",
            "visibility": "link",
        },
        format="multipart",
        **extra,
    )


def _open_review(c, prefix="/api"):
    return c.post(
        f"{prefix}/reviews/",
        data=json.dumps({
            "visibility": "link",
            "request_json": {
                "run_id": "chlorine-2026-10-07-001",
                "gate": "concept_change",
                "narrative_slug": "chlorine",
            },
        }),
        content_type="application/json",
    )


def _post_shareout(c, prefix="/api"):
    return c.post(
        f"{prefix}/shareouts/",
        data=json.dumps({"shareouts": [{
            "project_slug": "canopy-web",
            "period_start": "2026-10-07T09:00:00Z",
            "period_end": "2026-10-08T09:00:00Z",
            "source": "ace",
            "title": "t",
            "content": "b",
        }]}),
        content_type="application/json",
    )


def _share_session(c, prefix="/api"):
    rows = [
        {"type": "system", "subtype": "init", "session_id": f"s-{uuid.uuid4()}"},
        {"type": "user", "message": {"content": "hello"}},
    ]
    body = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    return c.post(
        f"{prefix}/sessions/upload",
        data={"file": SimpleUploadedFile("s.jsonl", body), "visibility": "link"},
        format="multipart",
    )


WRITES = [_upload_walkthrough, _open_review, _post_shareout, _share_session]


# --------------------------------------------------------------- no silent default


@pytest.mark.parametrize("write", WRITES, ids=lambda w: w.__name__)
def test_a_caller_in_two_workspaces_who_names_neither_is_refused(write, dimagi, connect):
    """The chlorine misfile, at each door it went through."""
    ace = _agent("ace@dimagi-ai.com", {dimagi: M.EDITOR, connect: M.EDITOR})
    resp = write(ace)
    assert resp.status_code == 422, resp.content
    detail = resp.json()["detail"]
    assert "connect" in detail and "dimagi" in detail, detail
    # and nothing was written anywhere
    assert not Walkthrough.objects.exists()
    assert not ReviewRequest.objects.exists()
    assert not Session.objects.exists()


@pytest.mark.parametrize("write", WRITES, ids=lambda w: w.__name__)
def test_naming_the_workspace_files_it_there(write, dimagi, connect):
    ace = _agent("ace@dimagi-ai.com", {dimagi: M.EDITOR, connect: M.EDITOR})
    resp = write(ace, prefix="/api/w/connect")
    assert resp.status_code == 201, resp.content
    rows = [*Walkthrough.objects.all(), *ReviewRequest.objects.all(), *Session.objects.all()]
    assert all(r.workspace_id == "connect" for r in rows)


@pytest.mark.parametrize("write", WRITES, ids=lambda w: w.__name__)
def test_a_single_workspace_caller_may_still_default(write, dimagi, connect):
    solo = _agent("solo@dimagi.com", {connect: M.EDITOR})
    assert write(solo).status_code == 201


# ----------------------------------------------------------- editor on the target


@pytest.mark.parametrize("write", WRITES, ids=lambda w: w.__name__)
def test_a_defaulted_target_still_needs_the_editor_role(write, dimagi, connect):
    """The default resolved a workspace; it did not grant a write in it."""
    viewer = _agent("viewer@dimagi.com", {dimagi: M.VIEWER})
    resp = write(viewer)
    assert resp.status_code == 403, resp.content


@pytest.mark.parametrize("write", WRITES, ids=lambda w: w.__name__)
def test_a_named_target_needs_the_editor_role_too(write, dimagi, connect):
    ace = _agent("ace@dimagi-ai.com", {dimagi: M.VIEWER, connect: M.EDITOR})
    assert write(ace, prefix="/api/w/dimagi").status_code == 403
    assert write(ace, prefix="/api/w/connect").status_code == 201


@pytest.mark.parametrize("write", WRITES, ids=lambda w: w.__name__)
def test_no_write_lands_in_a_workspace_the_caller_is_not_in(write, dimagi, connect):
    outsider = _agent("outsider@dimagi.com", {connect: M.EDITOR})
    assert write(outsider, prefix="/api/w/dimagi").status_code == 404


# ------------------------------------------------------------------ scoped links


def test_a_walkthrough_share_url_is_under_its_workspace_on_the_public_base(dimagi, connect):
    """It used to read `https://localhost/walkthrough/…`: built from the request's
    host, which an in-process MCP call does not have."""
    ace = _agent("ace@dimagi-ai.com", {connect: M.EDITOR})
    body = _upload_walkthrough(ace, HTTP_HOST="localhost").json()
    assert body["workspace"] == "connect"
    w = Walkthrough.objects.get()
    assert body["share_url"] == f"{PUBLIC}/w/connect/walkthrough/{w.id}?t={w.share_token}"


def test_a_review_create_answers_with_its_scoped_page(dimagi, connect):
    ace = _agent("ace@dimagi-ai.com", {connect: M.EDITOR})
    body = _open_review(ace).json()
    r = ReviewRequest.objects.get()
    assert body["url"] == f"/w/connect/review/{r.id}/"
    assert body["share_url"] == f"{PUBLIC}/w/connect/review/{r.id}/?t={r.share_token}"
    assert body["workspace"] == "connect"


def test_a_session_share_answers_with_its_scoped_page(dimagi, connect):
    ace = _agent("ace@dimagi-ai.com", {connect: M.EDITOR})
    body = _share_session(ace).json()
    assert body["share_url"] == f"{PUBLIC}/w/connect/share/{body['share_token']}"
    assert Session.objects.get().workspace_id == "connect"


# -------------------------------------------- the token is checked against the row


def test_a_token_opens_the_walkthrough_only_at_its_own_workspace(dimagi, connect):
    owner = a_member(connect, email="o@dimagi.com", role=M.EDITOR)
    w = Walkthrough.objects.create(
        title="t", kind="video", owner=owner, workspace=connect, visibility="link",
        content_type="video/mp4", size_bytes=1,
    )
    t = w.ensure_share_token()
    anon = Client()
    ok = anon.get(f"/api/walkthroughs/{w.id}/", {"t": t, "ws": "connect"})
    assert ok.status_code == 200 and ok.json()["workspace"] == "connect"
    assert anon.get(f"/api/walkthroughs/{w.id}/", {"t": t, "ws": "dimagi"}).status_code == 404
    # The flat read (what the old-link redirect asks) still answers, with the
    # workspace to redirect to.
    assert anon.get(f"/api/walkthroughs/{w.id}/", {"t": t}).json()["workspace"] == "connect"


def test_a_review_opens_only_at_its_own_workspace(dimagi, connect):
    r = ReviewRequest.objects.create(
        run_id="x-2026-10-07-001", gate="concept_change", status="pending",
        request_json={}, visibility="link", workspace=connect,
    )
    anon = Client()
    assert anon.get(f"/api/reviews/{r.id}/", {"ws": "connect"}).json()["workspace"] == "connect"
    assert anon.get(f"/api/reviews/{r.id}/", {"ws": "dimagi"}).status_code == 404


def test_a_share_token_opens_only_at_its_own_workspace(dimagi, connect):
    owner = a_member(connect, email="o@dimagi.com", role=M.EDITOR)
    s = Session.objects.create(owner=owner, workspace=connect, title="t")
    tok = ShareToken.objects.create(session=s, created_by=owner).token
    anon = Client()
    assert anon.get(f"/api/share/{tok}", {"ws": "connect"}).json()["workspace"] == "connect"
    assert anon.get(f"/api/share/{tok}", {"ws": "dimagi"}).status_code == 404
    assert anon.get(f"/api/share/{tok}").json()["workspace"] == "connect"


# --------------------------------------------- an anonymous visitor reaches the page


@pytest.mark.parametrize("path", [
    "/w/connect/walkthrough/00000000-0000-0000-0000-000000000000",
    "/w/connect/review/00000000-0000-0000-0000-000000000000",
    "/w/connect/share/sometoken",
])
def test_the_scoped_viewer_shells_load_without_a_login(path, settings, connect):
    settings.REQUIRE_AUTH = True
    # Not bounced to login. (The SPA itself is not built in tests, so the shell
    # answers 503 here and 200 in a deployment — either way, the gate is open.)
    assert Client().get(path).status_code not in (302, 401)


@pytest.mark.parametrize("path", ["/w/connect/walkthroughs", "/w/connect/agents", "/w/connect/reviewers/x"])
def test_the_rest_of_the_tenant_shell_stays_behind_the_login(path, settings, connect):
    settings.REQUIRE_AUTH = True
    assert Client().get(path).status_code == 302


# ----------------------------------------------- shared sessions predating the FK


def test_the_backfill_homes_each_share_in_its_owners_workspace(dimagi, connect):
    import importlib

    from django.apps import apps as live_apps

    home_rows = importlib.import_module(
        "apps.session_sharing.migrations.0005_workspace"
    ).home_rows
    solo = a_member(connect, email="solo@dimagi.com", role=M.EDITOR)
    both = a_member(connect, email="both@dimagi.com", role=M.EDITOR)
    a_member(dimagi, email="both@dimagi.com", role=M.EDITOR)
    nowhere = a_user("nowhere@example.org")
    rows = {u.email: Session.objects.create(owner=u, title="t") for u in (solo, both, nowhere)}

    home_rows(live_apps, None)

    homed = {e: Session.objects.get(pk=s.pk).workspace_id for e, s in rows.items()}
    assert homed == {
        "solo@dimagi.com": "connect",
        # in several: the org default, where their flat writes used to land
        "both@dimagi.com": "dimagi",
        "nowhere@example.org": None,
    }
