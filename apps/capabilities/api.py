"""Django Ninja routers for cloud capabilities (docs/architecture/capabilities.md).

`/api/agents/{slug}/capabilities` — what an agent declares (PUT is its deploy's
publish), and `/api/capabilities/…` — the workspace admin's grants and the
leases. Every route is also an MCP tool.

Who may call (the same doors as the rest of /api/agents, never a second rule):

- the agent's OWN canopy login (`Agent.user`) — what an agent's PAT is; or
- a member of the agent's workspace (non-members get the same 404 as a missing
  agent). Leasing a machine spends money, so a lease route also needs
  `agent.work` (editor) unless the caller is the agent itself; publishing (and
  re-reading a stack) needs the agent's owner/admins (`Agent.is_admin`) or the
  agent itself; a grant needs `integrations` (a workspace admin); a
  force-release and "terminate orphans now" need `integrations` or
  `Agent.is_admin`.

Long-poll: `GET /leases/{id}?wait_s=` and `GET /leases/{id}/exec/{exec_id}?wait_s=`
(0-20) hold the request until the state changes, re-checking about once a second.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Status
from ninja.errors import HttpError

from apps.agents import services as agent_services
from apps.api.auth import session_auth
from apps.events import audit
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import services
from .models import AgentCapability, CapabilityGrant, CapabilityLease
from .schemas import (
    AgentCapabilitiesIn,
    AgentCapabilitiesOut,
    AgentCapabilityOut,
    CapabilityArtifactsOut,
    CapabilityExecIn,
    CapabilityExecOut,
    CapabilityExecStatusOut,
    CapabilityForceReleaseIn,
    CapabilityGrantIn,
    CapabilityGrantOut,
    CapabilityLeaseIn,
    CapabilityLeaseListOut,
    CapabilityLeaseOut,
    CapabilityOrphansOut,
    CapabilitySpendOut,
    CapabilitySpendRow,
    CapabilityUploadsIn,
    CapabilityUploadsOut,
)

agents_router = Router(auth=session_auth, tags=["capabilities"])
router = Router(auth=session_auth, tags=["capabilities"])


# ---- who may call -------------------------------------------------------------

def _is_agent_itself(request: HttpRequest, agent) -> bool:
    return agent.user_id is not None and agent.user_id == getattr(request.user, "pk", None)


def _agent(request: HttpRequest, slug: str):
    """The agent, if the caller is it or a member of its workspace; else 404."""
    agent = agent_services.get_agent(slug)
    if agent is None or not (_is_agent_itself(request, agent)
                             or agent.workspace_id in wsvc.request_member_slugs(request)):
        raise HttpError(404, f"agent '{slug}' not found")
    return agent


def _agent_to_lease_for(request: HttpRequest, slug: str):
    agent = _agent(request, slug)
    if not (_is_agent_itself(request, agent) or perms.can(request.user, agent.workspace_id, perms.AGENT_WORK)):
        raise HttpError(403, f"leasing {slug}'s capabilities needs the editor role or above, "
                             "or the agent's own login")
    return agent


def _lease_visible(request: HttpRequest, lease_id: str) -> CapabilityLease:
    """The lease, if the caller may see its agent at all (a member, or the agent
    itself); else 404. The route then applies its own gate."""
    try:
        pk = uuid.UUID(lease_id)
    except ValueError:
        raise HttpError(404, f"lease '{lease_id}' not found") from None
    lease = (CapabilityLease.objects.select_related("agent", "capability", "workspace")
             .filter(pk=pk).first())
    if lease is None:
        raise HttpError(404, f"lease '{lease_id}' not found")
    try:
        _agent(request, lease.agent.slug)
    except HttpError:
        raise HttpError(404, f"lease '{lease_id}' not found") from None
    return lease


def _lease(request: HttpRequest, lease_id: str) -> CapabilityLease:
    try:
        pk = uuid.UUID(lease_id)
    except ValueError:
        raise HttpError(404, f"lease '{lease_id}' not found") from None
    lease = (CapabilityLease.objects.select_related("agent", "capability", "workspace")
             .filter(pk=pk).first())
    if lease is None:
        raise HttpError(404, f"lease '{lease_id}' not found")
    try:
        _agent_to_lease_for(request, lease.agent.slug)
    except HttpError as e:
        if e.status_code == 404:
            raise HttpError(404, f"lease '{lease_id}' not found") from None
        raise
    return lease


def _capability_admin(request: HttpRequest, agent) -> None:
    if not (perms.can(request.user, agent.workspace_id, perms.INTEGRATIONS) or agent.is_admin(request.user)):
        raise HttpError(403, f"this is for a workspace admin or one of {agent.slug}'s admins")


def _own_agent_ids(request: HttpRequest) -> list[int]:
    """Agents whose own login the caller IS (an agent's PAT sees its own leases)."""
    from apps.agents.models import Agent

    uid = getattr(request.user, "pk", None)
    return list(Agent.objects.filter(user_id=uid).values_list("pk", flat=True)) if uid else []


def _declared(request: HttpRequest, slug: str, name: str) -> AgentCapability:
    agent = _agent(request, slug)
    cap = (AgentCapability.objects.filter(agent=agent, name=name)
           .select_related("agent", "agent__workspace").first())
    if cap is None:
        raise HttpError(404, f"{slug} declares no capability {name!r}")
    return cap


def _refusal(e: services.CapabilityError) -> HttpError:
    return HttpError(e.status, e.detail)


# ---- serialization ------------------------------------------------------------

def _lease_out(lease: CapabilityLease) -> CapabilityLeaseOut:
    return CapabilityLeaseOut(
        lease_id=str(lease.pk), agent=lease.agent.slug, capability=lease.capability.name,
        status=lease.status, ready=lease.status == CapabilityLease.READY,
        position=services.queue_position(lease), instance_id=lease.instance_id or None,
        params=lease.params or {}, turn_id=str(lease.turn_id) if lease.turn_id else None,
        created_at=lease.created_at, provisioning_at=lease.provisioning_at, ready_at=lease.ready_at,
        ended_at=lease.ended_at, expires_at=lease.expires_at, last_heartbeat_at=lease.last_heartbeat_at,
        idle_release_s=lease.idle_release_s, reason=lease.reason, ready_info=lease.ready_info or {},
        slot=lease.slot or None, out_prefix=services.out_prefix(lease),
    )


def _grant_out(grant: CapabilityGrant) -> CapabilityGrantOut:
    return CapabilityGrantOut(
        id=grant.pk, workspace=grant.workspace_id, agent=grant.agent.slug, capability=grant.capability_name,
        max_concurrent=grant.max_concurrent, max_lease_s=grant.max_lease_s,
        created_by=getattr(grant.created_by, "email", None) or None,
        created_at=grant.created_at, updated_at=grant.updated_at,
    )


def _capability_out(cap: AgentCapability) -> AgentCapabilityOut:
    grant = services.grant_for(cap.agent, cap.name)
    live = cap.leases.filter(status__in=CapabilityLease.LIVE).select_related("agent", "capability")
    return AgentCapabilityOut(
        name=cap.name, kind=cap.kind, description=(cap.manifest or {}).get("description", ""),
        region=cap.region, stack=cap.stack, manifest=cap.manifest or {},
        stack_outputs=cap.stack_outputs or {}, published_at=cap.published_at,
        grant=_grant_out(grant) if grant else None,
        limits=services.effective_limits(cap, grant), live_leases=[_lease_out(lease) for lease in live],
    )


def _capabilities_out(agent, retired: list[str] | None = None) -> AgentCapabilitiesOut:
    caps = AgentCapability.objects.filter(agent=agent).select_related("agent")
    active = [c for c in caps if c.retired_at is None]
    if retired is None:
        retired = [c.name for c in caps if c.retired_at is not None]
    return AgentCapabilitiesOut(agent=agent.slug, capabilities=[_capability_out(c) for c in active],
                                retired=retired)


# ---- declared capabilities ----------------------------------------------------

@agents_router.get("/{slug}/capabilities", response=AgentCapabilitiesOut,
                   summary="An agent's declared capabilities, grants and live leases")
def get_agent_capabilities(request: HttpRequest, slug: str) -> AgentCapabilitiesOut:
    """What the agent declares (each capability's manifest and the stack Outputs
    resolved when it was published), whether its workspace grants each one, the
    limits an acquire gets now, and the leases holding or waiting for a machine."""
    return _capabilities_out(_agent(request, slug))


@agents_router.put("/{slug}/capabilities", response=AgentCapabilitiesOut,
                   summary="Publish an agent's declared capabilities (its whole list)")
def put_agent_capabilities(request: HttpRequest, slug: str, payload: AgentCapabilitiesIn) -> AgentCapabilitiesOut:
    """The agent's deploy calls this (`canopy-runtime publish-capabilities`) with
    every capability its runtime.yaml declares. Each ec2-ephemeral stack's
    Outputs are read now and must meet the contract (422 naming what is
    missing); a `pool` has no stack, only its `slots`. Idempotent; a capability
    left out is retired and its live leases released. A pool's live leases
    survive a change to its slots (one on a removed slot runs out; that slot is
    not handed out again). For the agent's owner and admins, or the agent's own
    login."""
    agent = _agent(request, slug)
    if not (_is_agent_itself(request, agent) or agent.is_admin(request.user)):
        raise HttpError(403, f"publishing {slug}'s capabilities is for its owner or admins, "
                             "or the agent's own login")
    before = {c.name: c.manifest for c in AgentCapability.objects.filter(agent=agent, retired_at__isnull=True)}
    try:
        _rows, retired = services.publish(agent, payload.capabilities, by=request.user)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    after = {m.name: m.model_dump(mode="json") for m in payload.capabilities}
    audit.note_change(scope=f"capabilities:{agent.slug}", before=before, after=after,
                      workspace=agent.workspace)
    return _capabilities_out(agent, retired=retired)


@agents_router.post("/{slug}/capabilities/{name}/refresh", response=AgentCapabilityOut,
                    summary="Re-read a capability's stack Outputs")
def refresh_agent_capability(request: HttpRequest, slug: str, name: str) -> AgentCapabilityOut:
    """Reads the capability's stack Outputs again (DescribeStacks) and checks them
    against the contract, as a publish would; the sweep also does this about every
    15 minutes. Use it right after updating the stack. 422 naming what is wrong if
    the stack no longer meets the contract (the old Outputs are kept). For the
    agent's owner and admins, or the agent's own login."""
    cap = _declared(request, slug, name)
    if not (_is_agent_itself(request, cap.agent) or cap.agent.is_admin(request.user)):
        raise HttpError(403, f"re-reading {slug}'s capabilities is for its owner or admins, "
                             "or the agent's own login")
    try:
        services.refresh_outputs(cap, why=f"requested by {getattr(request.user, 'email', '') or 'the agent'}",
                                 strict=True)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    cap.refresh_from_db()
    return _capability_out(cap)


@agents_router.post("/{slug}/capabilities/{name}/terminate-orphans", response=CapabilityOrphansOut,
                    summary="Terminate a capability's orphaned instances now")
def terminate_capability_orphans(request: HttpRequest, slug: str, name: str) -> CapabilityOrphansOut:
    """Runs the sweep's machine pass for one capability at once: terminates every
    instance tagged with it (and launched by this deployment) that has no live
    lease, and ends every ready lease whose machine is gone. For a workspace admin
    or the agent's admins. 502 when EC2 cannot be asked."""
    cap = _declared(request, slug, name)
    _capability_admin(request, cap.agent)
    try:
        out = services.terminate_orphans_now(cap)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityOrphansOut(agent=cap.agent.slug, capability=cap.name, **out)


# ---- grants -------------------------------------------------------------------

@router.get("/grants", response=list[CapabilityGrantOut], summary="List capability grants")
def list_capability_grants(request: HttpRequest, agent: str | None = None) -> list[CapabilityGrantOut]:
    """The grants in the caller's workspaces, optionally for one agent."""
    q = Q(workspace_id__in=wsvc.request_workspace_slugs(request))
    if agent:
        q &= Q(agent__slug=agent)
    return [_grant_out(g) for g in CapabilityGrant.objects.filter(q).select_related("agent", "created_by")]


@router.put("/grants", response=CapabilityGrantOut, summary="Grant an agent a capability (upsert)")
def set_capability_grant(request: HttpRequest, payload: CapabilityGrantIn) -> CapabilityGrantOut:
    """A workspace admin ALLOWS the agent to lease the capability in the agent's
    workspace, optionally lowering its limits (a grant may never raise them).
    Without a grant an acquire is refused with 403."""
    agent = _agent(request, payload.agent)
    if not perms.can(request.user, agent.workspace_id, perms.INTEGRATIONS):
        raise HttpError(403, "granting a capability is for a workspace admin")
    try:
        grant, before = services.set_grant(agent, payload.capability, max_concurrent=payload.max_concurrent,
                                           max_lease_s=payload.max_lease_s, by=request.user)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    audit.note_change(scope=f"capability-grant:{agent.slug}/{payload.capability}", before=before,
                      after=services.grant_state(grant), workspace=agent.workspace)
    return _grant_out(grant)


@router.delete("/grants/{grant_id}", response={204: None}, summary="Revoke a capability grant")
def delete_capability_grant(request: HttpRequest, grant_id: int):
    """Revokes the grant and releases every live lease it allowed."""
    grant = (CapabilityGrant.objects.select_related("agent", "workspace")
             .filter(pk=grant_id, workspace_id__in=wsvc.request_member_slugs(request)).first())
    if grant is None:
        raise HttpError(404, f"grant {grant_id} not found")
    if not perms.can(request.user, grant.workspace_id, perms.INTEGRATIONS):
        raise HttpError(403, "revoking a capability grant is for a workspace admin")
    before = services.grant_state(grant)
    workspace = grant.workspace
    services.revoke_grant(grant)
    audit.note_change(scope=f"capability-grant:{before['agent']}/{before['capability']}", before=before,
                      after=None, workspace=workspace)
    return Status(204, None)


# ---- leases -------------------------------------------------------------------

@router.post("/leases", response=CapabilityLeaseOut, summary="Acquire a capability lease")
def acquire_capability_lease(request: HttpRequest, payload: CapabilityLeaseIn) -> CapabilityLeaseOut:
    """Returns at once. Under the cap the machine is launched (`provisioning`;
    the launch's ClientToken is the lease id, so a retry never launches twice);
    over it the lease waits its turn (`queued`, with `position`). Poll
    GET /leases/{id} until `ready`. 422 for an undeclared capability or a bad
    param; 403 naming the grant to ask for when the workspace has not granted it.

    A `pool` capability leases a named slot, not a machine: with a slot free the
    lease is `ready` at once with `slot` (also `params.slot` and
    `ready_info.slot`); otherwise `queued` until a release frees one, oldest
    first. `params` may name one slot (`{"slot": "worker-2"}`) to wait for
    exactly that one."""
    agent = _agent_to_lease_for(request, payload.agent)
    try:
        lease = services.acquire(agent, payload.capability, params=payload.params,
                                 turn_id=payload.turn_id, by=request.user)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return _lease_out(lease)


@router.get("/leases", response=CapabilityLeaseListOut, summary="List capability leases")
def list_capability_leases(request: HttpRequest, agent: str | None = None, capability: str | None = None,
                           status: str | None = None, since: dt.datetime | None = None,
                           limit: int = Query(100, ge=1, le=500)) -> CapabilityLeaseListOut:
    """Leases in the caller's workspaces (or of the agent whose own login the
    caller is), newest first. `status` is one status or a comma-separated list
    (`live` for queued,provisioning,ready); `since` filters on `created_at`. A
    read: no heartbeat."""
    statuses = None
    if status:
        statuses = []
        for one in status.split(","):
            one = one.strip()
            statuses.extend(CapabilityLease.LIVE if one == "live" else [one])
        bad = sorted(set(statuses) - {c for c, _ in CapabilityLease.STATUS_CHOICES})
        if bad:
            raise HttpError(422, f"unknown lease status {', '.join(bad)}")
    rows = services.leases_for(wsvc.request_member_slugs(request), agent_ids=_own_agent_ids(request),
                               agent=agent, capability=capability, statuses=statuses, since=since,
                               limit=limit + 1)
    return CapabilityLeaseListOut(leases=[_lease_out(r) for r in rows[:limit]], truncated=len(rows) > limit)


@router.get("/spend", response=CapabilitySpendOut, summary="Machine time by agent and capability")
def capability_spend(request: HttpRequest, since: dt.datetime | None = None, until: dt.datetime | None = None,
                     agent: str | None = None, capability: str | None = None) -> CapabilitySpendOut:
    """Machine-seconds per agent and capability over [since, until) (default: the
    last 7 days), in the caller's workspaces. A lease counts from its launch
    (`provisioning_at`) to `ended_at`, or now while it still holds a machine,
    clipped to the window; time spent queued costs nothing."""
    until = until or timezone.now()
    since = since or until - dt.timedelta(days=7)
    if since >= until:
        raise HttpError(422, "since must be before until")
    rows = services.spend(wsvc.request_member_slugs(request), agent_ids=_own_agent_ids(request),
                          since=since, until=until, agent=agent, capability=capability)
    return CapabilitySpendOut(since=since, until=until, rows=[CapabilitySpendRow(**r) for r in rows],
                              total_machine_seconds=sum(r["machine_seconds"] for r in rows))


@router.get("/leases/{lease_id}", response=CapabilityLeaseOut, summary="Read (and heartbeat) a lease")
def get_capability_lease(request: HttpRequest, lease_id: str,
                         wait_s: int = Query(0, ge=0, le=20)) -> CapabilityLeaseOut:
    """The heartbeat: a lease nobody reads for `idle_release_s` (and that runs no
    exec) is released. While provisioning, it reads the machine's ready.json (one
    S3 read, plus one DescribeInstances while it is absent; no SSM).

    `wait_s` (0-20) long-polls: the request returns as soon as the lease leaves
    the status it had on arrival (queued → provisioning, provisioning → ready or
    failed), re-reading ready.json about once a second, else after `wait_s`."""
    lease = _lease(request, lease_id)
    return _lease_out(services.wait_for_lease(lease, wait_s))


@router.delete("/leases/{lease_id}", response=CapabilityLeaseOut, summary="Release a lease")
def release_capability_lease(request: HttpRequest, lease_id: str) -> CapabilityLeaseOut:
    """Terminates the machine → `released`. Idempotent: an ended lease is returned as it is."""
    lease = _lease(request, lease_id)
    return _lease_out(services.release(lease))


@router.post("/leases/{lease_id}/force-release", response=CapabilityLeaseOut,
             summary="Force-release a lease (admin)")
def force_release_capability_lease(request: HttpRequest, lease_id: str,
                                   payload: CapabilityForceReleaseIn) -> CapabilityLeaseOut:
    """A workspace admin's (or the agent's admins') release: ends the lease if it
    is live and terminates its machine — EVEN IF the lease already ended, for a
    machine a failed terminate left behind. `reason` lands in the lease's reason
    and the event log."""
    lease = _lease_visible(request, lease_id)
    _capability_admin(request, lease.agent)
    try:
        return _lease_out(services.force_release(lease, by=request.user, reason=payload.reason))
    except services.CapabilityError as e:
        raise _refusal(e) from e


@router.post("/leases/{lease_id}/exec", response=CapabilityExecOut, summary="Run an allowed command on a lease")
def exec_capability_lease(request: HttpRequest, lease_id: str, payload: CapabilityExecIn) -> CapabilityExecOut:
    """`argv[0]` must be on the capability's `exec.allow` (422 otherwise). The
    argv is sent over SSM through the capability stack's own exec document, which
    runs it only if argv[0] is also on the stack's allow list (a stack from before
    0.9.0: shell-quoted into AWS-RunShellScript), with its full output going to
    the lease's exec/ prefix. Returns the SSM CommandId as `exec_id` at once. The
    command sees CANOPY_LEASE_ID, CANOPY_LEASE_IN and CANOPY_LEASE_OUT. A
    running exec keeps the lease from idling out until its timeout. 422 on a
    `pool` lease: it holds a slot, not a machine."""
    lease = _lease(request, lease_id)
    try:
        exec_id, timeout_s = services.exec_(lease, payload.argv, payload.timeout_s)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityExecOut(lease_id=str(lease.pk), exec_id=exec_id, timeout_s=timeout_s)


@router.get("/leases/{lease_id}/exec/{exec_id}", response=CapabilityExecStatusOut,
            summary="Read an exec's status and output")
def get_capability_exec(request: HttpRequest, lease_id: str, exec_id: str,
                        wait_s: int = Query(0, ge=0, le=20)) -> CapabilityExecStatusOut:
    """Read back from SSM every time (nothing is held in a web process), so an
    exec survives a canopy-web deploy. Full streams are presigned once done.

    `wait_s` (0-20) long-polls: the request returns as soon as the exec is done,
    re-reading SSM about once a second, else after `wait_s`. 422 on a `pool` lease."""
    lease = _lease(request, lease_id)
    try:
        st = services.exec_status(lease, exec_id, wait_s=wait_s)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityExecStatusOut(**st.__dict__)


@router.post("/leases/{lease_id}/uploads", response=CapabilityUploadsOut,
             summary="Presign uploads for a lease's machine")
def create_capability_uploads(request: HttpRequest, lease_id: str,
                              payload: CapabilityUploadsIn) -> CapabilityUploadsOut:
    """Presigned PUT URLs (valid `expires_in_s`, up to 1 h) under the lease's in/,
    and the s3:// URI of each, to pass in an exec's argv. On a current stack in/
    is under the machine's own prefix, which no other machine can read — so
    uploads need the machine launched (409 while `instance_id` is unset). 422 on
    a `pool` lease (no machine)."""
    lease = _lease(request, lease_id)
    try:
        ups, ttl = services.uploads(lease, payload.names)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityUploadsOut(lease_id=str(lease.pk), uploads=ups, expires_in_s=ttl)


@router.get("/leases/{lease_id}/artifacts", response=CapabilityArtifactsOut,
            summary="List a lease's artifacts as presigned links")
def list_capability_artifacts(request: HttpRequest, lease_id: str, prefix: str = "") -> CapabilityArtifactsOut:
    """Presigned GETs (valid `expires_in_s`, up to 1 h) for everything the machine
    wrote under the lease's out/<prefix> (`out_prefix`). Readable after the lease
    ends, until the bucket expires it (7 days). 422 on a `pool` lease (no machine)."""
    lease = _lease(request, lease_id)
    try:
        arts, ttl = services.artifacts(lease, prefix)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityArtifactsOut(lease_id=str(lease.pk), prefix=prefix, artifacts=arts, expires_in_s=ttl)
