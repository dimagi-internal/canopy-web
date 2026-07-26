"""Django Ninja router for the unified agent run lifecycle, mounted under the
`/api/agents/{slug}/runs/...` namespace.

Store-agnostic by construction: every handler resolves the agent's `RunStore`
via `resolver.get_run_store` and then calls ONLY the `RunStore` Protocol. Which
store answers (DB-as-truth today, Drive-as-truth later) is the resolver's
concern, never an endpoint's. Responses are the storage-agnostic read model
(`Run` / `RunSummary` / `Step` / `Gate`) straight from `schemas.py` — the read
model stays the single source of truth.
"""
from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError
from pydantic import Field

from apps.api.auth import session_auth
from apps.api.pagination import Page, clamp_limit, paginate
from apps.common.schemas import StrictModel

from . import resolver
from .schemas import Gate, Run, RunMode, RunSummary, Step, StepStatus, Verdict, VerdictKind
from .stores import RunStore

router = Router(auth=session_auth, tags=["agent-runs"])


# ---- request input schemas (the read-model schemas in schemas.py stay pure
#      Out/read-model types; these are the write-side payloads) ----
class StepIn(StrictModel):
    """A seed step for a freshly created run."""

    key: str = Field(min_length=1, max_length=120)
    ordinal: int = 0
    title: str = ""
    status: StepStatus = "pending"


class RunCreateIn(StrictModel):
    """Create a run (optionally seeded with steps)."""

    label: str = ""
    mode: RunMode = "review"
    current_step: str = ""
    session_link: str = ""
    steps: list[StepIn] = Field(default_factory=list)


class GateDecisionIn(StrictModel):
    """Record (close) a gate decision on a step."""

    decision: str = Field(min_length=1, max_length=120)
    decided_by: str = ""
    note: str = ""


class VerdictIn(StrictModel):
    """Record a judge/QA verdict on a step. `kind=qa` is the binary gate;
    `kind=judge` carries the 0-N quality score that the run aggregates."""

    kind: VerdictKind
    score: float | None = None
    passed: bool | None = None
    criteria: dict = Field(default_factory=dict)
    rationale: str = ""


class ForkIn(StrictModel):
    """Fork a run at a step boundary."""

    at_step: str = Field(min_length=1, max_length=120)
    mode: str = "keep-overrides-only"
    # {step_key: {question: <override-str-or-edit-dict>}} — see stores._apply_decision_edit
    edits: dict = Field(default_factory=dict)


# ---- helpers ----
def _get_agent_or_404(request, slug: str):
    """Resolve an agent, gated by workspace membership (non-member → 404, no
    existence leak). Mirrors apps/agents' + apps/harness's gate (deliberately
    duplicated, not imported — api modules must not depend on each other) so
    runs are scoped via their agent.

    Fails CLOSED on an unhomed agent (agent.workspace_id IS NULL): the old
    `if agent.workspace_id and not wsvc.is_member(...)` short-circuited to
    "allow" whenever workspace_id was falsy, handing ANY authenticated user
    full read/write on a workspace-less agent's runs across every route in
    this file (list/create runs, get run, list steps, gate, verdict, fork).
    Flagged but left out of scope by the #421 security-review fix that closed
    the same pattern in apps/agents/api.py and apps/harness/api.py — this is
    that fourth site. Latent today (no unhomed agents in production), but a
    fail-open tenancy gate is a bug regardless of whether anything currently
    exploits it: an unhomed agent is simply unresolvable via this API until
    it's backfilled a workspace, not universally visible."""
    from apps.agents import services
    from apps.workspaces import services as wsvc

    agent = services.get_agent(slug)
    if agent is None:
        raise HttpError(404, f"agent '{slug}' not found")
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws and agent.workspace_id != ws:
        raise HttpError(404, f"agent '{slug}' not found")  # wrong tenant
    if not agent.workspace_id or not wsvc.is_member(request.user, agent.workspace_id):
        raise HttpError(404, f"agent '{slug}' not found")
    return agent


def _store_for(request, slug: str) -> tuple[object, RunStore]:
    agent = _get_agent_or_404(request, slug)
    return agent, resolver.get_run_store(agent)


