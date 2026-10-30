"""Whose authority a credential carries where it is read: the one table
(`apps/agents/credentials/authority.py`, spec 2026-10-29-two-axes-three-credentials-design.md).

Pins the whole (placement, kind) table, and that a person's own direct turn is a direct
read whoever they are: an agent admin or a runner owner is held to the same answer as
anyone, and steps up like anyone.
"""
from __future__ import annotations

import pytest

from apps.agents.credentials import authority as a

TABLE = {
    (a.RUNNER, a.AGENT_ONLY): True, (a.RUNNER, a.VAULT): True, (a.RUNNER, a.DELEGATED): True,
    (a.DIRECT, a.AGENT_ONLY): False, (a.DIRECT, a.VAULT): True, (a.DIRECT, a.DELEGATED): False,
    (a.MANAGE, a.AGENT_ONLY): True, (a.MANAGE, a.VAULT): True, (a.MANAGE, a.DELEGATED): False,
}


@pytest.mark.parametrize(("key", "expected"), TABLE.items())
def test_the_table(key, expected):
    assert a.hands_out(*key) is expected


def test_the_table_is_complete_and_a_refusal_says_why():
    for placement in (a.RUNNER, a.DIRECT, a.MANAGE):
        for kind in (a.AGENT_ONLY, a.VAULT, a.DELEGATED):
            s = a.stance(placement, kind)
            assert s.hands_out or s.why, (placement, kind)


def test_every_principal_has_a_placement():
    assert {a.placement_of(v) for v in ("runner", "license", "admin")} == {a.RUNNER, a.DIRECT, a.MANAGE}


def test_in_a_direct_session_a_lent_account_is_the_persons_own():
    assert "your own account" in a.stance(a.DIRECT, a.DELEGATED).why
