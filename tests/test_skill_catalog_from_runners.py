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
    box = Runner.objects.create(name="cloud", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    other = Runner.objects.create(name="theirs", kind=Runner.EMDASH, owner=stranger, workspace=ws,
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now())
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


def test_a_report_that_cannot_date_its_copy_leaves_a_published_catalog_alone(w):
    # An older runner's report, which could be older than what was published.
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


def _dated(name, description="d", installed="2026-11-04T10:00:00.000Z", **kw):
    return {**_cmd(name, description, **kw), "plugin_updated_at": installed}


def _published(agent, name, *, at="2026-07-20T00:00:00Z", **kw):
    row = AgentSkill.objects.create(agent=agent, name=name, **kw)
    AgentSkill.objects.filter(pk=row.pk).update(created_at=at)
    return row


def test_a_published_catalog_stays_fresh_and_keeps_what_was_published(w):
    agent = w["trellis"]
    _published(agent, "turn", description="old words", url="https://x/turn", improvement_note="n",
               launchable=True, args_hint="scope")
    _published(agent, "gone-from-the-repo", improvement_note="n")
    _report(w["box"], [_dated("trellis:turn", "new words", args="other"), _dated("trellis:added", "added"),
                       {**_dated("trellis:shipping"), "internal": True}])

    assert skill_catalog.refresh(agent) == 3

    rows = _catalog(w["jj"], agent)
    assert set(rows) == {"turn", "added", "shipping"}
    turn = rows["turn"]
    assert (turn["url"], turn["improvement_note"], turn["launchable"], turn["args_hint"]) == (
        "https://x/turn", "n", True, "scope")
    assert turn["description"] == "new words"
    assert rows["added"]["launchable"] is False and rows["shipping"]["internal"] is True
    sources = dict(agent.skills.values_list("name", "source"))
    assert sources == {"turn": "published", "added": "runner", "shipping": "runner"}


def test_a_description_cut_at_the_boxs_limit_does_not_replace_a_published_one(w):
    agent = w["trellis"]
    _published(agent, "turn", description="the whole published description")
    _report(w["box"], [_dated("trellis:turn", "x" * skill_catalog.DESCRIPTION_LIMIT)])
    skill_catalog.refresh(agent)
    assert _catalog(w["jj"], agent)["turn"]["description"] == "the whole published description"


def test_acps_label_is_not_part_of_a_description(w):
    _report(w["box"], [_cmd("trellis:turn", "(trellis) Do a turn.", source="acp")])
    skill_catalog.refresh(w["trellis"])
    assert _catalog(w["jj"], w["trellis"])["turn"]["description"] == "Do a turn."


def test_a_copy_installed_before_the_publish_changes_only_the_mark(w):
    agent = w["trellis"]
    _published(agent, "published-today", description="words", at="2026-11-04T12:00:00Z")
    _report(w["box"], [{**_dated("trellis:published-today", "older"), "internal": True},
                       _dated("trellis:older-skill")])
    assert skill_catalog.refresh(agent) is None
    rows = _catalog(w["jj"], agent)
    assert set(rows) == {"published-today"}
    assert rows["published-today"]["description"] == "words" and rows["published-today"]["internal"] is True


def test_the_box_with_the_newest_copy_wins_whichever_reported_last(w):
    agent = w["trellis"]
    laptop = Runner.objects.create(name="laptop", kind=Runner.EMDASH, owner=w["jj"], workspace=w["ws"],
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    _report(w["box"], [_dated("trellis:current", installed="2026-11-04T10:00:00Z")])
    # The stale box restarted, so its unchanged list is the most recent report.
    _report(laptop, [_dated("trellis:old", installed="2026-08-01T10:00:00Z")])
    assert skill_catalog.refresh(agent) == 1
    assert set(_catalog(w["jj"], agent)) == {"current"}


def test_a_box_that_has_gone_is_not_asked(w):
    _report(w["box"], [_cmd("trellis:turn")])
    Runner.objects.filter(pk=w["box"].pk).update(last_heartbeat_at=timezone.now() - timedelta(days=30))
    assert skill_catalog.refresh(w["trellis"]) is None
    assert w["trellis"].skills.count() == 0


def test_a_box_reporting_none_of_the_agents_skills_wipes_nothing(w):
    agent = w["trellis"]
    _published(agent, "turn")
    _report(w["box"], [_dated("ace:run"), _cmd("compact", source="acp")])
    assert skill_catalog.refresh(agent) is None
    assert set(_catalog(w["jj"], agent)) == {"turn"}


def test_a_report_at_its_size_limit_adds_but_removes_nothing(w):
    agent = w["trellis"]
    _published(agent, "zebra", improvement_note="n")
    padding = [_cmd(f"other:{i}") for i in range(skill_catalog.REPORT_LIMIT - 1)]
    _report(w["box"], [*padding, _dated("trellis:added")])
    assert skill_catalog.refresh(agent) == 2
    assert set(_catalog(w["jj"], agent)) == {"zebra", "added"}


def test_a_report_that_would_remove_most_of_a_catalog_removes_nothing(w, caplog):
    agent = w["trellis"]
    for i in range(10):
        _published(agent, f"skill-{i}")
    _report(w["box"], [_dated("trellis:skill-0"), _dated("trellis:skill-1"), _dated("trellis:added")])
    assert skill_catalog.refresh(agent) == 11
    assert "kept 8 of 10" in caplog.text


def test_removing_up_to_half_of_a_catalog_is_an_ordinary_change(w):
    agent = w["trellis"]
    for i in range(10):
        _published(agent, f"skill-{i}")
    _report(w["box"], [_dated(f"trellis:skill-{i}") for i in range(5)])
    assert skill_catalog.refresh(agent) == 5


def test_a_skill_only_acp_remembers_is_not_a_skill_in_a_dated_report(w):
    agent = w["trellis"]
    _published(agent, "deleted-last-week")
    _report(w["box"], [_dated("trellis:turn"), _cmd("trellis:deleted-last-week", source="acp")])
    assert skill_catalog.refresh(agent) == 1
    assert set(_catalog(w["jj"], agent)) == {"turn"}


def test_a_publish_still_replaces_the_merged_catalog_and_an_older_copy_cannot_undo_it(w):
    agent = w["trellis"]
    _published(agent, "turn")
    _report(w["box"], [_dated("trellis:turn"), _dated("trellis:added")])
    skill_catalog.refresh(agent)
    c = Client()
    c.force_login(w["jj"])
    resp = c.put(f"/api/agents/{agent.slug}/skills/", {"skills": [{"name": "only-this", "launchable": True}]},
                 content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert skill_catalog.refresh(agent) is None  # the copy on the box predates that publish
    assert set(_catalog(w["jj"], agent)) == {"only-this"}


def test_the_tick_reads_the_reports_once_however_many_agents(w, monkeypatch):
    Agent.objects.create(slug="second", name="Second", workspace=w["ws"], owner=w["jj"])
    _report(w["box"], [_dated("trellis:turn"), _dated("second:turn")])
    calls, real = [], skill_catalog._reports
    monkeypatch.setattr(skill_catalog, "_reports", lambda: calls.append(1) or real())
    assert skill_catalog.refresh_all() == {"trellis": 1, "second": 1}
    assert calls == [1]


def test_an_older_report_can_still_mark_a_published_catalog(w):
    # A publish cannot say a skill is internal, so only a report can mark it, even
    # one too old or undated to change the rows.
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
