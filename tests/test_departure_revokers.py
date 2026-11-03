"""Every grant that hangs off membership is revoked on departure, and the sweep runs each.

`apps.workspaces.departure` owns a registry; agents, harness, canopy_sessions and realtime
register their revokers from `AppConfig.ready()`. A revoker nobody registered silently leaves
a grant behind, so the expected list is written out here."""
from __future__ import annotations

import pytest

from apps.workspaces import departure
from apps.workspaces.testing import a_user, a_workspace

pytestmark = pytest.mark.django_db

EXPECTED = [
    "agent_admins", "runner_admins", "session_participants", "delegations",
    "runner_assignments", "workspace_orders", "runner_enrollments", "agents_unowned", "sockets",
]


def test_every_expected_revoker_is_registered_in_order():
    assert departure.revoker_names() == EXPECTED


def test_sweep_runs_every_registered_revoker(monkeypatch):
    ws = a_workspace()
    outsider = a_user("gone@example.com")  # in no workspace, so `ws` counts as left
    ran = []

    def spy(name):
        return lambda user, left, by: ran.append((name, list(left))) or 1

    for name, (order, _fn, counted) in list(departure._REVOKERS.items()):
        monkeypatch.setitem(departure._REVOKERS, name, (order, spy(name), counted))
    counts = departure.sweep(outsider, ws.slug)
    assert [n for n, _ in ran] == EXPECTED
    assert all(left == [ws.slug] for _, left in ran)
    assert {k: v for k, v in counts.items()} == {n: 1 for n in EXPECTED if n != "sockets"}
