"""Django Ninja router for /api/agents/{slug}/schedules — the human-facing CRUD
for recurring turns.

Deliberately separate from api.py: that module is the machine control plane
(runner pairing, claim, lease), this one is the supervisor's editing surface.
Mounted on the /agents namespace exactly as agent_runs already is, so the
tenant path /api/w/{ws}/agents/... works via WorkspaceResolveMiddleware.

The handlers are thin: they resolve + serialize via schedule_services (the
request-free layer the MCP tools also call, so the two surfaces can't drift),
mapping its domain exceptions to HTTP — ScheduleNotFound -> 404,
ScheduleForbidden -> 403, and DuplicateScheduleName -> the repo's 409
uniqueness convention. The savepoint +
supersede logic lives in the service, so these handlers no longer touch the ORM.
"""
from __future__ import annotations

import datetime as dt

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, ProblemError
from apps.api.pagination import Page, clamp_limit, paginate
from apps.api.route_gates import gate
from apps.workspaces import services as wsvc

from . import schedule_services as ss
from .schemas import (
    ScheduledFireOut,
    ScheduleIn,
    ScheduleOut,
    SchedulePatch,
    SchedulePreviewIn,
    SchedulePreviewOut,
    ScheduleWeekOut,
    TriggerIn,
    TriggerOut,
    TriggerPatch,
)

router = Router(auth=session_auth, tags=["schedules"])


def _pin(request: HttpRequest) -> str | None:
    return getattr(request, "workspace_slug", None)


def _not_found(exc: ss.ScheduleNotFound) -> HttpError:
    return HttpError(404, "not found")


def _forbidden(exc: ss.ScheduleForbidden) -> HttpError:
    """A member holding too low a role. 403, and it says which role is needed —
    a refusal a viewer cannot act on is a refusal they will report as a bug."""
    if exc.required == "agent admin":
        return HttpError(403, "this action requires the agent's owner or an admin")
    return HttpError(403, f"this action requires the {exc.required} or owner role")


def _invalid(exc: ss.InvalidSchedule) -> HttpError:
    """Timing that cannot fire (e.g. a one-off in the past) -> 422, like any
    other payload the schema rejects."""
    return HttpError(422, str(exc))


def _stopped(exc: ss.TriggerStopped) -> ProblemError:
    """Run now on a trigger whose creator lost the right to it -> 409, saying why."""
    return ProblemError(409, "Trigger stopped", type_=TYPE_CONFLICT,
                        detail=f"this trigger may not fire: {exc.reason}")


def _duplicate_name(name: str) -> ProblemError:
    """uniq_agent_schedule_name -> 409, the repo's convention for a uniqueness
    violation (apps/projects/api.py, apps/workspaces/api.py)."""
    return ProblemError(
        409,
        "Schedule name already exists",
        type_=TYPE_CONFLICT,
        detail=f"A schedule named '{name}' already exists for this agent.",
    )


def _visible_workspace_ids(request: HttpRequest) -> set[str]:
    """The workspaces whose agents this caller may see — pinned to one (tenant
    URL) or spanning every membership (flat/personal) — the same set
    `apps.agents.gates.listed_agent_workspaces` returns. Purely membership:
    `Agent.workspace` is NOT NULL, so there is no NULL to admit."""
    # The week view is a LIST: the read rule, so a caller whose workspaces sit
    # in more than one tenant tree must pin one (422 otherwise).
    return wsvc.request_workspace_slugs(request)


# Route-ordering invariant: this literal "/schedules/week" route must stay
# declared BEFORE the "/{slug}/schedules/..." routes below — the same defensive
# ordering the "preview" route relies on. Ninja compiles path params with no
# int: converter and Django resolves method-agnostically, so once a {slug}
# pattern exists first, "agents/schedules/week" would bind {"slug": "schedules"}
# and this route would be unreachable. Declaration order keeps "schedules"
# literal.
@router.get("/schedules/week", response=ScheduleWeekOut,
            summary="A week of scheduled fires across the visible fleet",)
@gate("member")
def schedule_week(request: HttpRequest, start: dt.datetime, mine: bool = False) -> ScheduleWeekOut:
    """Every enabled schedule the caller can see, each with its fires in
    [start, start+7d). Scope is the URL: flat → all my workspaces; /w/{ws}/ →
    that one (WorkspaceResolveMiddleware sets request.workspace_slug).

    `mine=true` narrows to schedules the CALLER created — the actually-personal
    calendar, vs. the default 'everything in my workspaces'."""
    creator = request.user if (mine and request.user.is_authenticated) else None
    rows = ss.week_schedules(_visible_workspace_ids(request), start, created_by=creator)
    return ScheduleWeekOut(start=start, items=[ScheduledFireOut(**r) for r in rows])


