"""Authorization tests for /api/harness — a non-member must get 404, never 403,
and never a leak that the resource exists. Mirrors apps/agents' posture."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def stranger():
    """Authenticated, but a member of nothing. auto_join_workspaces keys off the
    email domain, so use one outside the auto-join set."""
    return User.objects.create_user("stranger", "stranger@example.org", "pw")


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


@pytest.fixture()
def owner_client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def stranger_client(stranger):
    c = Client()
    c.force_login(stranger)
    return c


def _enqueue(client, slug="echo", key="k1"):
    return client.post(
        "/api/harness/turns/",
        {"agent_slug": slug, "origin": "manual", "idempotency_key": key, "prompt": "/echo:turn"},
        content_type="application/json",
    )


def test_member_can_enqueue(owner_client, agent):
    assert _enqueue(owner_client).status_code == 201


def test_stranger_enqueueing_for_someone_elses_agent_gets_404(stranger_client, agent):
    """404, not 403: a non-member must not learn the agent exists."""
    resp = _enqueue(stranger_client)
    assert resp.status_code == 404


def test_stranger_cannot_read_someone_elses_turn(owner_client, stranger_client, agent):
    turn_id = _enqueue(owner_client).json()["id"]
    resp = stranger_client.get(f"/api/harness/turns/{turn_id}")
    assert resp.status_code == 404
    # Security review 2026-07-26, F4: status-code-only assertions here are
    # exactly what let the agent-turn branch leak the agent's slug via
    # _agent_or_404's own 404 message (copied into both title and detail by
    # the shared HttpError handler) go undetected. _turn_or_404 now catches
    # and re-raises a uniform message — pin the body, not just the code.
    body = resp.json()
    assert body["title"] == "turn not found"
    assert body["detail"] == "turn not found"
    assert "echo" not in body["title"].lower()
    assert "echo" not in (body["detail"] or "").lower()


def test_stranger_cannot_finish_someone_elses_turn(owner_client, stranger_client, agent):
    turn_id = _enqueue(owner_client).json()["id"]
    resp = stranger_client.post(
        f"/api/harness/turns/{turn_id}/finish",
        {"status": "done", "result_note": "pwned"},
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert Turn.objects.get(pk=turn_id).status == Turn.QUEUED  # untouched


def test_stranger_cannot_heartbeat_someone_elses_runner(owner_client, stranger_client, workspace):
    rid = owner_client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {"agents": ["echo"]}},
        content_type="application/json",
    ).json()["id"]
    resp = stranger_client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert Runner.objects.get(pk=rid).last_heartbeat_at is None  # untouched


def test_stranger_cannot_claim_as_someone_elses_runner(owner_client, stranger_client, agent):
    rid = owner_client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {"agents": ["echo"]}},
        content_type="application/json",
    ).json()["id"]
    _enqueue(owner_client)
    # Heartbeat the runner online FIRST — otherwise claim_next_turn bails on line 1
    # (status != ONLINE) before ever reaching the tenant/ownership check below, and
    # the 404 this test asserts would prove nothing about the claim path itself.
    hb = owner_client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 200  # sanity: the runner is genuinely ONLINE
    assert stranger_client.post(f"/api/harness/runners/{rid}/claim").status_code == 404


def test_stranger_cannot_claim_victim_turn_via_own_untenanted_runner(
    owner_client, stranger_client, agent
):
    """The actual exploit (Critical): capabilities is caller-supplied and never
    validated at pairing. A stranger pairs their OWN runner (so _runner_or_404
    admits them forever — they own it), declares capabilities={"agents": ["echo"]}
    even though 'echo' belongs to the owner's workspace, heartbeats it online (the
    only precondition claim_next_turn enforces), then claims. This must return 204
    (no work for them) and must NOT mutate the victim's turn — non-mutation is the
    point: a claim that both leaks the prompt/origin_ref AND flips the turn to
    CLAIMED is a repeatable denial of service against the victim's real runner.

    Pairing an untenanted runner via the API now 422s (a workspace is required
    since the 2026-07-25 incident), but LEGACY null-workspace rows still exist
    in prod — so mint the row directly and prove the claim path still holds."""
    turn_id = _enqueue(owner_client).json()["id"]
    stranger = User.objects.get(username="stranger")
    rid = Runner.objects.create(
        name="attacker-mbp", kind="emdash", capabilities={"agents": ["echo"]},
        paired_by=stranger, workspace=None,
    ).id
    hb = stranger_client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 200  # sanity: this is the stranger's own runner
    resp = stranger_client.post(f"/api/harness/runners/{rid}/claim")
    assert resp.status_code == 204  # no work for them — capabilities is not the gate
    victim_turn = Turn.objects.get(pk=turn_id)
    assert victim_turn.status == Turn.QUEUED
    assert victim_turn.claimed_by is None


def test_tenanted_attacker_cannot_claim_other_workspace_turn(owner_client, agent):
    """Tenant boundary on claim_next_turn's production branch: a tenanted runner
    (homed to workspace 'evil') cannot claim turns for agents in another workspace
    ('canopy'). Tests the if runner.workspace_id branch (lines 131-132)."""
    attacker = User.objects.create_user("attacker", "attacker@dimagi.com", "pw")
    evil_ws = Workspace.objects.create(slug="evil", display_name="Evil", created_by=attacker)
    WorkspaceMembership.objects.create(user=attacker, workspace=evil_ws, role=WorkspaceMembership.OWNER)
    attacker_client = Client()
    attacker_client.force_login(attacker)

    # Enqueue a turn in the owner's workspace (canopy) for agent echo
    turn_id = _enqueue(owner_client).json()["id"]

    # Attacker pairs their own runner (homed to evil workspace)
    rid = attacker_client.post(
        "/api/harness/runners/",
        {"name": "attacker-mbp", "kind": "emdash", "capabilities": {"agents": ["echo"]}},
        content_type="application/json",
    ).json()["id"]

    # Heartbeat it online
    hb = attacker_client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 200  # sanity: attacker's own runner is ONLINE

    # Attempt to claim the owner's turn — must return 204 (no work for them)
    # and must NOT mutate the victim's turn
    resp = attacker_client.post(f"/api/harness/runners/{rid}/claim")
    assert resp.status_code == 204  # tenant predicate blocks: agent not in evil workspace

    # Assert non-mutation: the victim's turn is still QUEUED and unclaimed
    victim_turn = Turn.objects.get(pk=turn_id)
    assert victim_turn.status == Turn.QUEUED
    assert victim_turn.claimed_by is None


# --- claim scope: one runner, a fleet that spans workspaces (the outage) -------


def test_runner_claims_for_agents_in_any_workspace_its_pairer_belongs_to(owner, owner_client, workspace):
    """The production shape, and the outage this pins closed.

    There is ONE laptop runner but the agent fleet deliberately spans workspaces:
    `eva` is homed to the runner's own workspace, `ace` to a sibling one. The
    pairing human is a member of BOTH. Scoping the claim by `runner.workspace_id`
    (the shipped rule) let the runner claim only `eva` — `ace`'s turns sat QUEUED
    forever, which is exactly what happened on production (4 of 5 agents unable to
    execute any turn at all).

    The claim must scope by the workspaces `runner.paired_by` is a member of —
    consistent with `_runner_schedule_qs`, which already derives tenancy that way
    for the schedule-sync route. `owner` can already drive `ace` through the UI,
    so claiming for it is no escalation.
    """
    sibling = Workspace.objects.create(
        slug="connect", display_name="Connect", created_by=owner, auto_join_domains=[]
    )
    WorkspaceMembership.objects.create(
        user=owner, workspace=sibling, role=WorkspaceMembership.OWNER
    )
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=workspace)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=sibling)

    # One runner, homed to `workspace` (as the 0005 backfill homed the live one),
    # declaring the whole fleet in its capabilities.
    rid = owner_client.post(
        "/api/harness/runners/",
        {
            "name": "jj-mbp-cdp",
            "kind": "emdash",
            "capabilities": {"agents": ["eva", "ace"]},
            "workspace": workspace.slug,
        },
        content_type="application/json",
    ).json()["id"]
    assert Runner.objects.get(pk=rid).workspace_id == workspace.slug  # sanity: homed to one
    RunnerAssignment.objects.create(agent=eva, runner_id=rid, rank=0)
    RunnerAssignment.objects.create(agent=ace, runner_id=rid, rank=0)

    hb = owner_client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 200  # sanity: ONLINE, or claim_next_turn bails before the scope check

    turn_id = _enqueue(owner_client, slug="ace", key="k-ace").json()["id"]

    resp = owner_client.post(f"/api/harness/runners/{rid}/claim")
    assert resp.status_code == 200, "the cross-workspace turn must be claimable"
    assert resp.json()["id"] == turn_id
    claimed = Turn.objects.get(pk=turn_id)
    assert claimed.status == Turn.CLAIMED
    assert str(claimed.claimed_by_id) == rid


def test_runner_with_null_paired_by_claims_nothing_tenanted(owner_client, agent, workspace):
    """Runner.paired_by is SET_NULL, so an orphaned runner must fail closed: with
    no pairer there is no identity to derive a tenant from, and inferring one
    would be a privilege escalation. It must claim NOTHING tenanted — not
    everything, and not the workspace it happens to still be homed to.

    (It retains the null-workspace legacy leg, which is what the pre-tenancy
    suite runs on; see test_harness_services.py, whose runners are all
    paired_by=None. A null-workspace agent is ungated everywhere in this
    codebase, and none exist in production.)
    """
    orphan = Runner.objects.create(
        name="orphan", kind=Runner.EMDASH, capabilities={"agents": ["echo"]},
        paired_by=None, workspace=workspace,
    )
    from apps.harness import services as hsvc

    hsvc.heartbeat(orphan, active_turn_ids=[])  # ONLINE, or this proves nothing
    assert Runner.objects.get(pk=orphan.id).status == Runner.ONLINE

    turn_id = _enqueue(owner_client).json()["id"]
    assert hsvc.claim_next_turn(orphan) is None
    victim = Turn.objects.get(pk=turn_id)
    assert victim.status == Turn.QUEUED
    assert victim.claimed_by is None


def test_list_turns_only_shows_my_tenants_turns(owner_client, stranger_client, agent):
    _enqueue(owner_client)
    resp = stranger_client.get("/api/harness/turns/")
    assert resp.status_code == 200
    assert resp.json() == []  # filtered, not 404 — a list of nothing


def test_null_workspace_agent_now_fails_closed(owner_client):
    """Superseded by the security review (2026-07-26, F1): a workspace-less
    agent used to be silently ungated here (`if agent.workspace_id and not
    is_member(...)` short-circuits to "allow" when workspace_id is falsy) —
    any authenticated user, not just a workspace member, could act on it.
    With transcripts that would hand a stranger an agent's full raw
    `claude -p` output. `_agent_or_404` now fails CLOSED instead: a
    workspace-less agent is unresolvable via this API until it's homed.

    Production carries zero agents with workspace_id IS NULL (verified at
    review time), and the real creation path (`apps.agents.api.upsert_agent`)
    always homes a new agent to a workspace — this is a legacy/pre-migration
    edge case, not a live user-facing flow."""
    Agent.objects.create(slug="legacy", name="Legacy")
    assert _enqueue(owner_client, slug="legacy", key="k-legacy").status_code == 404


def test_list_turns_excludes_unhomed_agent_turns(owner_client):
    """Security review 2026-07-26, hole B: list_turns unconditionally OR'd in
    Q(agent__workspace_id__isnull=True), so ANY authenticated caller saw every
    unhomed agent's turns — TurnOut leaks `prompt`, `origin_ref`, `session_id`.
    That is strictly more permissive than `_agent_or_404` (F1 above), which
    already 404s a workspace-less agent for everyone: a turn could be listed
    here and then 404 on every single-turn action, the exact list-vs-gate
    drift `_runner_visibility_q`'s docstring warns about. Fails closed to
    match: an unhomed agent's turns are invisible in the list too.

    Enqueueing via the API is itself fail-closed now, so build the turn
    directly (mirrors the legacy/pre-migration row this models)."""
    orphan = Agent.objects.create(slug="orphan", name="Orphan")
    Turn.objects.create(
        agent=orphan, origin=Turn.ORIGIN_MANUAL, idempotency_key="k-orphan",
        prompt="/echo:turn",
    )
    resp = owner_client.get("/api/harness/turns/")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_turns_excludes_other_tenants_project_and_session_turns(owner_client, stranger_client, workspace):
    """Found while fixing hole B, broader than that hole's own description: the
    old single clause `Q(agent__workspace_id__isnull=True)` traverses a
    nullable FK, so for a PROJECT or SESSION turn (agent_id IS NULL) it was
    unconditionally true regardless of the turn's OWN workspace — every
    tenant's project/session turns (prompt, origin_ref, session_id) leaked to
    every authenticated caller, not just the unhomed-agent case. Verified
    empirically pre-fix with a throwaway repro before writing this test.
    Mirrors the same split-by-target-kind fix `claim_next_turn`
    (apps/harness/services.py) already applies to its own tenant_q."""
    Turn.objects.create(
        project="secret-repo", workspace=workspace, origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p-secret", prompt="secret project prompt",
    )
    resp = stranger_client.get("/api/harness/turns/")
    assert resp.status_code == 200
    assert resp.json() == []

    # The owner (an actual member of the turn's workspace) still sees it —
    # this is a tenant filter, not a blanket lockout.
    owner_resp = owner_client.get("/api/harness/turns/")
    assert owner_resp.status_code == 200
    assert len(owner_resp.json()) == 1


# --- pair_runner workspace-assignment branches (Task 2, uncovered) ---------


def test_pair_runner_with_explicit_workspace_the_caller_is_member_of(owner_client, workspace):
    resp = owner_client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {}, "workspace": workspace.slug},
        content_type="application/json",
    )
    assert resp.status_code == 201
    assert resp.json()["workspace"] == workspace.slug


def test_pair_runner_with_explicit_workspace_the_caller_is_not_member_of_gets_404(
    stranger_client, workspace
):
    """404, not 403 — same as a nonexistent workspace, no existence leak."""
    resp = stranger_client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {}, "workspace": workspace.slug},
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert not Runner.objects.filter(name="jj-mbp").exists()


def test_pair_runner_with_no_workspace_homes_to_the_callers_sole_membership(owner_client, owner, workspace):
    """No `workspace` key + exactly one membership -> homed to that default workspace.
    `wsvc.user_default_workspace` only resolves when the caller has EXACTLY one
    membership, so `owner`'s single `workspace` membership makes this unambiguous."""
    assert wsvc.user_default_workspace(owner).slug == workspace.slug  # sanity: exactly one
    resp = owner_client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {}},
        content_type="application/json",
    )
    assert resp.status_code == 201
    assert resp.json()["workspace"] == workspace.slug


