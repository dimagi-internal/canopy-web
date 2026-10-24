"""A workspace project serves its own MCP server — publish once, call from anywhere.

The steward uploads a kb.sqlite.gz (presigned, straight to storage), publishes
it, and from then on `/api/mcp/projects/<ws>/<handle>/` answers that KB's tools
for anyone who may read the project — over real HTTP, with a real personal
token, through the same dispatcher `config/asgi.py` mounts. Nobody downloads
anything but canopy itself.
"""
from __future__ import annotations

import gzip
import hashlib
import json

import httpx2
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client, override_settings
from starlette.applications import Starlette
from starlette.routing import Mount

from apps.agents import kb_store
from apps.agents.models import AgentProject
from apps.mcp.projects import ProjectMcpDispatch, project_mcp
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from canopy_kb import build
from tests.fixtures import kb as kb_fixtures

pytestmark = pytest.mark.django_db

# the bucket and project fixtures (a fixture is found by its name in the module)
s3, world = kb_fixtures.s3, kb_fixtures.world
FakeS3 = kb_fixtures.FakeS3

J = "application/json"
BASE = "https://canopy.test"


ORIGINAL = {"sha256": "c" * 64, "filename": "Diarrheal disease.txt", "content_type": "text/plain", "bytes": 52,
            "source_url": "https://gw/1/transcript.txt", "fetched_at": "2026-10-20T00:00:00Z", "path": "/eva/raw/x"}
ORIGINALS = "workspace-projects/{}/givewell-kb/originals/"


def _kb_gz(tmp_path, original=None) -> bytes:
    b = tmp_path / "bundle"
    (b / "tables").mkdir(parents=True)
    (b / "manifest.json").write_text(json.dumps({
        "name": "givewell", "publisher": "GiveWell", "license": "CC BY-NC-SA 3.0 US",
        "tables": {"grants": {"description": "Every grant.", "date_column": "Date", "amount_column": "Amount",
                              "filter_columns": ["Recipient"]}}}))
    (b / "items.jsonl").write_text(json.dumps(
        {"id": "p:1", "type": "podcast_episode", "title": "Diarrheal disease", "url": "https://gw/1",
         "date": "2026-07-23", "text": "WASHmobile follows up households after a cholera case.", "sha256": "a",
         **({"original": original} if original else {})}))
    (b / "tables" / "grants.jsonl").write_text(json.dumps(
        {"Grant": "Water pilot", "Recipient": "Dimagi Inc", "Amount": 1069324, "Date": "2025-12-03"}))
    db = tmp_path / "kb.sqlite"
    build(b, db)
    return gzip.compress(db.read_bytes())


def _publish(world, s3, tmp_path, client=None, original=None):
    client = client or world["client"]
    up = client.post("/api/workspace-projects/connect/givewell-kb/kb/upload", content_type=J)
    assert up.status_code == 200, up.content
    body = _kb_gz(tmp_path, original)
    s3.objects[up.json()["key"]] = body  # what the curator's PUT to the signed url does
    kb = AgentProject.objects.get(handle="givewell-kb").knowledge_base
    head = kb.kb_version if kb else None
    return client.post("/api/workspace-projects/connect/givewell-kb/kb/publish",
                       {"key": up.json()["key"], "sha256": hashlib.sha256(body).hexdigest(),
                        "base": head.number if head else None}, content_type=J)


def test_publishing_makes_the_project_serve_its_own_mcp(world, s3, tmp_path):
    assert world["client"].get("/api/workspace-projects/connect/givewell-kb/").json()["mcp_path"] == ""

    r = _publish(world, s3, tmp_path)

    assert r.status_code == 200, r.content
    assert (r.json()["mcp_kind"], r.json()["mcp_path"]) == ("kb", "/api/mcp/projects/connect/givewell-kb/")
    assert r.json()["kb_published_at"]


def test_a_republish_serves_the_new_file_and_keeps_the_old_one_as_a_version(world, s3, tmp_path):
    _publish(world, s3, tmp_path)
    first = AgentProject.objects.get(handle="givewell-kb").knowledge_base.kb_key
    (tmp_path / "bundle").rename(tmp_path / "old-bundle")
    (tmp_path / "kb.sqlite").unlink()
    _publish(world, s3, tmp_path)

    project = AgentProject.objects.get(handle="givewell-kb")
    assert project.knowledge_base.kb_key != first and first in s3.objects
    assert [(v.number, v.kind, v.kb_key) for v in project.kb_versions.order_by("number")] == [
        (1, "full", first), (2, "full", project.knowledge_base.kb_key)]


