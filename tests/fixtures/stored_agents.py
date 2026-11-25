"""What the stored-agent tests share: the agent's slug and the calls they make."""
from __future__ import annotations

import base64
import json
import uuid

from django.test import Client
from django.utils import timezone

from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.testing import grant_step_up

SLUG = "scout"


def _browser(user) -> Client:
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


def _create(client, slug=SLUG, ws="scouts"):
    return client.post("/api/agents/stored", data={"name": "Scout", "slug": slug, "mandate": "Finds trails.",
                                                   "workspace": ws}, content_type="application/json")


def _post(client, url, body=None, **extra):
    return client.post(url, data=json.dumps(body or {}), content_type="application/json", **extra)


def _draft(client, base=1, changes=None, **extra):
    changes = changes or [{"path": "skills/trail/SKILL.md",
                           "content": "---\nname: trail\ndescription: Walk a trail.\n---\nWalk it.\n"}]
    return _post(client, f"/api/agents/{SLUG}/definition/drafts",
                 {"base": base, "changes": changes, "message": "add trail"}, **extra)


def _files(**files):
    return [{"path": p, "content_b64": base64.b64encode(c.encode()).decode(), "executable": False}
            for p, c in files.items()]


def _runner_client(owner, ws, agent=None) -> tuple[Client, Runner]:
    runner = Runner.objects.create(name=f"box-{uuid.uuid4().hex[:6]}", kind=Runner.CLOUD, owner=owner,
                                   workspace=ws, last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    if agent is not None:
        RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=uuid.uuid4().hex, status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}"), runner
