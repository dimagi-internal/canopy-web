"""Runner installers and updates: the deployed runner version's build, by presigned URL.

The bytes sit in the private bucket under runner-releases/<sha>/; canopy-web
decides who may have them (a signed-in person, or a runner with its own token)
and which version is current (the one its own image names).
"""
from __future__ import annotations

import pytest
from canopy_sdk.jose import make_dpop_proof
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.harness import device_keys, runner_auth
from apps.harness import runner_releases as rel
from apps.harness.models import Runner, RunnerDeviceKey
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SHA = "a" * 40
MANIFEST = {
    "sha": SHA, "committed_at": 1760000000, "built_at": "2026-10-19T12:00:00Z",
    "artifacts": {
        "macos-arm64": {
            "installer": {"file": "Canopy-Runner-macos-arm64.dmg", "sha256": "d" * 64, "size": 101},
            "update": {"file": "Canopy-Runner-macos-arm64.app.zip", "sha256": "e" * 64, "size": 99},
            "signed": True, "notarized": True},
        "windows-x86_64": {
            "installer": {"file": "Canopy-Runner-Setup-windows-x86_64.exe", "sha256": "f" * 64, "size": 80},
            "update": {"file": "Canopy-Runner-Setup-windows-x86_64.exe", "sha256": "f" * 64, "size": 80},
            "signed": False, "notarized": False},
    },
}


class FakeS3:
    def __init__(self, manifest=MANIFEST):
        self.manifest = manifest
        self.reads = 0

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3's names
        import io
        import json

        self.reads += 1
        assert Bucket == "releases-bucket"
        if self.manifest is None or Key != f"runner-releases/{SHA}/manifest.json":
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(json.dumps(self.manifest).encode())}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        assert op == "get_object" and ExpiresIn == 900
        return (f"https://s3.example/{Params['Bucket']}/{Params['Key']}"
                f"?cd={Params['ResponseContentDisposition']}")


@pytest.fixture
def s3(settings, monkeypatch):
    cache.clear()
    settings.CANOPY_RUNNER_RELEASES_BUCKET = "releases-bucket"
    settings.RUNNER_RELEASES_PREFIX = "runner-releases/"
    settings.RUNNER_CODE_SHA = SHA
    fake = FakeS3()
    monkeypatch.setattr(rel, "_client", lambda: fake)
    yield fake
    cache.clear()


@pytest.fixture
def person():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(jj)
    return {"user": jj, "ws": ws, "client": c}


# ---- the updater's read -------------------------------------------------------------

def test_current_is_the_deployed_versions_update_with_a_presigned_url(s3, person):
    res = person["client"].get("/api/harness/runner-releases/current?platform=macos-arm64")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["sha"] == SHA and body["platform"] == "macos-arm64"
    assert body["file"] == "Canopy-Runner-macos-arm64.app.zip"
    assert body["sha256"] == "e" * 64 and body["size"] == 99
    assert body["signed"] is True and body["notarized"] is True
    assert body["url"].startswith(f"https://s3.example/releases-bucket/runner-releases/{SHA}/"
                                  "Canopy-Runner-macos-arm64.app.zip")
    assert 'attachment; filename="Canopy-Runner-macos-arm64.app.zip"' in body["url"]


def test_current_404s_when_nothing_is_built_or_nothing_is_deployed(s3, person, settings):
    c = person["client"]
    assert c.get("/api/harness/runner-releases/current?platform=macos-x86_64").status_code == 404
    assert c.get("/api/harness/runner-releases/current?platform=beos").status_code == 422
    settings.RUNNER_CODE_SHA = "b" * 40
    assert c.get("/api/harness/runner-releases/current?platform=macos-arm64").status_code == 404
    settings.RUNNER_CODE_SHA = ""
    res = c.get("/api/harness/runner-releases/current?platform=macos-arm64")
    assert res.status_code == 404 and "no deployed runner version" in res.content.decode()


def test_current_503s_with_no_bucket(s3, person, settings):
    settings.CANOPY_RUNNER_RELEASES_BUCKET = ""
    assert person["client"].get("/api/harness/runner-releases/current?platform=macos-arm64").status_code == 503


def test_the_manifest_is_cached_hit_or_miss(s3, person):
    for _ in range(3):
        person["client"].get("/api/harness/runner-releases/downloads")
    assert s3.reads == 1
    s3.manifest = None
    cache.clear()
    for _ in range(3):
        person["client"].get("/api/harness/runner-releases/downloads")
    assert s3.reads == 2