def test_publish_refuses_a_key_that_is_not_this_projects_upload(world, s3):
    s3.objects["workspace-projects/connect/other/kb/x.sqlite.gz"] = b"x"
    r = world["client"].post("/api/workspace-projects/connect/givewell-kb/kb/publish",
                             {"key": "workspace-projects/connect/other/kb/x.sqlite.gz", "sha256": "a" * 64, "base": None},
                             content_type=J)
    assert r.status_code == 422


def test_publish_refuses_an_upload_that_never_arrived(world, s3):
    up = world["client"].post("/api/workspace-projects/connect/givewell-kb/kb/upload", content_type=J).json()
    r = world["client"].post("/api/workspace-projects/connect/givewell-kb/kb/publish",
                             {"key": up["key"], "sha256": "a" * 64, "base": None}, content_type=J)
    assert r.status_code == 422


def test_no_bucket_is_a_clear_503(world, monkeypatch):
    with override_settings(CANOPY_KB_BUCKET="", CANOPY_ATTACHMENTS_BUCKET=""):
        r = world["client"].post("/api/workspace-projects/connect/givewell-kb/kb/upload", content_type=J)
    assert r.status_code == 503


def test_a_viewer_cannot_publish(world, s3):
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=world["connect"], role=WorkspaceMembership.VIEWER)
    vc = Client()
    vc.force_login(viewer)
    r = vc.post("/api/workspace-projects/connect/givewell-kb/kb/upload", content_type=J)
    assert r.status_code in (403, 404)


def test_the_steward_uploads_only_the_originals_the_store_lacks(world, s3):
    prefix = ORIGINALS.format(world["connect"].pk)
    s3.objects[prefix + "a" * 64] = b"already stored"
    files = [{"sha256": "a" * 64, "bytes": 14, "content_type": "application/pdf"},
             {"sha256": "B" * 64, "bytes": 9, "content_type": "application/pdf"},
             {"sha256": "b" * 64, "bytes": 9, "content_type": "application/pdf"}]  # the same file twice

    r = world["client"].post("/api/workspace-projects/connect/givewell-kb/kb/originals", {"files": files},
                             content_type=J)

    assert r.status_code == 200, r.content
    body = r.json()
    assert body["have"] == 1 and [u["sha256"] for u in body["uploads"]] == ["b" * 64]
    up = body["uploads"][0]
    assert up["url"].startswith(f"https://s3.test/{prefix}{'b' * 64}")
    # S3 refuses bytes whose sha256 is not the key's: the checksum is signed in and sent
    op, params = s3.signed[-1]
    assert op == "put_object" and params["ChecksumSHA256"] == up["headers"]["x-amz-checksum-sha256"]
    assert up["headers"]["Content-Type"] == "application/pdf"


