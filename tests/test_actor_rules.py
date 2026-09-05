"""Actor rules: route by WHO the work came from, not just what kind it is.

A rule is the set of rows sharing (agent, source, actor), rank-ordered — a LIST
of runners, not one. That is forced by the operator's real topology: two macOS
accounts on one machine, alternated as each runs out of tokens, so "my work
stays on my boxes" names two runners whose live one rotates.

Precedence: explicit pin -> sticky binding -> (source, actor) -> (source, "")
-> default order. This file covers the two middle rungs; the outer two are
claim_next_turn's and are covered in tests/test_source_rules.py and the
directed-routing suite.

Spec: docs/superpowers/specs/2026-09-05-actor-aware-runner-routing-design.md
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

JON = "jjackson@dimagi.com"
SARVESH = "stewari@dimagi.com"


def _agent(slug="echo"):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=a_workspace())


def _runner(name, kind=Runner.EMDASH):
    return Runner.objects.create(name=name, kind=kind, capabilities={})


def _rows(agent, origin, actor=""):
    defaults, priorities = services.load_assignment_rows([agent.id])
    return [
        r for _rank, r in services.assignment_rows_for(agent.id, origin, actor, defaults, priorities)
    ]


def _rule(agent, source, actor, runners, *, strict=False, enabled=True):
    """One rule = its runners in rank order."""
    for rank, runner in enumerate(runners):
        RunnerAssignment.objects.create(
            agent=agent, runner=runner, rank=rank,
            source=source, actor=actor, strict=strict, enabled=enabled,
        )


# --- the constraint -----------------------------------------------------------

def test_a_runner_may_appear_only_once_within_one_rule():
    a, r1 = _agent(), _runner("r1")
    RunnerAssignment.objects.create(
        agent=a, runner=r1, rank=0, source=Turn.ORIGIN_EMAIL, actor=JON
    )
    with pytest.raises(IntegrityError):
        RunnerAssignment.objects.create(
            agent=a, runner=r1, rank=1, source=Turn.ORIGIN_EMAIL, actor=JON
        )


def test_two_actors_may_now_share_one_source():
    """The constraint this replaces capped a source at ONE row, which is what
    made per-person routing impossible."""
    a, cloud, laptop = _agent(), _runner("cloud-1", Runner.CLOUD), _runner("jj-mbp")
    _rule(a, Turn.ORIGIN_EMAIL, SARVESH, [cloud])
    _rule(a, Turn.ORIGIN_EMAIL, JON, [laptop])
    assert RunnerAssignment.objects.filter(agent=a, source=Turn.ORIGIN_EMAIL).count() == 2


# --- composition --------------------------------------------------------------

def test_an_actor_rule_beats_the_source_rule_which_beats_the_defaults():
    a = _agent()
    mine, cloud, other = _runner("acedimagi-mbp"), _runner("cloud-1", Runner.CLOUD), _runner("spare")
    RunnerAssignment.objects.create(agent=a, runner=other, rank=0)          # default
    _rule(a, Turn.ORIGIN_EMAIL, "", [cloud])                                # source rule
    _rule(a, Turn.ORIGIN_EMAIL, JON, [mine])                                # actor rule
    assert _rows(a, Turn.ORIGIN_EMAIL, JON) == [mine, cloud, other]


def test_an_unmatched_actor_falls_through_to_the_source_rule():
    a, mine, cloud = _agent(), _runner("acedimagi-mbp"), _runner("cloud-1", Runner.CLOUD)
    _rule(a, Turn.ORIGIN_EMAIL, "", [cloud])
    _rule(a, Turn.ORIGIN_EMAIL, JON, [mine])
    assert _rows(a, Turn.ORIGIN_EMAIL, SARVESH) == [cloud]


def test_an_actorless_turn_never_matches_an_actor_rule():
    """A scheduler turn resolves to actor="" and must land on the source rule."""
    a, mine, cloud = _agent(), _runner("acedimagi-mbp"), _runner("cloud-1", Runner.CLOUD)
    _rule(a, Turn.ORIGIN_EMAIL, "", [cloud])
    _rule(a, Turn.ORIGIN_EMAIL, JON, [mine])
    assert _rows(a, Turn.ORIGIN_EMAIL, "") == [cloud]


def test_an_actor_rule_is_scoped_to_its_own_source():
    a, cloud, laptop = _agent(), _runner("cloud-1", Runner.CLOUD), _runner("jj-mbp")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    _rule(a, Turn.ORIGIN_ACE_WEB, SARVESH, [cloud], strict=True)
    assert _rows(a, Turn.ORIGIN_ACE_WEB, SARVESH) == [cloud]
    assert _rows(a, Turn.ORIGIN_EMAIL, SARVESH) == [laptop]


# --- multi-runner rules: the case the model exists for -------------------------

def test_a_strict_rule_yields_its_runners_in_rank_order_and_no_others():
    """OPERATOR_BOXES: either of my accounts, never cloud."""
    a = _agent()
    cloud, acedimagi, jj = _runner("cloud-1", Runner.CLOUD), _runner("acedimagi-mbp"), _runner("jj-mbp")
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)      # cloud-DEFAULT agent
    RunnerAssignment.objects.create(agent=a, runner=acedimagi, rank=1)
    _rule(a, Turn.ORIGIN_EMAIL, JON, [acedimagi, jj], strict=True)
    assert _rows(a, Turn.ORIGIN_EMAIL, JON) == [acedimagi, jj]


def test_a_strict_rule_truncates_the_list_so_the_defaults_cannot_leak_back():
    """The bug caught in spec review: appending the default order after a strict
    rule hands work straight back to the runners the rule exists to exclude —
    and the 60s wedged-runner grace would then promote them."""
    a, cloud, mine = _agent(), _runner("cloud-1", Runner.CLOUD), _runner("acedimagi-mbp")
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    _rule(a, Turn.ORIGIN_EMAIL, JON, [mine], strict=True)
    assert cloud not in _rows(a, Turn.ORIGIN_EMAIL, JON)


def test_a_non_strict_multi_runner_rule_still_falls_through_to_the_defaults():
    a, r1, r2, spare = _agent(), _runner("r1"), _runner("r2"), _runner("spare")
    RunnerAssignment.objects.create(agent=a, runner=spare, rank=0)
    _rule(a, Turn.ORIGIN_EMAIL, JON, [r1, r2], strict=False)
    assert _rows(a, Turn.ORIGIN_EMAIL, JON) == [r1, r2, spare]


def test_a_runner_in_both_a_rule_and_the_defaults_appears_once_keeping_its_rule_rank():
    a, mine, spare = _agent(), _runner("acedimagi-mbp"), _runner("spare")
    RunnerAssignment.objects.create(agent=a, runner=spare, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=mine, rank=1)
    _rule(a, Turn.ORIGIN_EMAIL, JON, [mine])
    assert _rows(a, Turn.ORIGIN_EMAIL, JON) == [mine, spare]


def test_ranks_are_renumbered_from_zero_so_the_cascade_can_compare_them():
    """Two runners both sitting at rank 0 would each read as blocking the other."""
    a, mine, spare = _agent(), _runner("acedimagi-mbp"), _runner("spare")
    RunnerAssignment.objects.create(agent=a, runner=spare, rank=0)
    _rule(a, Turn.ORIGIN_EMAIL, JON, [mine])
    defaults, priorities = services.load_assignment_rows([a.id])
    assert [rank for rank, _r in
            services.assignment_rows_for(a.id, Turn.ORIGIN_EMAIL, JON, defaults, priorities)] == [0, 1]


# --- enabled ------------------------------------------------------------------

def test_disabling_every_row_of_a_rule_switches_the_rule_off_strictness_included():
    """Switching a rule off must not park its queue — the rule simply stops
    existing and the turn falls through."""
    a, cloud, mine = _agent(), _runner("cloud-1", Runner.CLOUD), _runner("acedimagi-mbp")
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    _rule(a, Turn.ORIGIN_EMAIL, JON, [mine], strict=True, enabled=False)
    assert _rows(a, Turn.ORIGIN_EMAIL, JON) == [cloud]


def test_disabling_one_runner_of_a_rule_drops_only_that_runner():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    _rule(a, Turn.ORIGIN_EMAIL, JON, [r1], strict=True, enabled=False)
    RunnerAssignment.objects.create(
        agent=a, runner=r2, rank=1, source=Turn.ORIGIN_EMAIL, actor=JON, strict=True
    )
    assert _rows(a, Turn.ORIGIN_EMAIL, JON) == [r2]


# --- backwards compatibility --------------------------------------------------

def test_a_rule_with_no_actor_behaves_exactly_like_a_pre_migration_source_rule():
    a, cloud, laptop = _agent(), _runner("cloud-1", Runner.CLOUD), _runner("jj-mbp")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    _rule(a, Turn.ORIGIN_ACE_WEB, "", [cloud])
    assert _rows(a, Turn.ORIGIN_ACE_WEB, JON) == [cloud, laptop]
    assert _rows(a, Turn.ORIGIN_ACE_WEB, "") == [cloud, laptop]