def test_a_runner_reaches_current_with_its_own_token_and_not_downloads(s3, person):
    rid = "11111111-2222-3333-4444-555555555555"
    assert runner_auth.reaches("GET", "/api/harness/runner-releases/current", rid)
    assert runner_auth.reaches("GET", "/canopy/api/harness/runner-releases/current", rid)
    assert not runner_auth.reaches("GET", "/api/harness/runner-releases/downloads", rid)
    assert not runner_auth.reaches("POST", "/api/harness/runner-releases/current", rid)

    # End to end: a keyless box's token.
    key = ec.generate_private_key(ec.SECP256R1())
    from canopy_sdk.keys import public_jwk

    jwk = device_keys.normalize_public_jwk(public_jwk(key))
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=person["user"], workspace=person["ws"])
    RunnerDeviceKey.objects.create(runner=runner, kind="software", public_jwk=jwk,
                                   jkt=device_keys.thumbprint(jwk), status=RunnerDeviceKey.ACTIVE)
    sess = Client().post("/api/runner-sessions/", data={"runner": str(runner.pk)}, content_type="application/json",
                         HTTP_DPOP=make_dpop_proof(key, "POST", "http://testserver/api/runner-sessions/"))
    token = sess.json()["access_token"]
    box = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert box.get("/api/harness/runner-releases/current?platform=windows-x86_64").status_code == 200
    assert box.get("/api/harness/runner-releases/downloads").status_code == 403


# ---- the download buttons -----------------------------------------------------------

def test_downloads_lists_each_built_installer(s3, person):
    res = person["client"].get("/api/harness/runner-releases/downloads")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["sha"] == SHA
    assert body["downloads"] == [
        {"platform": "macos-arm64", "label": "macOS (Apple silicon)", "file": "Canopy-Runner-macos-arm64.dmg",
         "size": 101, "signed": True, "notarized": True, "href": "/download/runner/macos-arm64"},
        {"platform": "windows-x86_64", "label": "Windows", "file": "Canopy-Runner-Setup-windows-x86_64.exe",
         "size": 80, "signed": False, "notarized": False, "href": "/download/runner/windows-x86_64"},
    ]


def test_downloads_is_empty_not_an_error_when_none_is_built(s3, person, settings):
    s3.manifest = None
    assert person["client"].get("/api/harness/runner-releases/downloads").json()["downloads"] == []
    settings.RUNNER_CODE_SHA = ""
    assert person["client"].get("/api/harness/runner-releases/downloads").json() == {"sha": "", "downloads": []}


def test_a_manifest_naming_an_unsafe_file_is_ignored(s3, person):
    s3.manifest = {"artifacts": {"macos-arm64": {"installer": {"file": "../../sessions/x", "size": 1}}}}
    assert person["client"].get("/api/harness/runner-releases/downloads").json()["downloads"] == []


def test_downloads_needs_a_signed_in_person(s3, settings):
    settings.REQUIRE_AUTH = True
    assert Client().get("/api/harness/runner-releases/downloads").status_code == 401


# ---- /download/runner/<platform> ----------------------------------------------------

def test_the_download_view_redirects_a_signed_in_person_to_the_installer(s3, person):
    res = person["client"].get("/download/runner/macos-arm64")
    assert res.status_code == 302
    assert res["Location"].startswith(
        f"https://s3.example/releases-bucket/runner-releases/{SHA}/Canopy-Runner-macos-arm64.dmg")
    assert res["Cache-Control"] == "no-store"


def test_the_download_view_sends_a_signed_out_visitor_to_sign_in(s3, settings):
    for require in (True, False):
        settings.REQUIRE_AUTH = require
        res = Client().get("/download/runner/windows-x86_64")
        assert res.status_code == 302
        assert res["Location"].startswith(settings.LOGIN_URL)
        from urllib.parse import parse_qs, urlsplit

        assert parse_qs(urlsplit(res["Location"]).query)["next"] == ["/download/runner/windows-x86_64"]


def test_the_download_view_says_when_nothing_is_built(s3, person, settings):
    res = person["client"].get("/download/runner/macos-x86_64")
    assert res.status_code == 404 and b"No download for macOS (Intel) yet" in res.content
    assert person["client"].get("/download/runner/amiga").status_code == 404
    settings.CANOPY_RUNNER_RELEASES_BUCKET = ""
    assert person["client"].get("/download/runner/macos-arm64").status_code == 404
