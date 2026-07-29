"""PATCH /api/harness/runners/{id} — update a paired runner's capabilities.

The only prior way to change a capability was to re-pair, which mints a new
runner and orphans the old one's RunnerBindings. This lets a runner opt into
driving new agents in place.

`projects` is NO LONGER one of them (spec 2026-07-28). It is reported by the
runner on every heartbeat from what the box actually has, so a hand-written value
is a ghost edit — 200 to the caller, overwritten seconds later, and a dispatch
into a hole. It is refused here and preserved across writes that omit it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, ws, **kw):
    return Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=pairer, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"agents": ["echo"]}, **kw,
    )


def _patch(client, runner_id, caps):
    return client.patch(
        f"/api/harness/runners/{runner_id}",
        {"capabilities": caps},
        content_type="application/json",
    )


def test_owner_adds_agents_to_a_paired_runner():
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    c = Client()
    c.force_login(jj)

    resp = _patch(c, runner.id, {"agents": ["echo", "ada"], "sessions": True})

    assert resp.status_code == 200, resp.content
    runner.refresh_from_db()
    assert runner.agent_slugs() == ["echo", "ada"]
    assert runner.session_capable() is True


def test_writing_projects_by_hand_is_refused():
    """The retired half. A 200 here would be a lie the caller acts on: the next
    heartbeat replaces the value, so the repo they think they just declared is
    still undispatchable. The message names the real fix instead."""
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    c = Client()
    c.force_login(jj)

    resp = _patch(c, runner.id, {"agents": ["echo"], "projects": ["canopy-web"]})

    assert resp.status_code == 422
    runner.refresh_from_db()
    assert runner.project_names() == []
    assert runner.agent_slugs() == ["echo"]  # the whole write is rejected, not half of it


def test_replacement_is_wholesale_for_the_keys_patch_owns():
    """Sending {} clears the capabilities the caller owns — no accidental merge
    that leaves stale entries."""
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    c = Client()
    c.force_login(jj)

    _patch(c, runner.id, {"sessions": True})
    runner.refresh_from_db()
    assert runner.agent_slugs() == []  # the prior agents entry is gone
    assert runner.session_capable() is True


def test_a_capabilities_write_does_not_drop_the_reported_projects():
    """`projects` belongs to the runner now, so a wholesale write of the OTHER
    keys must not take it out as a side effect — that would unroute every repo
    turn until the next heartbeat, for a caller who never mentioned projects."""
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws, )
    runner.capabilities = {"agents": ["echo"], "projects": ["canopy-web"]}
    runner.save(update_fields=["capabilities"])
    c = Client()
    c.force_login(jj)

    _patch(c, runner.id, {"agents": ["ada"]})

    runner.refresh_from_db()
    assert runner.agent_slugs() == ["ada"]
    assert runner.project_names() == ["canopy-web"]


def test_a_non_owner_cannot_touch_another_users_runner():
    """404 not 403 — _runner_or_404 must not leak that the runner exists."""
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)

    mallory = _user("mallory")
    _ws("mallory-space", mallory)
    c = Client()
    c.force_login(mallory)

    resp = _patch(c, runner.id, {"projects": ["canopy-web"]})
    assert resp.status_code == 404
    runner.refresh_from_db()
    assert runner.project_names() == []  # untouched