def _run_or_404(store: RunStore, slug: str, run_id: str) -> Run:
    try:
        return store.get_run(slug, run_id)
    except (ObjectDoesNotExist, KeyError):
        raise HttpError(404, f"run '{run_id}' not found for agent '{slug}'")


# ---- endpoints ----
@router.get("/{slug}/runs/", response=Page[RunSummary], summary="List an agent's runs",
            openapi_extra={"x-mcp-expose": True})
def list_runs(request: HttpRequest, slug: str, limit: int = 100) -> Page[RunSummary]:
    limit = clamp_limit(limit)
    agent, store = _store_for(request, slug)
    items = store.list_runs(agent.slug)
    return paginate(items, offset=0, limit=limit)


@router.post("/{slug}/runs/", response={201: RunSummary}, summary="Create a run",
             openapi_extra={"x-mcp-expose": True})
def create_run(request: HttpRequest, slug: str, payload: RunCreateIn) -> Status:
    agent, store = _store_for(request, slug)
    try:
        summary = store.create_run(
            agent.slug,
            label=payload.label,
            mode=payload.mode,
            current_step=payload.current_step,
            session_link=payload.session_link,
            steps=[s.model_dump() for s in payload.steps],
        )
    except NotImplementedError as exc:
        # Drive-backed agents don't create runs through this API (the resolver
        # seam). Surface as a clean 409 rather than a 500.
        raise HttpError(409, str(exc))
    return Status(201, summary)


@router.get("/{slug}/runs/{run_id}/", response=Run, summary="Full run read model",
            openapi_extra={"x-mcp-expose": True})
def get_run(request: HttpRequest, slug: str, run_id: str) -> Run:
    agent, store = _store_for(request, slug)
    return _run_or_404(store, agent.slug, run_id)


@router.get("/{slug}/runs/{run_id}/steps/", response=list[Step], summary="A run's steps",
            openapi_extra={"x-mcp-expose": True})
def list_steps(request: HttpRequest, slug: str, run_id: str) -> list[Step]:
    agent, store = _store_for(request, slug)
    _run_or_404(store, agent.slug, run_id)  # 404 if the run doesn't exist
    return store.list_steps(agent.slug, run_id)


@router.post("/{slug}/runs/{run_id}/steps/{step_key}/gate", response={201: Gate},
             summary="Record a gate decision on a step",
             openapi_extra={"x-mcp-expose": True})
def record_gate(request: HttpRequest, slug: str, run_id: str, step_key: str,
                payload: GateDecisionIn) -> Status:
    agent, store = _store_for(request, slug)
    decided_by = payload.decided_by or getattr(request.user, "email", "")
    try:
        gate = store.record_gate(
            agent.slug, run_id, step_key,
            decision=payload.decision, decided_by=decided_by, note=payload.note,
        )
    except (ObjectDoesNotExist, KeyError):
        raise HttpError(404, f"step '{step_key}' not found in run '{run_id}'")
    return Status(201, gate)


@router.post("/{slug}/runs/{run_id}/steps/{step_key}/verdict", response={201: Verdict},
             summary="Record a judge/QA verdict on a step",
             openapi_extra={"x-mcp-expose": True})
def record_verdict(request: HttpRequest, slug: str, run_id: str, step_key: str,
                   payload: VerdictIn) -> Status:
    agent, store = _store_for(request, slug)
    try:
        verdict = store.record_verdict(
            agent.slug, run_id, step_key,
            kind=payload.kind, score=payload.score, passed=payload.passed,
            criteria=payload.criteria, rationale=payload.rationale,
        )
    except (ObjectDoesNotExist, KeyError):
        raise HttpError(404, f"step '{step_key}' not found in run '{run_id}'")
    return Status(201, verdict)


@router.post("/{slug}/runs/{run_id}/fork", response={201: RunSummary}, summary="Fork a run",
             openapi_extra={"x-mcp-expose": True})
def fork_run(request: HttpRequest, slug: str, run_id: str, payload: ForkIn) -> Status:
    agent, store = _store_for(request, slug)
    try:
        summary = store.fork(
            agent.slug, run_id, payload.at_step,
            mode=payload.mode, edits=payload.edits,
        )
    except (ObjectDoesNotExist, KeyError):
        raise HttpError(404, f"run '{run_id}' not found for agent '{slug}'")
    except ValueError as exc:
        raise HttpError(400, str(exc))
    return Status(201, summary)
