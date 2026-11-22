"""A learner's audience decides who reads what it produced (`outputs.visible_to`): each leg opens only when the
learner's audience names it. A registration may only narrow its class's audience (`registry._audience`), so this only
ever closes a leg; the default (the whole class's audience) reads exactly as before."""
from __future__ import annotations

import pytest

from apps.agents.models import Agent, AgentProject
from apps.brain import outputs, registry, reporting
from apps.brain.models import BrainCapability, BrainOutput
from apps.canopy_sessions.models import CoachingRole
from apps.workspaces.models import Workspace
from tests.fixtures.brain_world import agree, brain_world, member, user

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    out = brain_world()
    root = user("root")
    root.is_superuser = True
    root.save()
    member(root, out["ws"])
    coach = user("coach_person")
    member(coach, out["ws"])
    CoachingRole.objects.create(workspace=out["ws"], user=coach)
    agree(out["alice"], "individual")
    reporting.set_manager(out["ws"], person=out["alice"], manager=out["bob"], by=out["owner"])
    return {**out, "root": root, "coach_person": coach}


def _out(w, cap, subject_type, subject):
    return BrainOutput.objects.create(capability=cap, kind="note", workspace=w["ws"], subject_type=subject_type,
                                      subject=str(subject), title="t", body={"x": 1})


def _narrow(cap, *audience):
    BrainCapability.objects.filter(pk=cap.pk).update(audience=list(audience))


def _sees(u, o, *, as_person=True) -> bool:
    return outputs.visible_to(u, as_person=as_person).filter(pk=o.pk).exists()


def test_the_whole_class_audience_reads_as_before(w):
    o = _out(w, w["coach"], "person", w["alice"].pk)
    assert all(_sees(u, o) for u in (w["alice"], w["coach_person"], w["bob"]))
    s = _out(w, w["sys"], "system", "acme")
    assert _sees(w["root"], s) and _sees(w["owner"], s)


@pytest.mark.parametrize(("audience", "seen_by", "hidden_from"), [
    (("person",), ("alice",), ("coach_person", "bob")),
    (("coaches",), ("coach_person",), ("alice", "bob")),
    (("manager",), ("bob",), ("alice", "coach_person")),
    (("person", "manager"), ("alice", "bob"), ("coach_person",)),
])
def test_an_individual_learner_narrowed_hides_the_legs_it_left_out(w, audience, seen_by, hidden_from):
    _narrow(w["coach"], *audience)
    o = _out(w, w["coach"], "person", w["alice"].pk)
    assert all(_sees(w[n], o) for n in seen_by)
    assert not any(_sees(w[n], o) for n in hidden_from)


def test_a_learner_with_no_audience_is_read_by_no_one(w):
    _narrow(w["coach"])
    o = _out(w, w["coach"], "person", w["alice"].pk)
    assert not any(_sees(w[n], o) for n in ("alice", "coach_person", "bob", "root"))


def test_a_persons_own_memory_stays_theirs_whatever_its_audience(w):
    mem = _out(w, w["mem"], "person", w["alice"].pk)
    _narrow(w["mem"], "serving_agents")
    assert _sees(w["alice"], mem) and not _sees(w["bob"], mem)


def test_a_project_learner_without_project_readers_is_read_by_no_one(w):
    lead = Agent.objects.create(slug="eva", name="Eva", owner=w["owner"], workspace=w["ws"])
    p = AgentProject.objects.create(agent=lead, ext_id="P1", name="Supply", handle="supply")
    o = _out(w, w["proj"], "project", p.pk)
    assert _sees(w["alice"], o)
    _narrow(w["proj"])
    assert not _sees(w["alice"], o)


def test_a_system_learner_narrowed_to_canopy_admins_hides_it_from_workspace_owners(w):
    s = _out(w, w["sys"], "system", "acme")
    _narrow(w["sys"], "canopy_admins")
    assert _sees(w["root"], s) and not _sees(w["owner"], s)


def test_a_system_learner_narrowed_to_agent_admins_hides_it_from_canopy_admins_who_do_not_manage_there(w):
    s = _out(w, w["sys"], "system", "acme")
    elsewhere = Workspace.objects.create(slug="beta", display_name="Beta", created_by=w["owner"])
    t = BrainOutput.objects.create(capability=w["sys"], kind="note", workspace=elsewhere, subject_type="system",
                                   subject="beta", title="t", body={})
    member(w["root"], elsewhere)                                   # an editor in beta, not an owner or admin there
    _narrow(w["sys"], "agent_admins")
    assert _sees(w["owner"], s)
    assert not _sees(w["root"], s) and not _sees(w["root"], t)     # canopy_admins left out: a superuser who is only
                                                                   # an editor in both opens nothing


def test_a_registered_narrowing_is_one_the_registry_allows(w):
    """The narrowings above are ones a canopy admin can make: the registry refuses anything wider."""
    with pytest.raises(registry.RefusedError):
        registry.update(BrainCapability.objects.get(pk=w["coach"].pk), audience=["person", "project_readers"])
