"""`agent_token_expiring`: an agent's own canopy token near expiry with nothing to
rotate it is raised to the agent's admins."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents import alerts_agent_token as src
from apps.agents.models import Agent
from apps.attention import alerts
from apps.attention.models import Alert
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def w(monkeypatch):
    jj, ed, bot = _user("jj"), _user("ed"), _user("evabot")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=ed, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=bot, role=WorkspaceMembership.EDITOR)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj, user=bot)
    monkeypatch.setattr(alerts, "SOURCES", ["apps.agents.alerts_agent_token"])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    return {"jj": jj, "ed": ed, "bot": bot, "eva": eva}


def _token(user, days):
    _, tok = PersonalToken.create_for_user(user=user, label="eva agent", ttl_days=days)
    return tok


def test_a_token_about_to_expire_with_no_rotation_is_raised_to_admins(w):
    _token(w["bot"], 10)
    alerts.tick(timezone.now())
    alert = Alert.objects.get(kind=src.KIND)
    assert "automatic rotation under Settings" in alert.body and "1Password" not in alert.body
    assert [a.id for a in src.actions(alert, w["jj"])] == ["remint", "dismiss"]
    assert src.actions(alert, w["ed"]) == []  # not an admin: cannot mint it


def test_nothing_is_raised_when_it_is_far_off_rotating_or_never_expiring(w):
    now = timezone.now()
    _token(w["bot"], 10)
    later = _token(w["bot"], 100)
    assert src.reconcile(now) == []           # a re-minted token lives past the window
    later.delete()
    Agent.objects.filter(pk=w["eva"].pk).update(pat_rotation_enabled=True)
    assert src.reconcile(now) == []           # rotation replaces it
    Agent.objects.filter(pk=w["eva"].pk).update(pat_rotation_enabled=False)
    _token(w["bot"], 0)
    assert src.reconcile(now) == []           # an old never-expiring token lapses never


def test_a_reminted_token_resolves_it(w):
    _token(w["bot"], 10)
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).status == Alert.OPEN
    _token(w["bot"], 180)
    alerts.tick(timezone.now() + timedelta(minutes=1))
    assert Alert.objects.get(kind=src.KIND).status != Alert.OPEN
