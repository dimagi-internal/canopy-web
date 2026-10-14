"""Django Ninja routers for cloud capabilities (docs/architecture/capabilities.md).

`/api/agents/{slug}/capabilities` — what an agent declares (PUT is its deploy's
publish), and `/api/capabilities/…` — the workspace admin's grants and the
leases. Every route is also an MCP tool.

Who may call (the same doors as the rest of /api/agents, never a second rule):

- the agent's OWN canopy login (`Agent.user`) — what an agent's PAT is; or
- a member of the agent's workspace (non-members get the same 404 as a missing
  agent). Leasing a machine spends money, so a lease route also needs
  `agent.work` (editor) unless the caller is the agent itself; publishing
  needs the agent's owner/admins (`Agent.is_admin`) or the agent itself; a grant
  needs `integrations` (a workspace admin).
"""
from __future__ import annotations

import uuid

from django.db.models import Q
from django.http import HttpRequest
from ninja import Router, Status
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
    CapabilityGrantIn,
    CapabilityGrantOut,
    CapabilityLeaseIn,
    CapabilityLeaseOut,
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
    every capability its runtime.yaml declares. Each stack's Outputs are read
    now and must meet the ec2-ephemeral contract (422 naming what is missing).
    Idempotent; a capability left out is retired and its live leases released.
    For the agent's owner and admins, or the agent's own login."""
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
    param; 403 naming the grant to ask for when the workspace has not granted it."""
    agent = _agent_to_lease_for(request, payload.agent)
    try:
        lease = services.acquire(agent, payload.capability, params=payload.params,
                                 turn_id=payload.turn_id, by=request.user)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return _lease_out(lease)


@router.get("/leases/{lease_id}", response=CapabilityLeaseOut, summary="Read (and heartbeat) a lease")
def get_capability_lease(request: HttpRequest, lease_id: str) -> CapabilityLeaseOut:
    """The heartbeat: a lease nobody reads for `idle_release_s` is released.
    While provisioning, it checks the machine's ready.json (an S3 read; no SSM)."""
    lease = _lease(request, lease_id)
    return _lease_out(services.heartbeat(lease))


@router.delete("/leases/{lease_id}", response=CapabilityLeaseOut, summary="Release a lease")
def release_capability_lease(request: HttpRequest, lease_id: str) -> CapabilityLeaseOut:
    """Terminates the machine → `released`. Idempotent: an ended lease is returned as it is."""
    lease = _lease(request, lease_id)
    return _lease_out(services.release(lease))


@router.post("/leases/{lease_id}/exec", response=CapabilityExecOut, summary="Run an allowed command on a lease")
def exec_capability_lease(request: HttpRequest, lease_id: str, payload: CapabilityExecIn) -> CapabilityExecOut:
    """`argv[0]` must be on the capability's `exec.allow` (422 otherwise); the
    argv is shell-quoted and sent over SSM with its full output going to
    s3://…/leases/<id>/exec/. Returns the SSM CommandId as `exec_id` at once.
    The command sees CANOPY_LEASE_ID, CANOPY_LEASE_IN and CANOPY_LEASE_OUT."""
    lease = _lease(request, lease_id)
    try:
        exec_id, timeout_s = services.exec_(lease, payload.argv, payload.timeout_s)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityExecOut(lease_id=str(lease.pk), exec_id=exec_id, timeout_s=timeout_s)


@router.get("/leases/{lease_id}/exec/{exec_id}", response=CapabilityExecStatusOut,
            summary="Read an exec's status and output")
def get_capability_exec(request: HttpRequest, lease_id: str, exec_id: str) -> CapabilityExecStatusOut:
    """Read back from SSM every time (nothing is held in a web process), so an
    exec survives a canopy-web deploy. Full streams are presigned once done."""
    lease = _lease(request, lease_id)
    try:
        st = services.exec_status(lease, exec_id)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityExecStatusOut(**st.__dict__)


@router.post("/leases/{lease_id}/uploads", response=CapabilityUploadsOut,
             summary="Presign uploads for a lease's machine")
def create_capability_uploads(request: HttpRequest, lease_id: str,
                              payload: CapabilityUploadsIn) -> CapabilityUploadsOut:
    """Presigned PUT URLs (1 h) under s3://…/leases/<id>/in/, and the s3:// URI of
    each, to pass in an exec's argv. The machine may read in/ and nothing else
    of the lease's inputs."""
    lease = _lease(request, lease_id)
    try:
        ups = services.uploads(lease, payload.names)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityUploadsOut(lease_id=str(lease.pk), uploads=ups, expires_in_s=services.PRESIGN_TTL_S)


@router.get("/leases/{lease_id}/artifacts", response=CapabilityArtifactsOut,
            summary="List a lease's artifacts as presigned links")
def list_capability_artifacts(request: HttpRequest, lease_id: str, prefix: str = "") -> CapabilityArtifactsOut:
    """Presigned GETs (1 h) for everything the machine wrote under
    s3://…/leases/<id>/out/<prefix>. Readable after the lease ends, until the
    bucket expires it (7 days)."""
    lease = _lease(request, lease_id)
    try:
        arts = services.artifacts(lease, prefix)
    except services.CapabilityError as e:
        raise _refusal(e) from e
    return CapabilityArtifactsOut(lease_id=str(lease.pk), prefix=prefix, artifacts=arts,
                                  expires_in_s=services.PRESIGN_TTL_S)
