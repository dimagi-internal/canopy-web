"""A fake KB bucket and a GiveWell-shaped workspace project, for the knowledge-base tests."""
from __future__ import annotations

import io

import pytest
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