@router.get("/{slug}/schedules/", response=Page[ScheduleOut],
            summary="List an agent's recurring schedules",)
@gate("member")
def list_schedules(request: HttpRequest, slug: str, limit: int = 100) -> Page[ScheduleOut]:
    try:
        schedules = ss.list_schedules(request.user, slug, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    return paginate(schedules, offset=0, limit=clamp_limit(limit),
                    map=lambda s: ScheduleOut(**ss.serialize_schedule(s)))


@router.post("/{slug}/schedules/", response={201: ScheduleOut},
             summary="Create a recurring schedule",)
@gate("agent.work", setting=True)  # enforced in schedule_services._resolve_agent
def create_schedule(request: HttpRequest, slug: str, payload: ScheduleIn) -> Status:
    try:
        schedule = ss.create_schedule(request.user, slug, payload.dict(), workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    except ss.DuplicateScheduleName as exc:
        raise _duplicate_name(exc.name) from None
    except ss.InvalidSchedule as exc:
        raise _invalid(exc) from None
    return Status(201, ScheduleOut(**ss.serialize_schedule(schedule)))


# Route-ordering invariant: this literal "preview" route must stay declared
# BEFORE the "/{slug}/schedules/{schedule_id}" routes below. Django Ninja
# compiles path params with no int: converter, so "agents/echo/schedules/preview"
# resolves as {"schedule_id": "preview"} once a {schedule_id} pattern exists —
# and Django's URL resolution is method-agnostic (POST vs PATCH/DELETE doesn't
# disambiguate), so only declaration order keeps this route reachable. Moving
# this block below PATCH/DELETE would silently shadow it.
@router.post("/{slug}/schedules/preview", response=SchedulePreviewOut,
             summary="Preview the next fire times for a cron expression",)
@gate("member", kind="action", viewer_write="POST but read-only: computes next fire times, writes nothing")
def preview_cron(
    request: HttpRequest, slug: str, payload: SchedulePreviewIn
) -> SchedulePreviewOut:
    """Answer 'when would this actually run?' at edit time. Computed with the same
    next_slots() the firing path uses — the client must never re-implement cron."""
    try:
        runs = ss.preview_cron(
            request.user, slug, payload.cron, payload.timezone, workspace_slug=_pin(request)
        )
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    return SchedulePreviewOut(next_runs=runs)


@router.patch("/{slug}/schedules/{schedule_id}", response=ScheduleOut,
              summary="Update a recurring schedule",)
@gate("agent.work", setting=True)
def update_schedule(
    request: HttpRequest, slug: str, schedule_id: int, payload: SchedulePatch
) -> ScheduleOut:
    # exclude_none as well as exclude_unset: SchedulePatch's validators are
    # `validate_cron(v) if v is not None else v`, so an explicit {"cron": null}
    # slips past validation AND counts as "set" — setattr'ing None onto a
    # non-nullable column would 500 where a 422 belongs. No AgentSchedule field
    # is nullable via PATCH, so dropping None is always the right reading.
    fields = payload.dict(exclude_unset=True, exclude_none=True)
    try:
        schedule = ss.update_schedule(request.user, slug, schedule_id, fields, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    except ss.DuplicateScheduleName as exc:
        raise _duplicate_name(exc.name) from None
    except ss.InvalidSchedule as exc:
        raise _invalid(exc) from None
    return ScheduleOut(**ss.serialize_schedule(schedule))


@router.delete("/{slug}/schedules/{schedule_id}", response={204: None},
               summary="Delete a recurring schedule",)
@gate("agent.work", setting=True)
def delete_schedule(request: HttpRequest, slug: str, schedule_id: int) -> Status:
    try:
        ss.delete_schedule(request.user, slug, schedule_id, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    return Status(204, None)


@router.post("/{slug}/schedules/{schedule_id}/run-now", response={202: ScheduleOut},
             summary="Trigger a schedule off-cycle, now",)
@gate("agent.work", kind="action")
def run_schedule_now(request: HttpRequest, slug: str, schedule_id: int) -> Status:
    try:
        schedule = ss.run_schedule_now(request.user, slug, schedule_id, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    except ss.TriggerStopped as exc:
        raise _stopped(exc) from None
    return Status(202, ScheduleOut(**ss.serialize_schedule(schedule)))


# ---- triggers: every kind on one list (docs/superpowers/specs/2026-10-27-agent-
# triggers-design.md). The /schedules/ routes above are the cron subset the runner,
# the calendar and existing tools drive; these are the agent's whole list. Both call
# the same services, so they cannot drift. Each write is gated in
# `schedule_services._check_may_set_up` (`triggers.may_set_up`): `agent.work` for a
# cron trigger, an agent admin for an inbound_mail or auto one.


@router.get("/{slug}/triggers/", response=Page[TriggerOut],
            summary="List an agent's triggers (schedules and inbound mail)")
@gate("member")
def list_triggers(request: HttpRequest, slug: str, limit: int = 100) -> Page[TriggerOut]:
    """Every trigger of the agent: `cron` (a schedule) and `inbound_mail` (mail from
    a bound sender). `stopped` says why one may not fire — its creator lost the
    right to it — and an alert stays open on the agent until that is fixed."""
    try:
        rows = ss.list_schedules(request.user, slug, workspace_slug=_pin(request), kind=None)
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    return paginate(rows, offset=0, limit=clamp_limit(limit),
                    map=lambda t: TriggerOut(**ss.serialize_trigger(t)))


@router.post("/{slug}/triggers/", response={201: TriggerOut}, summary="Create a trigger")
@gate("agent.work", "agent-admin", setting=True)  # per kind, in schedule_services
def create_trigger(request: HttpRequest, slug: str, payload: TriggerIn) -> Status:
    """A cron trigger needs `agent.work` (editor); an inbound_mail trigger, or any
    trigger with `turn_mode: auto`, needs an admin of the agent. You become its
    creator: it fires with no more authority than yours on the agent."""
    try:
        trigger = ss.create_schedule(request.user, slug, payload.dict(), workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    except ss.DuplicateScheduleName as exc:
        raise _duplicate_name(exc.name) from None
    except ss.InvalidSchedule as exc:
        raise _invalid(exc) from None
    return Status(201, TriggerOut(**ss.serialize_trigger(trigger)))


@router.patch("/{slug}/triggers/{trigger_id}", response=TriggerOut, summary="Update a trigger")
@gate("agent.work", "agent-admin", setting=True)
def update_trigger(request: HttpRequest, slug: str, trigger_id: int,
                   payload: TriggerPatch) -> TriggerOut:
    """Its kind and its creator are fixed; the rest may change, by anyone who may
    set up the trigger both as it is and as it will be."""
    fields = payload.dict(exclude_unset=True, exclude_none=True)
    try:
        trigger = ss.update_schedule(request.user, slug, trigger_id, fields,
                                     workspace_slug=_pin(request), kind=None)
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    except ss.DuplicateScheduleName as exc:
        raise _duplicate_name(exc.name) from None
    except ss.InvalidSchedule as exc:
        raise _invalid(exc) from None
    return TriggerOut(**ss.serialize_trigger(trigger))


@router.delete("/{slug}/triggers/{trigger_id}", response={204: None}, summary="Delete a trigger")
@gate("agent.work", "agent-admin", setting=True)
def delete_trigger(request: HttpRequest, slug: str, trigger_id: int) -> Status:
    try:
        ss.delete_schedule(request.user, slug, trigger_id, workspace_slug=_pin(request), kind=None)
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    return Status(204, None)


@router.post("/{slug}/triggers/{trigger_id}/run-now", response={202: TriggerOut},
             summary="Fire a cron trigger off-cycle, now")
@gate("agent.work", "agent-admin", kind="action")
def run_trigger_now(request: HttpRequest, slug: str, trigger_id: int) -> Status:
    """Cron triggers only — mail is what fires an inbound_mail trigger (404 here).
    409 when its creator lost the right to it."""
    try:
        trigger = ss.run_schedule_now(request.user, slug, trigger_id, workspace_slug=_pin(request))
    except ss.ScheduleNotFound as exc:
        raise _not_found(exc) from None
    except ss.ScheduleForbidden as exc:
        raise _forbidden(exc) from None
    except ss.TriggerStopped as exc:
        raise _stopped(exc) from None
    return Status(202, TriggerOut(**ss.serialize_trigger(trigger)))
