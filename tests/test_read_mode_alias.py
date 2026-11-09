"""`read` and `READ_MODE_STAMP` are one read mode, and `published_profiles.is_read_mode` is the only place that says so.

: an alias only. Read mode still
resolves only through a published profile here; stored data (stamps, rules, lines) behaves as before.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.admission import evaluate
from apps.admission import rules as admission_rules
from apps.admission.models import AdmissionRule
from apps.agents import access, published_profiles, roles, turn_context
from apps.agents.published_profiles import is_read_mode, parse
from apps.agents.models import Agent
from apps.workspaces.models import Workspace, WorkspaceMembership

from .access_helpers import allow

IFACE = {"capabilities": {"ask": {"tools": ["Read"]}, "canopy": {"tools": ["Read", "Grep"]}}}


def test_the_one_function():
    assert is_read_mode("read") and is_read_mode("ask")
    assert not any(is_read_mode(n) for n in ("", None, "canopy", "write", "use:ask", "Read"))


@pytest.mark.parametrize("held,name,ok", [
    ({roles.READ}, "ask", True),
    ({roles.READ}, "read", True),
    ({"use:ask"}, "read", True),          # a `use:ask` line is a read-mode line
    ({"use:read"}, "ask", True),
    ({"use:canopy"}, "ask", False),
    ({roles.READ}, "canopy", False),
    ({roles.WRITE, "use:*", roles.READ}, "canopy", True),
])
def test_opens_treats_read_and_the_stamp_alike(held, name, ok):
    assert published_profiles.opens(frozenset(held), name) is ok


def test_profile_of_either_name_is_the_published_read_mode():
    class A:
        interface = parse(IFACE)

    assert published_profiles.profile(A, "ask")["tools"] == ["Read"]
    got = published_profiles.profile(A, "read")
    assert got["tools"] == ["Read"] and got["name"] == "read"       # the stamp is kept as given


def test_an_unpublished_name_still_fails_closed():
    class A:
        interface = parse(IFACE)

    gone = published_profiles.profile(A, "retired-site")
    assert gone["tools"] == [] and gone["bash"] == [] and gone["write_paths"] == []

    # (a bare agent's `read` now builds the default read profile: tests/test_read_mode.py)


def _rule(**kw):
    return AdmissionRule(action=AdmissionRule.REJECT, **kw)


@pytest.mark.parametrize("want,have,expected", [
    ("ask", "read", True), ("read", "ask", True), ("ask", "ask", True), ("read", "read", True),
    ("ask", "canopy", False), ("read", "", False), ("canopy", "ask", False), ("canopy", "read", False),
])
def test_admission_matches_read_mode_by_either_name(want, have, expected):
    assert evaluate.match(_rule(capability=want), {"capability": have}) is expected


def test_admission_summary_says_read_mode_for_either_name():
    for name in ("ask", "read"):
        assert "read mode" in admission_rules.summary(_rule(capability=name))
    assert "host site canopy" in admission_rules.summary(_rule(capability="canopy"))


@pytest.mark.django_db
def test_a_stamped_or_use_read_line_reaches_the_published_read_mode():
    mk = get_user_model().objects.create_user
    owner, vw = mk("o", "o@dimagi.com", "pw"), mk("v", "v@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=vw, workspace=ws, role=WorkspaceMembership.VIEWER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner, interface=parse(IFACE))
    for cap in ("ask", "read"):
        line = allow(agent, capability=cap)
        d = turn_context.for_asker(agent, vw, verified=True)
        assert (d.access, d.stamped) == (access.CONFINED, "ask"), cap     # stamped with what runs
        line.delete()
    # Asking by the other name reaches the same published read mode.
    allow(agent, role=roles.READ_ONLY)
    assert turn_context.for_asker(agent, vw, verified=True, requested="read").stamped == "ask"
    assert turn_context.for_asker(agent, vw, verified=True, requested="canopy").access == turn_context.REFUSED
