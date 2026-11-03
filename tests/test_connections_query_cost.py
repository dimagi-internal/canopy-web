"""What a connection's readers cost in queries: `recipients.device_reasons` runs on
every list, every keeper pass of every box (each 120 s) and under the row lock of
every write-back, so judging who vouches for a machine reads each agent's facts
once per call (`recipients.facts_for`), and an extra agent adds a small constant,
not another pass over the fleet."""
from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.agents.models import Agent, AgentAdmin
from apps.connections import keeper
from apps.connections import recipients as rcpt
from apps.connections.models import Connection
from apps.harness.models import Runner
from tests.fixtures.connections import ShareWorld

pytestmark = pytest.mark.django_db

# What was measured (69, 59, 58, 18), plus a little room. The last is what each extra agent
# may add: its vault's members, read once, and a few rows.
LIST_CAP, KEEPER_CAP, REASONS_CAP, PER_AGENT_CAP = 72, 62, 62, 20


def _add_agents(world, n: int) -> list[Agent]:
    """`n` more agents, each run by one cloud box of op's that jj vouches for."""
    out = []
    for i in range(n):
        a = Agent.objects.create(slug=f"extra-{i}", name=f"Extra {i}", workspace=world.ws, owner=world.op)
        AgentAdmin.objects.create(agent=a, user=world.person)
        world._box(f"extra-{i}-cloud", Runner.CLOUD, world.op, a)
        out.append(a)
    return out


def _shared_to_both(world) -> Connection:
    """jj's connection lent to eva and echo, and held by their boxes."""
    conn = world.shared(world.persons(), agents=[world.eva, world.echo])
    conn.refresh_from_db()
    return conn


def _count(call) -> int:
    with CaptureQueriesContext(connection) as q:
        call()
    return len(q)


def _list(world):
    c = Client()
    c.force_login(world.person)
    return lambda: c.get("/api/connections/")


def _keeper(world):
    runner = world.eva_box.runner
    return lambda: keeper.work(runner)


def test_listing_connections_is_bounded(django_assert_max_num_queries):
    world = ShareWorld()
    _shared_to_both(world)
    call = _list(world)
    with django_assert_max_num_queries(LIST_CAP):
        assert call().status_code == 200


def test_a_keeper_pass_with_nothing_waiting_is_bounded(django_assert_max_num_queries):
    world = ShareWorld()
    _shared_to_both(world)
    with django_assert_max_num_queries(KEEPER_CAP):
        assert _keeper(world)() == []


def test_device_reasons_is_bounded(django_assert_max_num_queries):
    world = ShareWorld()
    conn = _shared_to_both(world)
    with django_assert_max_num_queries(REASONS_CAP):
        assert rcpt.device_reasons(conn)


@pytest.mark.parametrize("reader", [_list, _keeper])
def test_an_extra_agent_adds_a_small_constant(reader):
    world = ShareWorld()
    conn = _shared_to_both(world)
    two = _count(reader(world))
    world.shared(conn, agents=[world.eva, world.echo, *_add_agents(world, 2)])
    four = _count(reader(world))
    assert (four - two) / 2 <= PER_AGENT_CAP, f"{two} queries with 2 agents, {four} with 4"
