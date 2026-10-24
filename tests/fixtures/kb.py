"""A fake KB bucket and a GiveWell-shaped workspace project, for the knowledge-base tests."""
from __future__ import annotations

import gzip
import hashlib
import io
import json

import pytest
from canopy_kb import build
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.agents import kb_store
from apps.agents.models import Agent, AgentProject
from apps.workspaces.models import Workspace, WorkspaceMembership


class FakeS3:
    """The calls kb_store makes, over a dict."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.signed: list[tuple[str, dict]] = []

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.signed.append((op, Params))
        return f"https://s3.test/{Params['Key']}?signed"

    def get_paginator(self, op):
        # The task role may not list the bucket (no s3:ListBucket in its policy): a
        # listing is a 500 in production, so it is an error here.
        raise AssertionError(f"canopy-web may not list the bucket ({op})")

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = bytes(Body)

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        with open(Filename, "rb") as fh:
            self.objects[Key] = fh.read()


@pytest.fixture()
def s3(monkeypatch, tmp_path):
    fake = FakeS3()
    monkeypatch.setattr(kb_store, "_client", lambda: fake)
    with override_settings(CANOPY_KB_BUCKET="kb-bucket", CANOPY_KB_CACHE=str(tmp_path / "cache")):
        yield fake


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=dimagi)
    WorkspaceMembership.objects.create(user=owner, workspace=dimagi, role=WorkspaceMembership.OWNER)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=owner)
    project = AgentProject.objects.create(agent=eva, ext_id="P12", name="GiveWell Knowledge Base",
                                           workspace=connect, handle="givewell-kb")
    client = Client()
    client.force_login(owner)
    return {"owner": owner, "connect": connect, "eva": eva, "project": project, "client": client}


# ---- a GiveWell-shaped knowledge base, published the way the steward's CLI does ----

J = "application/json"
KB_URL = "/api/workspace-projects/connect/givewell-kb/kb"

ITEMS = [
    {"id": "p:1", "type": "podcast_episode", "title": "Diarrheal disease", "url": "https://gw/1", "date": "2026-07-23",
     "text": "WASHmobile follows up households after a cholera case.", "sha256": "a",
     "card": {"summary": "A podcast on cholera.", "orgs": ["Dimagi"]}},
    {"id": "b:1", "type": "blog_post", "title": "Safe water", "url": "https://gw/2", "date": "2025-10-29",
     "text": "Chlorination pilots in six countries.", "sha256": "b", "meta": {"linked_from": ["https://gw/1"]}},
    {"id": "r:1", "type": "research", "title": "ORS and zinc", "url": "https://gw/3", "date": "2024",
     "text": "# ORS\n\nOral rehydration salts and zinc cut diarrhoea deaths.", "sha256": "c"},
]


def bundle(tmp_path, name, items, grants=None):
    b = tmp_path / name
    (b / "tables").mkdir(parents=True)
    (b / "manifest.json").write_text(json.dumps({
        "name": "givewell", "publisher": "GiveWell",
        "tables": {"grants": {"description": "Every grant.", "date_column": "Date", "amount_column": "Amount",
                              "filter_columns": ["Recipient"]}}}))
    (b / "items.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    (b / "tables" / "grants.jsonl").write_text(json.dumps(grants or {
        "Grant": "Water pilot", "Recipient": "Dimagi Inc", "Amount": 1069324, "Date": "2025-12-03"}))
    return b


def head(world) -> int | None:
    """The version the project serves: what a steward's next publish is made from."""
    project = AgentProject.objects.get(pk=world["project"].pk)
    return project.kb_version.number if project.kb_version_id else None


def full(world, s3, tmp_path, bundle, *, previous=None, base="head", curation="", expect=200):
    """A publish the way the steward's refresh makes one: build the whole file (from the
    `previous` kb.sqlite, so its history carries on), upload it, publish it made from
    `base` (default: the head) with the `curation` state it was built from."""
    db = tmp_path / f"{bundle.name}.sqlite"
    build(bundle, db, previous=previous)
    body = gzip.compress(db.read_bytes())
    c = world["client"]
    up = c.post(f"{KB_URL}/upload", content_type=J).json()
    s3.objects[up["key"]] = body
    r = c.post(f"{KB_URL}/publish", {"key": up["key"], "sha256": hashlib.sha256(body).hexdigest(),
                                     "base": head(world) if base == "head" else base, "curation": curation},
               content_type=J)
    assert r.status_code == expect, r.content
    return r


V2 = [dict(ITEMS[0], text=ITEMS[0]["text"] + " Zinc too.", sha256="a2"),  # changed
      ITEMS[1],                                                           # same
      {"id": "n:1", "type": "blog_post", "title": "New grant", "url": "https://gw/9", "date": "2026-10-20",
       "text": "A new grant for in-line chlorination.", "sha256": "n"}]   # r:1 removed, n:1 new
