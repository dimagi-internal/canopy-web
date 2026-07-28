"""POST /api/harness/turns/ may name the runner (spec 2026-07-27).

Pinning bypasses assignments and source rules — never the tenant gate, and never
a runner the caller cannot see. That last part is the whole security surface of
this field, so it is tested from the outside via the API.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    runner = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    client.force_login(jj)
    return {"client": client, "user": jj, "agent": agent, "runner": runner}


def _post(client, body):
    return client.post("/api/harness/turns/", data=body, content_type="application/json")


def test_runner_id_pins_the_turn(setup):
    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k1",
        "runner_id": str(setup["runner"].id),
    })

    assert res.status_code == 201, res.content
    assert Turn.objects.get(idempotency_key="k1").pinned_runner_id == setup["runner"].id


def test_omitting_runner_id_leaves_the_turn_unpinned(setup):
    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k2",
    })

    assert res.status_code == 201, res.content
    assert Turn.objects.get(idempotency_key="k2").pinned_runner is None


def test_a_runner_the_caller_cannot_see_is_rejected(setup):
    outsider = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    theirs = Runner.objects.create(
        name="mal-box", kind=Runner.CLOUD, paired_by=outsider, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )

    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k3",
        "runner_id": str(theirs.id),
    })

    assert res.status_code == 422
    # Rejected, not silently enqueued unpinned: a caller that asked for a specific
    # box and got "anywhere" would look like it worked.
    assert not Turn.objects.filter(idempotency_key="k3").exists()


def test_a_retired_runner_is_rejected(setup):
    """Pinning to a retired runner strands the turn forever — nothing can claim it."""
    Runner.objects.filter(pk=setup["runner"].pk).update(status=Runner.RETIRED)

    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k4",
        "runner_id": str(setup["runner"].id),
    })

    assert res.status_code == 422


def test_an_unknown_runner_id_is_rejected(setup):
    res = _post(setup["client"], {
        "agent_slug": "echo", "origin": "api", "idempotency_key": "k5",
        "runner_id": "00000000-0000-0000-0000-000000000000",
    })

    assert res.status_code == 422