# --- list_runners / _runner_or_404 agreement under a tenant-pinned request ----
#
# The two must be built from the same predicate (_runner_visibility_q) so a
# runner that is listed is always one you can act on. Before the fix, a
# null-workspace runner was listed under a tenant-pinned request (list OR'd in
# workspace_id__isnull=True unconditionally) but 404'd on every action
# (_runner_or_404's first check treated null as wrong-tenant once a tenant was
# pinned) — the supervisor would render a runner every action then 404'd on.


def test_pinned_null_workspace_runner_is_neither_listed_nor_actionable(owner_client, owner, workspace):
    """Under /api/w/{ws}/harness/..., a null-workspace runner must be invisible
    on BOTH halves: not in the list, and 404 on heartbeat. Asserted together —
    the agreement between list and gate is the property under test."""
    runner = Runner.objects.create(
        name="legacy-runner", kind=Runner.EMDASH, capabilities={}, paired_by=owner,
        workspace=None,
    )
    listed_ids = [
        r["id"] for r in owner_client.get(f"/api/w/{workspace.slug}/harness/runners/").json()
    ]
    assert str(runner.id) not in listed_ids

    hb = owner_client.post(
        f"/api/w/{workspace.slug}/harness/runners/{runner.id}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 404
    assert Runner.objects.get(pk=runner.id).last_heartbeat_at is None  # untouched


def test_pinned_matching_workspace_runner_is_listed_and_actionable(owner_client, owner, workspace):
    """Under /api/w/{ws}/harness/..., a runner homed to THAT workspace is listed
    and heartbeat succeeds — the positive counterpart to the null-workspace case."""
    runner = Runner.objects.create(
        name="tenant-runner", kind=Runner.EMDASH, capabilities={}, paired_by=owner,
        workspace=workspace,
    )
    listed_ids = [
        r["id"] for r in owner_client.get(f"/api/w/{workspace.slug}/harness/runners/").json()
    ]
    assert str(runner.id) in listed_ids

    hb = owner_client.post(
        f"/api/w/{workspace.slug}/harness/runners/{runner.id}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 200
    assert Runner.objects.get(pk=runner.id).last_heartbeat_at is not None


def test_stranger_cannot_retire_someone_elses_runner(owner_client, stranger_client, workspace):
    rid = owner_client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {"agents": ["echo"]}},
        content_type="application/json",
    ).json()["id"]
    resp = stranger_client.post(f"/api/harness/runners/{rid}/retire")
    assert resp.status_code == 404
    assert Runner.objects.get(pk=rid).status != Runner.RETIRED  # untouched