def test_an_original_upload_signs_its_checksum(world, monkeypatch):
    """Real boto3, no network: the presigned PUT names x-amz-checksum-sha256 among its
    signed headers, so S3 itself refuses bytes that do not hash to the key."""
    from urllib.parse import parse_qs, urlparse

    for k, v in {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(kb_store, "_stored_originals", lambda project, shas: set())
    with override_settings(CANOPY_KB_BUCKET="kb-bucket"):
        [up] = kb_store.originals_upload(world["project"], [{"sha256": "a" * 64, "bytes": 1}])["uploads"]
    q = parse_qs(urlparse(up["url"]).query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "x-amz-checksum-sha256" in q["X-Amz-SignedHeaders"][0].split(";")


def test_originals_refuse_a_bad_hash_and_a_viewer(world, s3):
    bad = world["client"].post("/api/workspace-projects/connect/givewell-kb/kb/originals",
                               {"files": [{"sha256": "z" * 64, "bytes": 1}]}, content_type=J)
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=world["connect"], role=WorkspaceMembership.VIEWER)
    vc = Client()
    vc.force_login(viewer)
    denied = vc.post("/api/workspace-projects/connect/givewell-kb/kb/originals",
                     {"files": [{"sha256": "a" * 64, "bytes": 1}]}, content_type=J)
    assert bad.status_code == 422 and denied.status_code in (403, 404)


# ---- over HTTP, the way a reader's MCP client calls it ----


@pytest.fixture()
def http():
    inner = project_mcp.http_app(path="/", transport="streamable-http", stateless_http=True, json_response=True)
    app = Starlette(routes=[Mount("/api/mcp/projects", app=ProjectMcpDispatch(inner))])

    def rpc(path, token, method, params=None):
        async def main():
            async with inner.router.lifespan_context(inner):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as c:
                    r = await c.post(path, headers={"Accept": "application/json, text/event-stream",
                                                    "Authorization": f"Bearer {token}"},
                                     json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
                    try:
                        return r.status_code, r.json()
                    except ValueError:
                        return r.status_code, r.text
        return async_to_sync(main)()
    return rpc


def _token(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _connect_reader():
    reader = User.objects.create_user("ace-human", "c@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=reader, workspace=Workspace.objects.get(slug="connect"),
                                       role=WorkspaceMembership.VIEWER)
    return reader


def test_a_reader_lists_and_calls_the_projects_tools(world, s3, tmp_path, http):
    _publish(world, s3, tmp_path)
    tok = _token(_connect_reader())  # a Connect member, nobody in dimagi

    status, listed = http("/api/mcp/projects/connect/givewell-kb/", tok, "tools/list")
    names = [t["name"] for t in listed["result"]["tools"]]
    status2, called = http("/api/mcp/projects/connect/givewell-kb", tok, "tools/call",  # no trailing slash
                           {"name": "grants", "arguments": {"Recipient": "Dimagi"}})

    assert status == 200 and {"search", "get", "whats_new", "grants"} <= set(names)
    assert "list_fleet_tasks" not in names  # only this project's tools, not canopy's
    assert status2 == 200, called
    result = called["result"]["structuredContent"]["result"]
    assert result["rows"][0]["Grant"] == "Water pilot"


def test_a_reader_gets_a_download_of_the_original_under_its_file_name(world, s3, tmp_path, http):
    _publish(world, s3, tmp_path, original=ORIGINAL)
    tok = _token(_connect_reader())
    _, listed = http("/api/mcp/projects/connect/givewell-kb/", tok, "tools/list")
    assert "original" in [t["name"] for t in listed["result"]["tools"]]

    _, missing = http("/api/mcp/projects/connect/givewell-kb/", tok, "tools/call",
                      {"name": "original", "arguments": {"ref": "p:1"}})
    s3.objects[ORIGINALS.format(world["connect"].pk) + ORIGINAL["sha256"]] = b"transcript"
    _, called = http("/api/mcp/projects/connect/givewell-kb/", tok, "tools/call",
                     {"name": "original", "arguments": {"ref": "p:1"}})

    assert "the publisher's copy (may have changed): https://gw/1/transcript.txt" in (
        missing["result"]["structuredContent"]["result"])
    out = called["result"]["structuredContent"]["result"]
    assert "download (valid 15 minutes): https://s3.test/" in out
    op, params = s3.signed[-1]
    assert op == "get_object" and params["Key"].endswith("c" * 64)
    assert params["ResponseContentDisposition"] == "attachment; filename*=UTF-8''Diarrheal%20disease.txt"
    assert params["ResponseContentType"] == "text/plain"


def test_a_stranger_sees_no_tools_and_cannot_call(world, s3, tmp_path, http):
    _publish(world, s3, tmp_path)
    tok = _token(User.objects.create_user("s", "s@else.com", "pw"))

    _, listed = http("/api/mcp/projects/connect/givewell-kb/", tok, "tools/list")
    _, called = http("/api/mcp/projects/connect/givewell-kb/", tok, "tools/call",
                     {"name": "search", "arguments": {"query": "cholera"}})

    assert listed["result"]["tools"] == []
    assert called.get("error") or called["result"].get("isError")


def test_no_token_is_a_401_with_the_sign_in_challenge(world, s3, tmp_path, http):
    status, _ = http("/api/mcp/projects/connect/givewell-kb/", "nope", "tools/list")
    assert status == 401


def test_a_project_that_publishes_nothing_serves_nothing(world, http):
    _, listed = http("/api/mcp/projects/connect/givewell-kb/", _token(world["owner"]), "tools/list")
    assert listed["result"]["tools"] == []


def test_the_url_must_name_a_project(http, world):
    status, _ = http("/api/mcp/projects/", _token(world["owner"]), "tools/list")
    assert status == 404
