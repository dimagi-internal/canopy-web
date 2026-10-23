"""A curator's working cache is saved on canopy-web, so a refresh can run on any machine.

What a refresh keeps between runs (fetched bytes with their ETags, extracted text, OCR,
catalog cards) is stored by sha256: a file the originals already hold is not stored
twice, and a save never replaces a cache another machine saved since.
"""
from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.workspaces.models import WorkspaceMembership
from tests.fixtures import kb as kb_fixtures

pytestmark = pytest.mark.django_db

s3, world = kb_fixtures.s3, kb_fixtures.world

J = "application/json"
KB_URL = "/api/workspace-projects/connect/givewell-kb/kb"
ORIGINALS = "workspace-projects/{ws}/givewell-kb/originals/"
WORKDIR = "workspace-projects/{ws}/givewell-kb/workdir/"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _put_signed(s3, ups, bodies):
    for u in ups:
        key = u["url"].split("https://s3.test/", 1)[1].split("?", 1)[0]
        s3.objects[key] = bodies[u["sha256"]]


def _save(world, s3, files: dict[str, bytes], base=None):
    """The CLI's save, by hand: ask what is stored, upload the rest gzipped, upload the
    manifest, point the project at it."""
    c = world["client"]
    shas = {path: _sha(body) for path, body in files.items()}
    have = c.post(f"{KB_URL}/workdir/have", {"shas": sorted(set(shas.values()))}, content_type=J)
    assert have.status_code == 200, have.content
    in_originals = set(have.json()["originals"])
    manifest = {"format": 1, "files": [
        {"path": p, "sha256": h, "bytes": len(files[p]), "store": "originals" if h in in_originals else "workdir"}
        for p, h in sorted(shas.items())]}
    mbody = json.dumps(manifest, sort_keys=True).encode()
    gz = {h: gzip.compress(files[p]) for p, h in shas.items() if h not in in_originals}
    gz[_sha(mbody)] = gzip.compress(mbody)
    blobs = [{"sha256": h, "gz_sha256": _sha(g), "bytes": len(g)} for h, g in gz.items()]
    ups = c.post(f"{KB_URL}/workdir/blobs", {"blobs": blobs}, content_type=J)
    assert ups.status_code == 200, ups.content
    _put_signed(s3, ups.json()["uploads"], gz)
    r = c.put(f"{KB_URL}/workdir", {"manifest": _sha(mbody), "base": base, "files": len(files),
                                    "bytes": sum(len(b) for b in files.values())}, content_type=J)
    return r, _sha(mbody), ups.json()


def test_a_saved_cache_stores_only_what_the_originals_lack_and_restores(world, s3):
    ws = world["connect"].pk
    pdf = b"%PDF-1.7 a grant report"
    s3.objects[ORIGINALS.format(ws=ws) + _sha(pdf)] = pdf  # `push --originals` stored it
    files = {"raw/http/ab12": pdf, "raw/http.jsonl": b'{"url": "https://gw/1", "etag": "\\"x\\""}\n',
             "cards.json": b'{"p:1": {"summary": "A podcast."}}'}
    r, manifest, ups = _save(world, s3, files)
    assert r.status_code == 200, r.content
    assert r.json()["manifest"] == manifest and r.json()["files"] == 3
    assert r.json()["saved_by"] == "jj@dimagi.com"
    assert {u["sha256"] for u in ups["uploads"]} == {_sha(files["raw/http.jsonl"]), _sha(files["cards.json"]),
                                                    manifest}  # never the PDF again
    got = world["client"].get(f"{KB_URL}/workdir").json()
    assert got["manifest"] == manifest

    links = world["client"].post(f"{KB_URL}/workdir/links", {"files": [
        {"sha256": manifest}, {"sha256": _sha(pdf), "store": "originals"}]}, content_type=J).json()["links"]
    keys = [u["url"].split("https://s3.test/", 1)[1].split("?", 1)[0] for u in links]
    assert keys == [WORKDIR.format(ws=ws) + manifest, ORIGINALS.format(ws=ws) + _sha(pdf)]
    listed = json.loads(gzip.decompress(s3.objects[keys[0]]))
    assert {f["path"]: f["store"] for f in listed["files"]} == {
        "raw/http/ab12": "originals", "raw/http.jsonl": "workdir", "cards.json": "workdir"}

    # a second save of the same files uploads nothing but finds it all stored
    r2, again, ups2 = _save(world, s3, files, base=manifest)
    assert r2.status_code == 200 and again == manifest and ups2["uploads"] == []


def test_a_save_never_replaces_a_cache_saved_since(world, s3):
    first, m1, _ = _save(world, s3, {"cards.json": b"{}"})
    assert first.status_code == 200
    stale, _, _ = _save(world, s3, {"cards.json": b'{"older": 1}'})  # restored nothing: base None
    assert stale.status_code == 409, stale.content
    assert world["client"].get(f"{KB_URL}/workdir").json()["manifest"] == m1
    ok, m2, _ = _save(world, s3, {"cards.json": b'{"newer": 1}'}, base=m1)
    assert ok.status_code == 200 and world["client"].get(f"{KB_URL}/workdir").json()["manifest"] == m2


def test_a_manifest_must_be_uploaded_and_shas_must_be_shas(world, s3):
    c = world["client"]
    r = c.put(f"{KB_URL}/workdir", {"manifest": "0" * 64, "files": 1, "bytes": 1}, content_type=J)
    assert r.status_code == 422 and b"no working-cache manifest" in r.content
    r = c.post(f"{KB_URL}/workdir/have", {"shas": ["not-a-sha"]}, content_type=J)
    assert r.status_code == 422


def test_only_the_steward_reads_the_cache(world, s3):
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=world["connect"], role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(viewer)
    assert c.get(f"{KB_URL}/workdir").status_code in (403, 404)
    assert c.post(f"{KB_URL}/workdir/links", {"files": [{"sha256": "0" * 64}]},
                  content_type=J).status_code in (403, 404)
