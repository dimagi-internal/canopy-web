"""Opting in to coaching tells the person's OWN runner to ship their sessions in full.

`GET /runners/{id}/streams` flags (`ship_full`) the runner-discovered sessions of a
box whose owner has a live `SessionReviewGrant`, and lists them even once they are
no longer active, so their whole history is sent. Never a sensitive session, never
from a sensitive-data box, never an agent's own work, never another person's box,
never a repo the owner has not opted in to; revoking the last grant clears it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session, SessionReviewGrant
from apps.harness.models import ProjectOptIn, Runner, RunnerFlag
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    bo = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=lili)
    for u in (lili, bo):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
        ProjectOptIn.objects.create(user=u, projects=["canopy-web"])
    trellis = Agent.objects.create(slug="trellis", name="Trellis", workspace=ws, owner=lili)
    laptop = Runner.objects.create(name="lili-laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=lili)
    bos_box = Runner.objects.create(name="bo-laptop", workspace=ws, location=Runner.LOCAL,
                                    status=Runner.ONLINE, owner=bo)
    return {"ws": ws, "lili": lili, "bo": bo, "trellis": trellis, "laptop": laptop, "bos_box": bos_box}


def _session(w, runner, key, *, status=Session.ACTIVE, project="canopy-web", **kw):
    s = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_RUNNER, project=project,
                               title=key, status=status, **kw)
    RunnerBinding.objects.create(session=s, runner=runner, session_key=key)
    return s


def _streams(user, runner) -> dict:
    c = Client()
    c.force_login(user)
    body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
    return {s["session_key"]: s for s in body["streams"]}


def _coach(w):
    return SessionReviewGrant.objects.create(user=w["lili"], agent=w["trellis"])


def test_uncoached_box_ships_active_sessions_only_and_flags_none(w):
    _session(w, w["laptop"], "live")
    _session(w, w["laptop"], "done", status=Session.ARCHIVED)
    got = _streams(w["lili"], w["laptop"])
    assert sorted(got) == ["live"]
    assert got["live"]["ship_full"] is False


def test_coaching_flags_the_owners_sessions_and_lists_retired_ones(w):
    _coach(w)
    _session(w, w["laptop"], "live")
    _session(w, w["laptop"], "done", status=Session.ARCHIVED)
    got = _streams(w["lili"], w["laptop"])
    assert sorted(got) == ["done", "live"]
    assert got["live"]["ship_full"] is True and got["live"]["archived"] is False
    assert got["done"]["ship_full"] is True and got["done"]["archived"] is True


def test_revoking_the_last_grant_stops_it_on_the_next_poll(w):
    grant = _coach(w)
    _session(w, w["laptop"], "done", status=Session.ARCHIVED)
    assert "done" in _streams(w["lili"], w["laptop"])
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])
    assert _streams(w["lili"], w["laptop"]) == {}


def test_a_sensitive_session_is_never_flagged(w):
    _coach(w)
    _session(w, w["laptop"], "sealed", status=Session.ARCHIVED,
             metadata={"runner_requirements": ["sensitive_data"]})
    live = _session(w, w["laptop"], "sealed-live", metadata={"runner_requirements": ["sensitive_data"]})
    got = _streams(w["lili"], w["laptop"])
    assert "sealed" not in got
    assert got[live.runner_binding.session_key]["ship_full"] is False


def test_a_sensitive_data_box_ships_nothing_ever(w):
    _coach(w)
    RunnerFlag.objects.create(runner=w["laptop"], flag="sensitive_data", declared_by=w["lili"])
    _session(w, w["laptop"], "live")
    _session(w, w["laptop"], "done", status=Session.ARCHIVED)
    assert _streams(w["lili"], w["laptop"]) == {}


def test_another_persons_box_is_not_flagged_by_her_coaching(w):
    _coach(w)  # lili is coached; bo is not
    _session(w, w["bos_box"], "bo-live")
    _session(w, w["bos_box"], "bo-done", status=Session.ARCHIVED)
    got = _streams(w["bo"], w["bos_box"])
    assert sorted(got) == ["bo-live"]
    assert got["bo-live"]["ship_full"] is False


def test_an_agents_own_sessions_are_never_flagged(w):
    _coach(w)
    _session(w, w["laptop"], "agent-done", status=Session.ARCHIVED, agent=w["trellis"], project="")
    agent_live = _session(w, w["laptop"], "agent-live", agent=w["trellis"], project="")
    got = _streams(w["lili"], w["laptop"])
    assert "agent-done" not in got
    assert got[agent_live.runner_binding.session_key]["ship_full"] is False


def test_a_repo_she_has_not_opted_in_to_is_never_listed(w):
    _coach(w)
    _session(w, w["laptop"], "private-repo", status=Session.ARCHIVED, project="diary")
    _session(w, w["laptop"], "private-live", project="diary")
    assert _streams(w["lili"], w["laptop"]) == {}
