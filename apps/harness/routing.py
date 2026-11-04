"""Directed runner routing — which runner may take which turn.

The per-agent ordered runner lists (`RunnerAssignment`) and the source/actor rules
layered on them, the workspace default order an agent with no runners of its own
follows (`inherited_orders`), the repo-turn order, the tenant gate every
runner-scoped predicate shares (`runner_tenant_slugs` / `agent_tenant_q`), and the
one-executing-turn-per-agent-or-session rule with its mid-turn riders.

Predicates over rows: nothing here writes a turn. `claim.py` composes them into
the claim, and every reader that asks "who would run this?" (the topology, the
turn status, the stuck-turn warning) asks these same functions, so the claim and
its explanations cannot disagree. Split out of `services.py`; import these names from here.

The code lives in four modules and this one re-exports every name: `routing_orders` (the
core), `routing_machine` (several runners on one machine), `routing_serving` (which agents
a box runs) and `routing_projects` (project chats).
"""

from __future__ import annotations

from .routing_machine import (  # noqa: F401
    _MACHINE_AGENT_RUNNERS,
    _dedicated_refusal,
    _served_agent_slug,
    dedicated_agent_slugs,
    machine_agent_runners,
    prime_dedicated_agents,
    prime_machine_runners,
    serves_refusal,
    turn_serves_refusal,
)
from .routing_orders import (  # noqa: F401
    CASCADE_GRACE_SECONDS,
    ENVELOPE_VERSION,
    EXECUTING,
    MIDTURN_FAILED,
    PROFILES_VERSION,
    RUNG_DEFAULT,
    RUNG_PERSON,
    RUNG_SOURCE,
    UNPREPARED,
    InheritedOrder,
    _ancestor_chains,
    _cascade_allows,
    _dialog_up,
    _transfers_in_progress,
    agent_tenant_q,
    agents_following_runner,
    agents_with_own_order,
    assignment_allows,
    assignment_allows_for_agent,
    assignment_rows_for,
    blocking_turn,
    busy_session_ids,
    cannot_serve,
    covered_slugs,
    default_order_source,
    delivers_midturn,
    enrolled_slugs,
    inherited_orders,
    is_repo_turn,
    kind_allows,
    labelled_assignment_rows,
    load_assignment_rows,
    load_workspace_orders,
    may_ride,
    order_for,
    profile_q,
    repo_order_allows,
    repo_order_for,
    ridable_sessions,
    routed_slug,
    runner_target_q,
    runner_tenant_slugs,
    runner_tenant_slugs_of,
    seed_assignments_from_capabilities,
    transfer_held_q,
    transfer_holding,
    turn_workspace_q,
)
from .routing_projects import (  # noqa: F401
    _chatter_administers,
    is_project_session_turn,
    project_session_allows,
    project_session_covers,
    project_session_rows,
    project_session_waits_why,
)
from .routing_serving import (  # noqa: F401
    ServingFacts,
    agents_routed_to,
    runner_routes_agent,
)
