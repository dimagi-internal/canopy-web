"""Dynamic views over REST (and so MCP): who may make, change and read one, and how
versions, sources and the frame behave."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import AgentProject
from apps.dynamic_views import catalog, frame
from apps.dynamic_views.models import DynamicView
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

J = "application/json"
CODE = "function View({ data }) { return <Card title='Hi'>{String(data.open?.length)}</Card>; }"
SOURCES = [{"alias": "open", "model": "tasks", "params": {"category": "started", "limit": 50}}]


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def world():
    owner, editor, viewer, outsider = (_user(n) for n in ("owner", "editor", "viewer", "outsider"))
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=outsider)
    for u, role in ((owner, WorkspaceMembership.OWNER), (editor, WorkspaceMembership.EDITOR),
                    (viewer, WorkspaceMembership.VIEWER)):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    WorkspaceMembership.objects.create(user=outsider, workspace=other, role=WorkspaceMembership.OWNER)
    project = AgentProject.objects.create(workspace=ws, handle="unga-2026", name="UNGA 2026")
    AgentProject.objects.create(workspace=other, handle="theirs", name="Theirs")
    return dict(ws=ws, other=other, project=project, owner=owner, editor=editor, viewer=viewer,
                outsider=outsider)


def _create(client, **body):
    body = {"workspace": "dimagi", "title": "Open work", "code": CODE, "sources": SOURCES, **body}
    return client.post("/api/dynamic-views/", body, content_type=J)


def test_an_editor_makes_a_workspace_view_and_a_project_view(world):
    c = _client(world["editor"])
    r = _create(c)
    assert r.status_code == 201, r.content
    body = r.json()
    assert (body["workspace"], body["project"], body["slug"], body["version"]) == ("dimagi", None, "open-work", 1)
    assert body["url"] == f"/w/dimagi/views/{body['id']}"
    assert body["sources"] == SOURCES

    r = _create(c, project="unga-2026", title="UNGA board")
    assert r.status_code == 201, r.content
    assert (r.json()["project"], r.json()["project_name"]) == ("unga-2026", "UNGA 2026")

    listed = c.get("/api/w/dimagi/dynamic-views/").json()
    assert {v["slug"] for v in listed} == {"open-work", "unga-board"}
    assert [v["slug"] for v in c.get("/api/w/dimagi/dynamic-views/?project=unga-2026").json()] == ["unga-board"]
    assert "code" not in listed[0]


def test_slugs_are_unique_per_workspace(world):
    c = _client(world["editor"])
    assert _create(c).json()["slug"] == "open-work"
    assert _create(c).json()["slug"] == "open-work-2"
    r = _create(c, slug="open-work")
    assert r.status_code == 422 and "already exists" in r.content.decode()


def test_a_viewer_reads_but_cannot_write(world):
    view_id = _create(_client(world["editor"])).json()["id"]
    v = _client(world["viewer"])
    assert v.get(f"/api/dynamic-views/{view_id}").status_code == 200
    assert _create(v).status_code == 403
    assert v.patch(f"/api/dynamic-views/{view_id}", {"title": "x"}, content_type=J).status_code == 403
    assert v.delete(f"/api/dynamic-views/{view_id}").status_code == 403


def test_another_workspace_cannot_see_or_touch_it(world):
    view_id = _create(_client(world["editor"])).json()["id"]
    o = _client(world["outsider"])
    assert o.get(f"/api/dynamic-views/{view_id}").status_code == 404
    assert o.patch(f"/api/dynamic-views/{view_id}", {"title": "x"}, content_type=J).status_code == 404
    assert o.get("/api/w/other/dynamic-views/").json() == []
    assert _create(o).status_code == 404  # writing into dimagi: not a workspace they can see


def test_a_project_must_belong_to_the_views_workspace(world):
    r = _create(_client(world["editor"]), project="theirs")
    assert r.status_code == 422 and "no project 'theirs'" in r.content.decode()


def test_code_and_sources_are_validated_on_write(world):
    c = _client(world["editor"])
    assert "must define" in _create(c, code="function Other() { return null }").content.decode()
    assert "model must be one of" in _create(c, sources=[{"alias": "x", "model": "vault"}]).content.decode()
    r = _create(c, sources=[{"alias": "x", "model": "tasks", "params": {"secret": 1}}])
    assert r.status_code == 422 and "does not take secret" in r.content.decode()
    r = _create(c, sources=[{"alias": "a", "model": "agent"}])
    assert r.status_code == 422 and "needs path" in r.content.decode()
    r = _create(c, sources=[{"alias": "a", "model": "agent", "path": {"slug": "../vault"}}])
    assert r.status_code == 422 and "not a valid identifier" in r.content.decode()
    # A project model may omit the handle: the page fills in the view's own project.
    assert _create(c, project="unga-2026", sources=[{"alias": "m", "model": "milestones"}]).status_code == 201


def test_every_change_is_a_version_and_rollback_restores_one(world):
    c = _client(world["editor"])
    view_id = _create(c).json()["id"]
    r = c.patch(f"/api/dynamic-views/{view_id}", {"code": CODE.replace("Hi", "Hello"), "note": "greet"},
                content_type=J)
    assert r.status_code == 200 and r.json()["version"] == 2
    # A patch that changes nothing is not a version.
    assert c.patch(f"/api/dynamic-views/{view_id}", {"title": "Open work"}, content_type=J).json()["version"] == 2
    revs = c.get(f"/api/dynamic-views/{view_id}/revisions/").json()
    assert [(x["version"], x["note"]) for x in revs] == [(2, "greet"), (1, "created")]

    r = c.post(f"/api/dynamic-views/{view_id}/rollback", {"version": 1}, content_type=J)
    assert r.status_code == 200
    assert (r.json()["version"], r.json()["code"]) == (3, CODE)
    assert c.post(f"/api/dynamic-views/{view_id}/rollback", {"version": 9}, content_type=J).status_code == 404


def test_moving_a_view_between_workspace_and_project(world):
    c = _client(world["editor"])
    view_id = _create(c).json()["id"]
    assert c.patch(f"/api/dynamic-views/{view_id}", {"project": "unga-2026"}, content_type=J).json()["project"] \
        == "unga-2026"
    assert c.patch(f"/api/dynamic-views/{view_id}", {"project": ""}, content_type=J).json()["project"] is None


def test_delete_removes_the_view_and_its_history(world):
    c = _client(world["editor"])
    view_id = _create(c).json()["id"]
    assert c.delete(f"/api/dynamic-views/{view_id}").status_code == 204
    assert not DynamicView.objects.filter(pk=view_id).exists()


def test_the_guide_names_the_whole_catalog(world):
    body = _client(world["viewer"]).get("/api/dynamic-views/guide/").json()
    assert "function View(props)" in body["contract"] and "function View(" in body["example"]
    names = {m["name"] for m in body["catalog"]}
    assert names == set(catalog.BY_NAME)
    assert {"tasks", "projects", "agents", "members", "milestones", "sessions"} <= names
    assert next(m for m in body["catalog"] if m["name"] == "project")["path_params"] == ["handle"]


def test_the_catalog_reads_nothing_secret():
    """A view's author sees what the view shows, so no catalog entry may name a
    secret-bearing surface. Adding one should be a deliberate edit of this list."""
    banned = ("vault", "secret", "token", "credential", "passkey", "mail", "transcript", "messages", "keyset")
    for m in catalog.CATALOG:
        assert not any(b in m.path for b in banned), m


def test_the_frame_is_opaque_networkless_and_sessionless(client):
    r = client.get(frame.PATH)
    assert r.status_code == 200
    policy = r["Content-Security-Policy"]
    assert policy.startswith("sandbox allow-scripts;")
    assert "connect-src 'none'" in policy and "default-src 'none'" in policy
    assert "frame-ancestors" in policy and "allow-same-origin" not in policy
    assert "Set-Cookie" not in r.headers
    assert r["Cache-Control"] == "no-store"
    assert client.post(frame.PATH).status_code == 405
    assert client.get("/dynamic-views/anything").status_code == 404


def test_the_frame_shell_names_its_built_runtime(client, monkeypatch, settings, tmp_path):
    vite = tmp_path / ".vite"
    vite.mkdir()
    (vite / "manifest.json").write_text(
        '{"src/dynamicViews/frame/main.tsx": {"file": "assets/dv-abc.js", "imports": ["_shared"]},'
        ' "_shared": {"file": "assets/shared.js", "css": ["assets/index.css"]}}')
    settings.FRONTEND_DIST_DIR = tmp_path
    from apps.embedding import views_embed
    views_embed._manifest.cache_clear()
    try:
        r = client.get(frame.PATH)
    finally:
        views_embed._manifest.cache_clear()
    body = r.content.decode()
    assert '<script type="module" src="/assets/dv-abc.js"></script>' in body
    assert '<link rel="stylesheet" href="/assets/index.css">' in body
    # The one inline script runs under this response's own nonce, and nothing else inline does.
    nonce = body.split('nonce="')[1].split('"')[0]
    assert f"'nonce-{nonce}'" in r["Content-Security-Policy"]
    assert "'unsafe-inline'" not in r["Content-Security-Policy"].split("script-src")[1].split(";")[0]
