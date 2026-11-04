"""An agent's skill catalog is filled from what its runners report, not only from the
agent publishing it (Trellis ran 157 turns with an empty catalog), and an agent left
with none is raised as an alert."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents import alerts_skill_catalog as src
from apps.agents import skill_catalog
from apps.agents.models import Agent, AgentSkill
from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness.models import Runner, RunnerSlashCommands
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _cmd(name, description="d", source="plugin", args=""):
    return {"name": name, "description": description, "args_hint": args, "source": source}


@pytest.fixture
def w(monkeypatch):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    stranger = get_user_model().objects.create_user(username="st", email="st@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    trellis = Agent.objects.create(slug="trellis", name="Trellis", workspace=ws, owner=jj,
                                   repo_url="https://github.com/dimagi-internal/trellis")
    box = Runner.objects.create(name="cloud", kind=Runner.CLOUD, owner=jj, workspace=ws)
    other = Runner.objects.create(name="theirs", kind=Runner.EMDASH, owner=stranger, workspace=ws)
    monkeypatch.setattr(alerts, "SOURCES", ["apps.agents.alerts_skill_catalog"])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    return {"jj": jj, "ws": ws, "trellis": trellis, "box": box, "other": other}


def _report(runner, commands):
    RunnerSlashCommands.objects.update_or_create(runner=runner, defaults={"commands": commands})


def test_an_empty_catalog_is_filled_from_the_runner_that_holds_the_agent(w):
    _report(w["box"], [_cmd("trellis:turn", "do a turn"), _cmd("trellis:coach", "coach", args="person"),
                       _cmd("ace:run"), _cmd("trellis:user-thing", source="user"), _cmd("compact")])
    assert skill_catalog.refresh(w["trellis"]) == 2
    rows = {s.name: s for s in w["trellis"].skills.all()}
    assert set(rows) == {"turn", "coach"}
    assert rows["coach"].args_hint == "person" and rows["turn"].source == "runner"


def test_a_skill_a_session_has_announced_still_counts(w):
    """A box marks a plugin's skill `acp` once a session has announced it; most of a live agent's skills are."""
    _report(w["box"], [_cmd("trellis:coach", source="acp"), _cmd("trellis:shipping"),
                          _cmd("compact", source="acp"), _cmd("ace:run", source="acp")])
    assert skill_catalog.refresh(w["trellis"]) == 2
    assert set(w["trellis"].skills.values_list("name", flat=True)) == {"coach", "shipping"}


def test_it_follows_the_repo_and_keeps_what_a_row_already_says(w):
    _report(w["box"], [_cmd("trellis:turn"), _cmd("trellis:old")])
    skill_catalog.refresh(w["trellis"])
    AgentSkill.objects.filter(agent=w["trellis"], name="turn").update(launchable=True, improvement_note="n")
    _report(w["box"], [_cmd("trellis:turn", "new words"), _cmd("trellis:added")])
    assert skill_catalog.refresh(w["trellis"]) == 2
    rows = {s.name: s for s in w["trellis"].skills.all()}
    assert set(rows) == {"turn", "added"}
    assert rows["turn"].launchable and rows["turn"].improvement_note == "n"
    assert rows["turn"].description == "new words"


def test_a_published_catalog_is_never_touched(w):
    AgentSkill.objects.create(agent=w["trellis"], name="mine", description="published")
    _report(w["box"], [_cmd("trellis:turn")])
    assert skill_catalog.refresh(w["trellis"]) is None
    assert [s.name for s in w["trellis"].skills.all()] == ["mine"]


def _catalog(user, agent):
    c = Client()
    c.force_login(user)
    resp = c.get(f"/api/agents/{agent.slug}/skills/")
    assert resp.status_code == 200, resp.content
    return {s["name"]: s for s in resp.json()}


def test_an_internal_skill_stays_in_the_catalog_marked(w):
    _report(w["box"], [_cmd("trellis:turn"), {**_cmd("trellis:shipping"), "internal": True}])
    assert skill_catalog.refresh(w["trellis"]) == 2
    rows = _catalog(w["jj"], w["trellis"])
    assert set(rows) == {"turn", "shipping"}
    assert rows["shipping"]["internal"] is True and rows["turn"]["internal"] is False


def test_the_mark_follows_the_report_and_a_report_without_it_means_not_internal(w):
    _report(w["box"], [{**_cmd("trellis:shipping"), "internal": True}])
    skill_catalog.refresh(w["trellis"])
    _report(w["box"], [_cmd("trellis:shipping")])  # an older runner's report
    skill_catalog.refresh(w["trellis"])
    assert _catalog(w["jj"], w["trellis"])["shipping"]["internal"] is False


def test_a_published_catalog_keeps_its_rows_and_takes_only_the_mark(w):
    # A publish cannot say a skill is internal, so only a report can mark it. ACE's
    # catalog is published and goes stale: rows the report does not name are left be.
    AgentSkill.objects.create(agent=w["trellis"], name="shipping", description="published words")
    AgentSkill.objects.create(agent=w["trellis"], name="turn", launchable=True)
    AgentSkill.objects.create(agent=w["trellis"], name="gone-from-the-repo")
    _report(w["box"], [{**_cmd("trellis:shipping", "reported words"), "internal": True},
                       _cmd("trellis:turn"), _cmd("trellis:not-published")])
    assert skill_catalog.refresh(w["trellis"]) is None
    rows = _catalog(w["jj"], w["trellis"])
    assert set(rows) == {"shipping", "turn", "gone-from-the-repo"}
    assert rows["shipping"]["internal"] is True and rows["shipping"]["description"] == "published words"
    assert rows["turn"]["internal"] is False and rows["turn"]["launchable"] is True


def test_a_box_that_may_not_hold_the_agent_is_not_believed(w):
    _report(w["other"], [_cmd("trellis:turn")])  # its owner is no admin of trellis
    assert skill_catalog.refresh(w["trellis"]) is None
    assert w["trellis"].skills.count() == 0


def test_the_repo_name_is_a_namespace_too(w):
    Agent.objects.filter(pk=w["trellis"].pk).update(slug="alafia-test",
                                                    repo_url="https://github.com/fsowd/alafiacomm-assistant")
    agent = Agent.objects.get(pk=w["trellis"].pk)
    _report(w["box"], [_cmd("alafiacomm-assistant:ask")])
    assert skill_catalog.refresh(agent) == 1


def test_the_tick_job_fills_every_agent_and_never_raises(w):
    _report(w["box"], [_cmd("trellis:turn")])
    assert skill_catalog.refresh_all() == {"trellis": 1}


def test_the_job_is_on_the_clock():
    from apps.common import ticks
    assert "agents.skill_catalog" in ticks.jobs()


def test_an_agent_with_no_catalog_after_a_day_is_raised_and_resolves_when_filled(w):
    agent = w["trellis"]
    now = timezone.now()
    alerts.tick(now)
    assert not Alert.objects.filter(kind=src.KIND).exists()      # too new to judge
    Agent.objects.filter(pk=agent.pk).update(created_at=now - timedelta(days=2))
    alerts.tick(now + timedelta(minutes=1))
    alert = Alert.objects.get(kind=src.KIND)
    assert alert.status == Alert.OPEN and "no skill catalog" in alert.title
    assert [a.id for a in src.actions(alert, w["jj"])] == ["open", "dismiss"]
    _report(w["box"], [_cmd("trellis:turn")])
    skill_catalog.refresh(agent)
    alerts.tick(now + timedelta(minutes=2))
    assert Alert.objects.get(kind=src.KIND).status == Alert.RESOLVED
