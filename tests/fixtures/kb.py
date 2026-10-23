"""A fake KB bucket and a GiveWell-shaped workspace project, for the knowledge-base tests."""
from __future__ import annotations

import gzip
import hashlib
import io
import json

import pytest
from canopy_kb import build, records
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
                                          scope="workspace", owner_workspace=connect, handle="givewell-kb")
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


def full(world, s3, tmp_path, bundle):
    """The first publish: the whole file, with its state."""
    db = tmp_path / f"{bundle.name}.sqlite"
    build(bundle, db)
    body = gzip.compress(db.read_bytes())
    c = world["client"]
    up = c.post(f"{KB_URL}/upload", content_type=J).json()
    s3.objects[up["key"]] = body
    r = c.post(f"{KB_URL}/publish", {"key": up["key"], "sha256": hashlib.sha256(body).hexdigest(),
                                     "state": records.state(bundle)}, content_type=J)
    assert r.status_code == 200, r.content
    return r


def delta(world, s3, bundle, client=None):
    """Plan, upload what the plan asks for, commit; returns (plan, commit response, bytes sent)."""
    c = client or world["client"]
    state = records.state(bundle)
    plan = c.post(f"{KB_URL}/plan", {"state": state}, content_type=J)
    assert plan.status_code == 200, plan.content
    p = plan.json()
    if p["unchanged"]:
        return p, None, 0
    recs = {iid: rec for iid, _, rec in records.records(bundle)}
    parts = records.parts(bundle)
    texts = dict(records.texts(bundle, set(p["upload_docs"])))
    objs = [recs[i] for i in p["upload_docs"]] + list(texts.values()) + [parts[n] for n in p["upload_parts"]]
    blobs = [{"sha256": records.sha(o), "gz_sha256": hashlib.sha256(records.encode(o)).hexdigest(),
              "bytes": len(records.encode(o))} for o in objs]
    sent = 0
    if blobs:
        ups = c.post(f"{KB_URL}/blobs", {"blobs": blobs}, content_type=J)
        assert ups.status_code == 200, ups.content
        by = {records.sha(o): o for o in objs}
        for u in ups.json()["uploads"]:
            key = u["url"].split("https://s3.test/", 1)[1].split("?", 1)[0]
            s3.objects[key] = records.encode(by[u["sha256"]])
            sent += len(s3.objects[key])
    r = c.post(f"{KB_URL}/versions", {"state": state, "base": p["base"], "uploaded_bytes": sent}, content_type=J)
    return p, r, sent


V2 = [dict(ITEMS[0], text=ITEMS[0]["text"] + " Zinc too.", sha256="a2"),  # changed
      ITEMS[1],                                                           # same
      {"id": "n:1", "type": "blog_post", "title": "New grant", "url": "https://gw/9", "date": "2026-10-20",
       "text": "A new grant for in-line chlorination.", "sha256": "n"}]   # r:1 removed, n:1 new
