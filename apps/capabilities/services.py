"""The capability lease lifecycle: publish, grant, acquire, heartbeat, exec, release, sweep.

    queued → provisioning → ready → released
                 │            │
                 └─ failed ←──┤
                    expired ←─┘   (and queued → released, when abandoned)

Rules this module keeps:

- **A lease's id is its launch's ClientToken.** The slot is reserved in the DB
  first (→ provisioning), and the launch happens after that commit; a crash or a
  timeout between the two is repaired by calling launch again, which EC2 answers
  with the same instance.
- **A ready lease never goes back to provisioning.** A sick machine is released
  and another acquired (restore, don't adapt).
- **No process state.** SSM holds an exec's state, S3 a machine's readiness,
  EC2 the machines; the DB holds the leases. A deploy mid-anything loses nothing.
- **No AWS call under a row lock.** `promote` counts EC2's tagged instances
  before it locks the capability; inside the lock it reads only the DB.
- **Layered cost guards, each independent of the ones before it:** explicit
  release; the bound turn ending, or no lease call for idle_release_s (a running
  exec counts as activity until its timeout); the sweep — on its own schedule in
  every web process (scheduler.py) and on the claim tick, deduplicated
  fleet-wide through the cache and still run if the cache is down — which
  expires, idles, promotes and kills every tagged instance with no live lease;
  the machine's own halt at max_lifetime_s (a halt terminates); and the stack's
  backstop Lambda, which terminates any of the capability's instances older
  than MaxInstanceLifetimeSeconds even with canopy-web gone.
- **Every transition is an `events.record` row** (`capability.lease.<status>`),
  and so is every sweep failure (`capability.sweep.*`).
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from canopy_runtime.schema import CapabilityManifest
from canopy_sdk.ondemand import TAG_BROKER, LaunchFailed
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from pydantic import ValidationError

from . import aws
from .models import AgentCapability, CapabilityGrant, CapabilityLease

logger = logging.getLogger(__name__)

SOURCE = "capabilities"
KIND = "ec2-ephemeral"
#: The sweep runs at most once per this many seconds across the fleet (a cache
#: key every web process and claim tick tries to add). A little under the
#: scheduler's interval, so one process's tick is never skipped by its own key.
SWEEP_EVERY_S = 25
SWEEP_CACHE_KEY = "capabilities:sweep:last"
#: When the last sweep FINISHED. The claim tick runs the sweep only when this is
#: older than SWEEP_STALE_S — i.e. when the scheduler is not keeping up — so in
#: the normal case no AWS call ever rides a runner's claim request.
SWEEP_RAN_KEY = "capabilities:sweep:ran"
SWEEP_STALE_S = 90
#: Re-read a capability's stack Outputs at most this often (from the sweep).
OUTPUTS_REFRESH_S = 900
#: Presigned URLs (uploads, artifacts, exec output) live at most this long —
#: less when the signing credentials expire sooner (aws.Broker.presign_ttl).
PRESIGN_TTL_S = 3600
STDOUT_TAIL = 4000
MAX_EXEC_TIMEOUT_S = 172800
#: How long after SendCommand an invocation SSM cannot see yet still reads as Pending.
INVOCATION_GRACE_S = 120
MAX_UPLOADS = 50
MAX_ARTIFACTS = 1000
#: Long-poll: the most a GET may wait, and how often it looks while it does.
MAX_WAIT_S = 20
POLL_S = 1.0
#: How long a retired capability's stack is still swept for orphans.
RETIRED_SWEEP_DAYS = 3
_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_PREFIX = re.compile(r"^([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]*$")

#: The sleep the long-polls use; tests replace it.
_sleep = time.sleep
_monotonic = time.monotonic


class CapabilityError(Exception):
    """A refusal the API turns into `status` with `detail` as the message."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---- reading a published capability ------------------------------------------

def manifest_of(cap: AgentCapability) -> CapabilityManifest:
    return CapabilityManifest.model_validate(cap.manifest)


def target(cap: AgentCapability) -> aws.Target:
    o = cap.stack_outputs or {}
    return aws.Target(region=cap.region, capability=cap.name, owner=o.get("Owner", ""),
                      launch_template_id=o.get("LaunchTemplateId", ""),
                      bucket=o.get("ArtifactsBucketName", ""), role_id=o.get("InstanceRoleId", ""),
                      lease_object_scope=o.get("LeaseObjectScope", ""),
                      exec_document=o.get("ExecDocumentName", ""))


def broker(cap: AgentCapability) -> aws.Broker:
    return aws.get(cap.region)


def broker_id() -> str:
    """This deployment's `canopy:broker` tag: CAPABILITY_BROKER_ID, else the host
    of CANOPY_IDENTITY_BASE_URL (canopy.dimagi.com in production)."""
    explicit = getattr(settings, "CAPABILITY_BROKER_ID", "") or ""
    if explicit:
        return explicit
    host = urlparse(getattr(settings, "CANOPY_IDENTITY_BASE_URL", "") or "").hostname
    return host or "canopy-web"


def _ours(d) -> bool:
    """An instance this deployment launched — or one launched before the broker
    tagged itself (0.9.0), when only one broker existed. Another broker's
    machine is never counted against this one's cap, and never killed."""
    tag = (d.tags or {}).get(TAG_BROKER, "")
    return tag in ("", broker_id())


def grant_for(agent, name: str) -> CapabilityGrant | None:
    return CapabilityGrant.objects.filter(agent=agent, workspace_id=agent.workspace_id,
                                          capability_name=name).first()


def effective_limits(cap: AgentCapability, grant: CapabilityGrant | None) -> dict:
    """The manifest's limits, lowered (never raised) by the grant."""
    limits = manifest_of(cap).limits.model_dump()
    if grant is not None:
        if grant.max_concurrent:
            limits["max_concurrent"] = min(limits["max_concurrent"], grant.max_concurrent)
        if grant.max_lease_s:
            limits["max_lease_s"] = min(limits["max_lease_s"], grant.max_lease_s)
            limits["idle_release_s"] = min(limits["idle_release_s"], limits["max_lease_s"])
    return limits


def active_capability(agent, name: str) -> AgentCapability | None:
    return AgentCapability.objects.filter(agent=agent, name=name, retired_at__isnull=True).first()


# ---- events -------------------------------------------------------------------

def _event(lease: CapabilityLease, kind: str, summary: str, *, level: str = "info", key: str = "",
           **payload) -> None:
    from apps.events import services as events

    try:
        events.record([{
            "source": SOURCE, "kind": f"capability.{kind}", "level": level, "summary": summary[:500],
            "key": key,
            "payload": {"lease_id": str(lease.pk), "agent": lease.agent.slug,
                        "capability": lease.capability.name, "instance_id": lease.instance_id,
                        "status": lease.status, **payload},
        }], workspace=lease.workspace)
    except Exception:  # noqa: BLE001 — the audit row must never undo the transition
        logger.exception("could not record capability event %s for %s", kind, lease.pk)


def _cap_event(cap: AgentCapability, kind: str, summary: str, *, level: str = "info", key: str = "",
               **payload) -> None:
    from apps.events import services as events

    try:
        events.record([{"source": SOURCE, "kind": f"capability.{kind}", "level": level,
                        "summary": summary[:500], "key": key,
                        "payload": {"agent": cap.agent.slug, "capability": cap.name, **payload}}],
                      workspace=cap.agent.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("could not record capability event %s for %s", kind, cap.pk)


# ---- publish / refresh --------------------------------------------------------

def _resolve_outputs(agent, m: CapabilityManifest, outputs: dict) -> dict:
    """The contract check on a stack's Outputs for manifest `m`; returns the
    outputs canopy-web keeps. Raises CapabilityError(422) naming what is wrong."""
    missing = [k for k in (*aws.REQUIRED_OUTPUTS, *aws.CHECKED_OUTPUTS) if not outputs.get(k)]
    if missing:
        raise CapabilityError(422, (
            f"{m.name}: stack {m.aws.stack!r} lacks the ec2-ephemeral contract output(s) "
            f"{', '.join(missing)}. Deploy it from canopy's ondemand-capability template with "
            "Mode=ephemeral (docs/architecture/capabilities.md)."))
    if outputs["Capability"] != m.name:
        raise CapabilityError(422, f"{m.name}: stack {m.aws.stack!r} is for capability "
                                   f"{outputs['Capability']!r}, not {m.name!r}")
    if outputs["Owner"] != agent.slug:
        raise CapabilityError(422, f"{m.name}: stack {m.aws.stack!r} belongs to {outputs['Owner']!r}, "
                                   f"not {agent.slug!r}; an agent may only publish its own stacks")
    if outputs.get("ExecDocumentName"):
        allowed = {a for a in (outputs.get("ExecAllow") or "").split(",") if a}
        extra = [a for a in m.exec.allow if a not in allowed]
        if extra:
            raise CapabilityError(422, (
                f"{m.name}: the manifest's exec.allow names {', '.join(extra)}, which stack "
                f"{m.aws.stack!r}'s exec document will not run (its ExecAllow is "
                f"{outputs.get('ExecAllow') or 'empty'!r}). Redeploy the stack with "
                f"ExecAllow={','.join(m.exec.allow)}."))
    lifetime = outputs.get("MaxInstanceLifetimeSeconds")
    if lifetime and str(lifetime).isdigit() and m.limits.max_lease_s > int(lifetime):
        raise CapabilityError(422, (
            f"{m.name}: limits.max_lease_s {m.limits.max_lease_s} is longer than stack "
            f"{m.aws.stack!r}'s MaxInstanceLifetimeSeconds {lifetime}: its backstop would "
            "terminate a lease's machine while the lease is still live. Raise the stack "
            "parameter or lower the manifest."))
    keep = (*aws.REQUIRED_OUTPUTS, *aws.CHECKED_OUTPUTS, *aws.OPTIONAL_OUTPUTS)
    return {k: outputs[k] for k in keep if outputs.get(k)}


def _read_outputs(m: CapabilityManifest) -> dict:
    try:
        return aws.get(m.aws.region).stack_outputs(m.aws.stack)
    except aws.ERRORS as e:
        raise CapabilityError(422, (
            f"{m.name}: {e}. canopy-web reads the stack with the permissions the stack itself "
            "grants it (BrokerPolicy, via BrokerRoleName): is it deployed in that region with "
            "Mode=ephemeral and canopy-web's task role as BrokerRoleName?")) from e


def publish(agent, manifests: list[CapabilityManifest], *, by) -> tuple[list[AgentCapability], list[str]]:
    """Replace the agent's declared capabilities with `manifests`.

    Each stack's Outputs are read now (DescribeStacks) and checked against the
    ec2-ephemeral contract; nothing is written unless every one passes. A
    capability the list leaves out is retired and its live leases released.
    Idempotent. Returns (the published rows, the names this call retired)."""
    names = [m.name for m in manifests]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise CapabilityError(422, f"capability names must be unique; repeated: {', '.join(dupes)}")
    resolved = [(m, _resolve_outputs(agent, m, _read_outputs(m))) for m in manifests]

    now = timezone.now()
    with transaction.atomic():
        rows = []
        for m, outputs in resolved:
            row, _ = AgentCapability.objects.update_or_create(
                agent=agent, name=m.name,
                defaults={"kind": m.kind, "manifest": m.model_dump(mode="json"), "region": m.aws.region,
                          "stack": m.aws.stack, "stack_outputs": outputs, "outputs_refreshed_at": now,
                          "published_at": now,
                          "published_by": by if getattr(by, "is_authenticated", False) else None,
                          "retired_at": None},
            )
            rows.append(row)
        retiring = list(AgentCapability.objects.filter(agent=agent, retired_at__isnull=True)
                        .exclude(name__in=names))
        AgentCapability.objects.filter(pk__in=[c.pk for c in retiring]).update(retired_at=now)
    for cap in retiring:
        _cap_event(cap, "retired", f"{agent.slug}/{cap.name} retired: no longer declared")
        for lease in cap.leases.filter(status__in=CapabilityLease.LIVE):
            release(lease, reason="capability retired: no longer declared by its agent")
    for row in rows:
        _cap_event(row, "published", f"{agent.slug}/{row.name} published (stack {row.stack})")
        promote(row)
    return rows, [c.name for c in retiring]


def refresh_outputs(cap: AgentCapability, *, why: str, strict: bool = False) -> bool:
    """Re-read the stack's Outputs (a stack update can change them: a 0.9.0
    upgrade adds the per-instance layout and the exec document). Keeps the old
    outputs, and records a warning, when the stack no longer meets the contract
    (`strict`: and raises that CapabilityError). True when they changed."""
    m = manifest_of(cap)
    try:
        outputs = _resolve_outputs(cap.agent, m, _read_outputs(m))
    except CapabilityError as e:
        if strict:
            raise
        _cap_event(cap, "outputs.unreadable", f"{cap.agent.slug}/{cap.name}: could not re-read the "
                   f"stack ({why}): {e.detail}", level="warn", key=f"outputs:{cap.pk}")
        return False
    changed = outputs != (cap.stack_outputs or {})
    now = timezone.now()
    AgentCapability.objects.filter(pk=cap.pk).update(stack_outputs=outputs, outputs_refreshed_at=now)
    cap.stack_outputs, cap.outputs_refreshed_at = outputs, now
    if changed:
        _cap_event(cap, "outputs.refreshed", f"{cap.agent.slug}/{cap.name}: stack outputs changed ({why})",
                   outputs=outputs)
    return changed


# ---- acquire ------------------------------------------------------------------

def acquire(agent, name: str, *, params: dict | None, turn_id=None, by=None) -> CapabilityLease:
    cap = active_capability(agent, name)
    if cap is None:
        declared = sorted(AgentCapability.objects.filter(agent=agent, retired_at__isnull=True)
                          .values_list("name", flat=True))
        raise CapabilityError(422, (
            f"{agent.slug} declares no capability {name!r} (declared: {', '.join(declared) or 'none'}). "
            "Declare it in runtime.yaml and run `canopy-runtime publish-capabilities`."))
    if cap.kind != KIND:
        raise CapabilityError(422, f"capability kind {cap.kind!r} is not brokered (only {KIND!r})")
    grant = grant_for(agent, name)
    if grant is None:
        raise CapabilityError(403, (
            f"{agent.slug} has no grant for capability {name!r} in workspace {agent.workspace_id}. "
            f"A workspace admin must grant it: PUT /api/capabilities/grants "
            f'{{"agent": "{agent.slug}", "capability": "{name}"}}.'))
    try:
        resolved = manifest_of(cap).resolve_params(params)
    except ValueError as e:
        raise CapabilityError(422, f"{name}: {e}") from e
    turn = _turn_for(agent, turn_id)
    limits = effective_limits(cap, grant)
    now = timezone.now()
    lease_id = uuid.uuid4()
    lease = CapabilityLease.objects.create(
        id=lease_id, workspace_id=agent.workspace_id, agent=agent, capability=cap,
        status=CapabilityLease.QUEUED, params=resolved, turn=turn,
        requested_by=by if getattr(by, "is_authenticated", False) else None,
        max_lease_s=limits["max_lease_s"], idle_release_s=limits["idle_release_s"],
        ready_timeout_s=limits["ready_timeout_s"], last_heartbeat_at=now,
        # Knowable now only on an older stack (leases/<id>); a 0.9.0 stack's is
        # set when the machine is recorded, since it names the machine.
        s3_base=target(cap).lease_base(str(lease_id), ""),
    )
    _event(lease, "lease.queued", f"{agent.slug}/{name} lease requested", turn_id=str(turn.pk) if turn else None)
    promote(cap)
    lease.refresh_from_db()
    return lease


def _turn_for(agent, turn_id):
    if not turn_id:
        return None
    from apps.harness.models import Turn

    try:
        turn = Turn.objects.select_related("agent", "chat_session__agent").get(pk=turn_id)
    except (Turn.DoesNotExist, ValueError, ValidationError):
        raise CapabilityError(422, f"turn {turn_id} does not exist") from None
    acting = turn.acting_agent
    if acting is None or acting.pk != agent.pk:
        raise CapabilityError(422, f"turn {turn_id} is not one of {agent.slug}'s turns")
    if turn.status in Turn.TERMINAL:
        raise CapabilityError(422, f"turn {turn_id} has already ended ({turn.status})")
    return turn


def queue_position(lease: CapabilityLease) -> int | None:
    if lease.status != CapabilityLease.QUEUED:
        return None
    return CapabilityLease.objects.filter(capability_id=lease.capability_id, status=CapabilityLease.QUEUED,
                                          created_at__lte=lease.created_at).count()


# ---- the queue ----------------------------------------------------------------

def _tagged(cap: AgentCapability, b: aws.Broker, t: aws.Target) -> list | None:
    """This deployment's tagged instances of `cap`; None when EC2 cannot be asked."""
    try:
        return [d for d in b.list_tagged(t) if _ours(d)]
    except aws.ERRORS as e:
        logger.warning("capability %s: cannot count tagged instances: %s", cap.pk, e)
        return None


def _holding_count(cap: AgentCapability, tagged: list) -> int:
    """Machines held against the cap: holding leases (read from the DB, so it
    must run under the capability's lock), plus any tagged instance no holding
    lease accounts for — an orphan still costs a slot until the sweep kills it.
    An instance whose lease already ended is not counted: it was terminated a
    moment ago, and counting it would hold the queue for one more sweep."""
    holding = {str(pk): iid for pk, iid in cap.leases.filter(status__in=CapabilityLease.HOLDING)
               .values_list("pk", "instance_id")}
    known_instances = {iid for iid in holding.values() if iid}
    extra = [d for d in tagged if d.lease_id not in holding and d.instance_id not in known_instances]
    ended = {str(pk) for pk in CapabilityLease.objects.filter(
        pk__in=[d.lease_id for d in extra if _is_uuid(d.lease_id)],
        status__in=CapabilityLease.TERMINAL).values_list("pk", flat=True)}
    return len(holding) + len([d for d in extra if d.lease_id not in ended])


def promote(cap: AgentCapability) -> list[CapabilityLease]:
    """Move the oldest queued leases to provisioning while under the cap (FIFO),
    then launch each. Safe to call any time, from any process: serialized per
    capability by a row lock, and EC2 is asked BEFORE the lock is taken — the
    count inside it reads holding leases from the DB, so a launch another
    process made after our EC2 read is still counted."""
    if cap.retired_at is not None or not CapabilityLease.objects.filter(
            capability=cap, status=CapabilityLease.QUEUED).exists():
        return []
    grant = grant_for(cap.agent, cap.name)
    if grant is None:
        return []
    b, t = broker(cap), target(cap)
    tagged = _tagged(cap, b, t)
    if tagged is None:
        return []
    now = timezone.now()
    with transaction.atomic():
        AgentCapability.objects.select_for_update().filter(pk=cap.pk).first()
        queued = list(CapabilityLease.objects.filter(capability=cap, status=CapabilityLease.QUEUED)
                      .order_by("created_at"))
        free = effective_limits(cap, grant)["max_concurrent"] - _holding_count(cap, tagged)
        promoted = []
        for lease in queued[:max(0, free)]:
            if CapabilityLease.objects.filter(pk=lease.pk, status=CapabilityLease.QUEUED).update(
                    status=CapabilityLease.PROVISIONING, provisioning_at=now, reason="",
                    expires_at=now + dt.timedelta(seconds=lease.max_lease_s)):
                lease.refresh_from_db()
                promoted.append(lease)
                _event(lease, "lease.provisioning", f"{cap.agent.slug}/{cap.name} lease launching")
    for lease in promoted:
        _launch(lease)
    return promoted


def _launch(lease: CapabilityLease) -> None:
    """RunInstances for a provisioning lease. Idempotent (ClientToken=lease id),
    so it is also how a launch that never recorded its instance is repaired."""
    cap = lease.capability
    t = target(cap)
    user_data = {"lease_id": str(lease.pk), "params": lease.params, "max_lifetime_s": lease.max_lease_s,
                 "bucket": t.bucket}
    try:
        instance_id = broker(cap).launch(t, lease_id=str(lease.pk), user_data=user_data,
                                         tags={TAG_BROKER: broker_id()})
    except LaunchFailed as e:
        if not e.transient:
            end(lease, CapabilityLease.FAILED, f"launch refused: {e}", terminate=False)
            return
        _retrying(lease, e)
        return
    except aws.ERRORS as e:
        _retrying(lease, e)
        return
    _record_instance(lease, t, instance_id, adopted=False)


def _record_instance(lease: CapabilityLease, t: aws.Target, instance_id: str, *, adopted: bool) -> bool:
    """Write a provisioning lease's instance id (and, on a 0.9.0 stack, the
    object prefix it names). A lease that ended while the launch was in flight
    has no use for the machine: terminate it."""
    cap = lease.capability
    fields = {"instance_id": instance_id, "reason": ""}
    base = lease.s3_base or t.lease_base(str(lease.pk), instance_id)
    if base:
        fields["s3_base"] = base
    q = CapabilityLease.objects.filter(pk=lease.pk, status=CapabilityLease.PROVISIONING)
    if adopted:
        q = q.filter(instance_id="")
    if not q.update(**fields):
        if not adopted and not CapabilityLease.objects.filter(
                pk=lease.pk, status__in=CapabilityLease.HOLDING, instance_id=instance_id).exists():
            # Released while the launch was in flight: the machine has no lease.
            try:
                broker(cap).terminate(t, [instance_id])
            except aws.ERRORS:
                logger.warning("lease %s: could not terminate %s (the sweep will)", lease.pk, instance_id)
        return False
    if lease.instance_id != instance_id:
        lease.instance_id = instance_id
        lease.s3_base = fields.get("s3_base", lease.s3_base)
        verb = "adopted" if adopted else "launched"
        _event(lease, "lease.launched", f"{cap.agent.slug}/{cap.name} instance {instance_id} {verb}")
    return True


def _retrying(lease: CapabilityLease, err: Exception) -> None:
    reason = f"launch retrying: {err}"[:500]
    previous = CapabilityLease.objects.filter(pk=lease.pk).values_list("reason", flat=True).first() or ""
    CapabilityLease.objects.filter(pk=lease.pk).update(reason=reason)
    logger.warning("lease %s: launch will be retried: %s", lease.pk, err)
    if previous != reason:  # one event per distinct failure, not one per sweep
        lease.reason = reason
        _event(lease, "lease.launch_retry", f"{lease.agent.slug}/{lease.capability.name} launch will be "
               f"retried: {err}", level="warn", error=str(err)[:500])


# ---- heartbeat / readiness ----------------------------------------------------

def heartbeat(lease: CapabilityLease) -> CapabilityLease:
    """GET on a lease: proves the holder is alive and, while provisioning, looks
    for the machine's ready.json (one S3 read, and one DescribeInstances while
    it is absent; no SSM, no worker)."""
    _touch(lease)
    if lease.status == CapabilityLease.PROVISIONING:
        check_ready(lease)
    lease.refresh_from_db()
    return lease


def _touch(lease: CapabilityLease) -> None:
    if lease.status in CapabilityLease.LIVE:
        CapabilityLease.objects.filter(pk=lease.pk, status__in=CapabilityLease.LIVE).update(
            last_heartbeat_at=timezone.now())


def wait_for_lease(lease: CapabilityLease, wait_s: int) -> CapabilityLease:
    """The heartbeat, long-polled: with `wait_s` (≤ MAX_WAIT_S) it returns as
    soon as the lease leaves the status it had when the request arrived (queued
    → provisioning, provisioning → ready or failed), else after `wait_s`. While
    it waits it reads ready.json about once a second — no DescribeInstances in
    the loop; the full check runs once at the start."""
    lease = heartbeat(lease)
    wait_s = max(0, min(int(wait_s or 0), MAX_WAIT_S))
    if not wait_s:
        return lease
    entered = lease.status
    deadline = _monotonic() + wait_s
    while lease.status == entered and entered in (CapabilityLease.QUEUED, CapabilityLease.PROVISIONING):
        if _monotonic() >= deadline:
            break
        _sleep(POLL_S)
        if lease.status == CapabilityLease.PROVISIONING:
            check_ready(lease, describe=False)
        lease.refresh_from_db()
    _touch(lease)
    lease.refresh_from_db()
    return lease


def check_ready(lease: CapabilityLease, *, describe: bool = True) -> None:
    """One readiness check of a provisioning lease. `describe=False` (the
    long-poll's loop) reads ready.json and the deadline only: no
    DescribeInstances, no launch retry."""
    lease.refresh_from_db()
    if lease.status != CapabilityLease.PROVISIONING:
        return
    cap = lease.capability
    b, t = broker(cap), target(cap)
    if not lease.instance_id and describe:
        _launch(lease)
        lease.refresh_from_db()
    if lease.status != CapabilityLease.PROVISIONING:
        return
    if lease.instance_id:
        try:
            info = b.read_json(t.bucket, t.ready_key(lease.instance_id))
        except aws.ERRORS as e:
            logger.warning("lease %s: cannot read ready.json: %s", lease.pk, e)
            info = None
        if info is not None:
            if info.get("ok") is True:
                updated = CapabilityLease.objects.filter(pk=lease.pk, status=CapabilityLease.PROVISIONING).update(
                    status=CapabilityLease.READY, ready_at=timezone.now(), ready_info=info, reason="")
                if updated:
                    lease.refresh_from_db()
                    _event(lease, "lease.ready", f"{cap.agent.slug}/{cap.name} ready on {lease.instance_id}",
                           timings=info.get("timings"), cli_version=info.get("cli_version"))
                return
            CapabilityLease.objects.filter(pk=lease.pk).update(ready_info=info)
            end(lease, CapabilityLease.FAILED,
                f"the machine reported it is not ready: {info.get('reason') or 'no reason given'}")
            return
        if describe:
            try:
                d = b.describe(t, lease.instance_id)
            except aws.ERRORS:
                d = False  # unknown; decide on the deadline alone
            if d is None or (d and d.state in ("shutting-down", "terminated")):
                end(lease, CapabilityLease.FAILED,
                    f"instance {lease.instance_id} is {'gone' if d is None else d.state} before it was ready",
                    terminate=False)
                return
    started = lease.provisioning_at or lease.created_at
    if timezone.now() > started + dt.timedelta(seconds=lease.ready_timeout_s):
        # The stack may have changed under the capability (a bucket, a role id):
        # re-read it, so the NEXT lease looks in the right place.
        _safe(refresh_outputs, cap, why="a lease was not ready in time")
        end(lease, CapabilityLease.FAILED, f"not ready after {lease.ready_timeout_s}s")


# ---- ending -------------------------------------------------------------------

def end(lease: CapabilityLease, status: str, reason: str, *, terminate: bool = True,
        promote_next: bool = True) -> bool:
    """Move a live lease to a terminal status, terminate its machine, and let the
    next queued lease in. False if it had already ended (idempotent)."""
    now = timezone.now()
    updated = CapabilityLease.objects.filter(pk=lease.pk, status__in=CapabilityLease.LIVE).update(
        status=status, ended_at=now, reason=reason[:500])
    lease.refresh_from_db()
    if not updated:
        return False
    level = "warn" if status == CapabilityLease.FAILED else "info"
    _event(lease, f"lease.{status}", f"{lease.agent.slug}/{lease.capability.name} lease {status}: {reason}",
           level=level, reason=reason)
    if terminate and lease.instance_id:
        try:
            broker(lease.capability).terminate(target(lease.capability), [lease.instance_id])
        except aws.ERRORS as e:
            # The machine has no live lease now, so the sweep's orphan pass kills it.
            logger.warning("lease %s: terminate %s failed (the sweep will retry): %s",
                           lease.pk, lease.instance_id, e)
    if promote_next:
        promote(lease.capability)
    return True


def release(lease: CapabilityLease, *, reason: str = "released by its holder") -> CapabilityLease:
    end(lease, CapabilityLease.RELEASED, reason)
    return lease


def force_release(lease: CapabilityLease, *, by, reason: str = "") -> CapabilityLease:
    """An admin's release: ends the lease if it is live, and terminates its
    machine EVEN IF the lease already ended (a terminate that failed, an
    instance a crash left behind)."""
    who = getattr(by, "email", "") or "an admin"
    why = f"force-released by {who}" + (f": {reason}" if reason else "")
    ended = end(lease, CapabilityLease.RELEASED, why)
    if not ended and lease.instance_id:
        try:
            broker(lease.capability).terminate(target(lease.capability), [lease.instance_id])
        except aws.ERRORS as e:
            raise CapabilityError(502, f"could not terminate {lease.instance_id}: {e}") from e
        _event(lease, "lease.force_terminated", f"{lease.agent.slug}/{lease.capability.name}: {why} "
               f"(already {lease.status}; instance {lease.instance_id} terminated again)", level="warn")
    return lease


def release_for_turn(turn_id) -> int:
    """Release every live lease bound to `turn_id` (its turn reached a terminal status)."""
    n = 0
    for lease in CapabilityLease.objects.filter(turn_id=turn_id, status__in=CapabilityLease.LIVE):
        n += end(lease, CapabilityLease.RELEASED, "its turn ended")
    return n


def terminate_for_deleted_row(lease: CapabilityLease) -> None:
    """A lease row is being deleted while it holds a machine (its agent,
    workspace or capability is going): terminate the machine first. Records no
    event — the rows an event would hang off are being deleted with it. A
    failure leaves the machine to the stack's backstop Lambda."""
    if lease.status not in CapabilityLease.HOLDING or not lease.instance_id:
        return
    try:
        cap = AgentCapability.objects.get(pk=lease.capability_id)
        broker(cap).terminate(target(cap), [lease.instance_id])
        logger.warning("lease %s deleted while %s: terminated %s", lease.pk, lease.status, lease.instance_id)
    except Exception:  # noqa: BLE001 — a delete must not depend on AWS
        logger.exception("lease %s deleted while %s: could not terminate %s (the stack's backstop will)",
                         lease.pk, lease.status, lease.instance_id)


# ---- activity -----------------------------------------------------------------

@dataclass
class ExecEntry:
    sent_at: dt.datetime
    timeout_s: int
    done: bool


def exec_entries(lease: CapabilityLease) -> dict[str, ExecEntry]:
    """`lease.execs`, read in either shape: {id: {sent_at, timeout_s, done}}, or
    a bare ISO sent-at (rows from before 0.9.0, read as timeout 0)."""
    out: dict[str, ExecEntry] = {}
    for cid, raw in (lease.execs or {}).items():
        try:
            if isinstance(raw, str):
                out[cid] = ExecEntry(dt.datetime.fromisoformat(raw), 0, False)
            elif isinstance(raw, dict):
                out[cid] = ExecEntry(dt.datetime.fromisoformat(raw["sent_at"]), int(raw.get("timeout_s") or 0),
                                     bool(raw.get("done")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def last_activity(lease: CapabilityLease) -> dt.datetime:
    """The later of the last lease call and the end of any exec not known to be
    done: an exec that runs for an hour is not an idle lease, even if its holder
    waits for it without polling."""
    latest = lease.last_heartbeat_at
    for e in exec_entries(lease).values():
        if not e.done:
            latest = max(latest, e.sent_at + dt.timedelta(seconds=e.timeout_s))
    return latest


# ---- exec ---------------------------------------------------------------------

def _base(lease: CapabilityLease) -> str:
    if lease.s3_base:
        return lease.s3_base
    return target(lease.capability).lease_base(str(lease.pk), lease.instance_id)


def out_prefix(lease: CapabilityLease) -> str:
    """s3://<bucket>/<base>/out/ — CANOPY_LEASE_OUT, as a literal for argv. ""
    until it is knowable (a 0.9.0 stack's lease before its machine launches)."""
    base = _base(lease)
    return f"s3://{target(lease.capability).bucket}/{base}/out/" if base else ""


def _encode_argv(argv: list[str]) -> str:
    """The exec document's Argv: every argument NUL-terminated, base64'd."""
    return base64.b64encode(b"".join(a.encode() + b"\0" for a in argv)).decode()


def exec_(lease: CapabilityLease, argv: list[str], timeout_s: int) -> tuple[str, int]:
    """Send `argv` to the lease's machine; return (the SSM CommandId, the timeout used).

    argv[0] must be exactly one of the manifest's `exec.allow`. On a 0.9.0 stack
    the argv goes to the stack's exec document, base64-encoded (no argument is
    ever script text), and the document checks argv[0] against the stack's own
    allow list as well; on an older stack it is shell-quoted into
    AWS-RunShellScript."""
    if lease.status != CapabilityLease.READY:
        raise CapabilityError(409, f"lease {lease.pk} is {lease.status}; exec needs a ready lease")
    allow = manifest_of(lease.capability).exec.allow
    if not argv or not all(isinstance(a, str) for a in argv):
        raise CapabilityError(422, "argv must be a non-empty list of strings")
    if argv[0] not in allow:
        raise CapabilityError(422, (f"argv[0] {argv[0]!r} is not allowed on {lease.capability.name}; "
                                    f"its manifest allows: {', '.join(allow)}"))
    if any("\x00" in a for a in argv) or sum(len(a) for a in argv) > 32000:
        raise CapabilityError(422, "argv has a NUL byte or is over 32000 characters")
    if timeout_s < 1 or timeout_s > MAX_EXEC_TIMEOUT_S:
        raise CapabilityError(422, f"timeout_s must be between 1 and {MAX_EXEC_TIMEOUT_S}")
    if lease.expires_at:
        remaining = int((lease.expires_at - timezone.now()).total_seconds())
        timeout_s = max(1, min(timeout_s, remaining))
    t = target(lease.capability)
    base = _base(lease)
    lease_in = f"s3://{t.bucket}/{base}/in/"
    output_prefix = f"{base}/exec"
    try:
        if t.exec_document:
            command_id = broker(lease.capability).run_document(t, lease.instance_id, {
                "Argv": [_encode_argv(argv)], "LeaseId": [str(lease.pk)], "LeaseIn": [lease_in],
                "LeaseOut": [out_prefix(lease)], "ExecutionTimeout": [str(timeout_s)],
            }, timeout_s=timeout_s, output_prefix=output_prefix)
        else:
            script = [
                f"export CANOPY_LEASE_ID={shlex.quote(str(lease.pk))}",
                f"export CANOPY_LEASE_IN={shlex.quote(lease_in)}",
                f"export CANOPY_LEASE_OUT={shlex.quote(out_prefix(lease))}",
                "exec " + shlex.join(argv),
            ]
            command_id = broker(lease.capability).run_async(t, lease.instance_id, script, timeout_s=timeout_s,
                                                            output_prefix=output_prefix)
    except aws.ERRORS as e:
        raise CapabilityError(502, f"could not send the command: {e}") from e
    now = timezone.now()
    with transaction.atomic():
        row = CapabilityLease.objects.select_for_update().get(pk=lease.pk)
        row.execs = {**(row.execs or {}),
                     command_id: {"sent_at": now.isoformat(), "timeout_s": timeout_s, "done": False}}
        row.last_heartbeat_at = now
        row.save(update_fields=["execs", "last_heartbeat_at"])
    # argv[0] and the count only: the arguments can carry paths and credentials,
    # and events are readable by every member of the workspace.
    _event(lease, "exec.sent", f"{lease.agent.slug}/{lease.capability.name}: {argv[0]} "
           f"({len(argv) - 1} argument{'s' if len(argv) != 2 else ''})",
           exec_id=command_id, argv0=argv[0], argc=len(argv), timeout_s=timeout_s)
    return command_id, timeout_s


@dataclass
class ExecStatus:
    exec_id: str
    status: str
    done: bool
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    stdout_url: str | None
    stderr_url: str | None


def _pending(exec_id: str) -> ExecStatus:
    return ExecStatus(exec_id=exec_id, status="Pending", done=False, exit_code=None,
                      stdout_tail="", stderr_tail="", stdout_url=None, stderr_url=None)


def exec_status(lease: CapabilityLease, exec_id: str, *, wait_s: int = 0) -> ExecStatus:
    """Read an exec back from SSM — no process state, so it answers the same
    after a canopy-web deploy as before it. With `wait_s` (≤ MAX_WAIT_S) it
    re-reads about once a second and returns as soon as the exec is done."""
    try:
        uuid.UUID(exec_id)
    except ValueError:
        raise CapabilityError(404, f"no exec {exec_id} on lease {lease.pk}") from None
    entry = exec_entries(lease).get(exec_id)
    if not lease.instance_id or entry is None:
        raise CapabilityError(404, f"no exec {exec_id} on lease {lease.pk}")
    cap = lease.capability
    b, t = broker(cap), target(cap)
    wait_s = max(0, min(int(wait_s or 0), MAX_WAIT_S))
    deadline = _monotonic() + wait_s
    while True:
        try:
            inv = b.invocation(t, exec_id, lease.instance_id)
        except aws.ERRORS as e:
            raise CapabilityError(502, f"could not read the command: {e}") from e
        if inv is None:
            # SSM's GetCommandInvocation answers InvocationDoesNotExist for a few
            # seconds after SendCommand (eventual consistency). An exec THIS lease
            # sent that SSM cannot see yet is pending, not missing — the first ACE
            # turn on a cloud runner polled inside that window and failed
            # (turn f51078e3, 2026-10-14). Past the grace window it really is lost.
            age = (timezone.now() - entry.sent_at).total_seconds()
            if age > INVOCATION_GRACE_S:
                raise CapabilityError(404, (f"exec {exec_id} on lease {lease.pk} was sent {int(age)}s ago "
                                            "but SSM has no invocation for it"))
        if (inv is not None and inv.done) or _monotonic() >= deadline:
            break
        _sleep(POLL_S)
    _touch(lease)
    if inv is None:
        return _pending(exec_id)
    urls: dict[str, str | None] = {"stdout": None, "stderr": None}
    if inv.done:
        _mark_done(lease, exec_id)
        ttl = _ttl(b)
        for stream, raw in (("stdout", inv.stdout_url), ("stderr", inv.stderr_url)):
            key = _output_key(lease, t, exec_id, stream, raw)
            try:
                urls[stream] = b.presign_get(t.bucket, key, ttl)
            except aws.ERRORS:
                urls[stream] = None
    return ExecStatus(exec_id=exec_id, status=inv.status, done=inv.done, exit_code=inv.exit_code,
                      stdout_tail=inv.stdout[-STDOUT_TAIL:], stderr_tail=inv.stderr[-STDOUT_TAIL:],
                      stdout_url=urls["stdout"], stderr_url=urls["stderr"])


def _mark_done(lease: CapabilityLease, exec_id: str) -> None:
    """Record that an exec finished, so it no longer holds the lease awake."""
    with transaction.atomic():
        row = CapabilityLease.objects.select_for_update().filter(pk=lease.pk).first()
        if row is None:
            return
        raw = (row.execs or {}).get(exec_id)
        if isinstance(raw, dict) and raw.get("done"):
            return
        entry = raw if isinstance(raw, dict) else {"sent_at": raw, "timeout_s": 0}
        row.execs = {**(row.execs or {}), exec_id: {**entry, "done": True}}
        row.save(update_fields=["execs"])
        lease.execs = row.execs


def _output_key(lease, t: aws.Target, exec_id: str, stream: str, url: str) -> str:
    """The S3 key SSM wrote a stream to: parsed from the URL SSM reports, else
    SSM's documented layout. Never outside this lease's exec/ prefix."""
    prefix = f"{_base(lease)}/exec/"
    step = "exec" if t.exec_document else "0.awsrunShellScript"
    fallback = f"{prefix}{exec_id}/{lease.instance_id}/awsrunShellScript/{step}/{stream}"
    if url:
        p = urlparse(url)
        path = unquote(p.path).lstrip("/")
        if path.startswith(t.bucket + "/"):
            path = path[len(t.bucket) + 1:]
        if path.startswith(prefix + exec_id + "/") and ".." not in path:
            return path
    return fallback


def _ttl(b: aws.Broker) -> int:
    try:
        return int(b.presign_ttl(PRESIGN_TTL_S))
    except Exception:  # noqa: BLE001
        return PRESIGN_TTL_S


# ---- uploads / artifacts ------------------------------------------------------

def uploads(lease: CapabilityLease, names: list[str]) -> tuple[list[dict], int]:
    """Presigned PUTs under the lease's in/. Returns (the uploads, their TTL)."""
    if lease.status not in CapabilityLease.LIVE:
        raise CapabilityError(409, f"lease {lease.pk} is {lease.status}; nothing can be staged for it")
    if not names or len(names) > MAX_UPLOADS:
        raise CapabilityError(422, f"names must list 1-{MAX_UPLOADS} files")
    if len(set(names)) != len(names):
        raise CapabilityError(422, "names must be unique")
    for n in names:
        if not isinstance(n, str) or len(n) > 200 or not _PATH.match(n) or ".." in n.split("/"):
            raise CapabilityError(422, (f"upload name {n!r} must be a relative path of letters, digits, "
                                        "'.', '_', '-' and '/' (no '..'), at most 200 characters"))
    base = _base(lease)
    if not base:
        raise CapabilityError(409, (f"lease {lease.pk} has no machine yet ({lease.status}); its inputs live "
                                    "under the machine's own prefix, so stage them once instance_id is set"))
    b, t = broker(lease.capability), target(lease.capability)
    ttl = _ttl(b)
    out = []
    for n in names:
        key = f"{base}/in/{n}"
        try:
            url = b.presign_put(t.bucket, key, ttl)
        except aws.ERRORS as e:
            raise CapabilityError(502, f"could not presign an upload: {e}") from e
        out.append({"name": n, "put_url": url, "s3_uri": f"s3://{t.bucket}/{key}"})
    _touch(lease)
    return out, ttl


def artifacts(lease: CapabilityLease, prefix: str = "") -> tuple[list[dict], int]:
    """Presigned GETs for everything under the lease's out/<prefix>. Returns (the
    artifacts, their TTL)."""
    prefix = prefix or ""
    if len(prefix) > 200 or not _PREFIX.match(prefix) or ".." in prefix.split("/"):
        raise CapabilityError(422, f"prefix {prefix!r} must be a relative path (no '..')")
    lease_base = _base(lease)
    if not lease_base:
        return [], PRESIGN_TTL_S
    b, t = broker(lease.capability), target(lease.capability)
    base = f"{lease_base}/out/"
    ttl = _ttl(b)
    try:
        objs = b.list_objects(t.bucket, base + prefix, MAX_ARTIFACTS)
        out = [{"path": o["key"][len(base):], "size": o.get("size", 0),
                "url": b.presign_get(t.bucket, o["key"], ttl)}
               for o in objs if o["key"].startswith(base)]
    except aws.ERRORS as e:
        raise CapabilityError(502, f"could not list artifacts: {e}") from e
    _touch(lease)
    return out, ttl


# ---- grants -------------------------------------------------------------------

def set_grant(agent, name: str, *, max_concurrent: int | None, max_lease_s: int | None, by) -> tuple[CapabilityGrant, dict | None]:
    cap = active_capability(agent, name)
    if cap is not None:
        ceiling = manifest_of(cap).limits
        if max_concurrent and max_concurrent > ceiling.max_concurrent:
            raise CapabilityError(422, f"max_concurrent {max_concurrent} is above the manifest's "
                                       f"{ceiling.max_concurrent}; a grant may only lower it")
        if max_lease_s and max_lease_s > ceiling.max_lease_s:
            raise CapabilityError(422, f"max_lease_s {max_lease_s} is above the manifest's "
                                       f"{ceiling.max_lease_s}; a grant may only lower it")
    existing = grant_for(agent, name)
    before = grant_state(existing)
    grant, _ = CapabilityGrant.objects.update_or_create(
        agent=agent, capability_name=name,
        defaults={"workspace_id": agent.workspace_id, "max_concurrent": max_concurrent,
                  "max_lease_s": max_lease_s,
                  **({} if existing else {"created_by": by if getattr(by, "is_authenticated", False) else None})},
    )
    if cap is not None:
        transaction.on_commit(lambda: _safe(promote, cap))
    return grant, before


def revoke_grant(grant: CapabilityGrant) -> int:
    """Delete the grant and release every live lease it allowed."""
    agent, name = grant.agent, grant.capability_name
    grant.delete()
    n = 0
    for lease in CapabilityLease.objects.filter(agent=agent, capability__name=name,
                                                status__in=CapabilityLease.LIVE):
        n += end(lease, CapabilityLease.RELEASED, "its capability grant was revoked")
    return n


def grant_state(grant: CapabilityGrant | None) -> dict | None:
    if grant is None:
        return None
    return {"agent": grant.agent.slug, "capability": grant.capability_name,
            "max_concurrent": grant.max_concurrent, "max_lease_s": grant.max_lease_s}


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("capabilities: %s failed", getattr(fn, "__name__", fn))


# ---- reading leases: the list and the spend -----------------------------------

def leases_for(workspace_slugs, *, agent_ids=(), agent: str | None = None, capability: str | None = None,
               statuses: list[str] | None = None, since: dt.datetime | None = None, limit: int = 100):
    """Leases in the caller's workspaces (or of agents the caller IS), newest first."""
    q = CapabilityLease.objects.filter(Q(workspace_id__in=list(workspace_slugs)) | Q(agent_id__in=list(agent_ids)))
    if agent:
        q = q.filter(agent__slug=agent)
    if capability:
        q = q.filter(capability__name=capability)
    if statuses:
        q = q.filter(status__in=statuses)
    if since:
        q = q.filter(created_at__gte=since)
    return list(q.select_related("agent", "capability", "workspace").order_by("-created_at")[:max(1, min(limit, 500))])


def spend(workspace_slugs, *, agent_ids=(), since: dt.datetime, until: dt.datetime, agent: str | None = None,
          capability: str | None = None) -> list[dict]:
    """Machine-seconds by agent and capability over [since, until): each lease
    counts from `provisioning_at` (its launch) to `ended_at` (or now, while it
    holds a machine), clipped to the window. Queued time costs nothing."""
    q = CapabilityLease.objects.filter(
        Q(workspace_id__in=list(workspace_slugs)) | Q(agent_id__in=list(agent_ids)),
        provisioning_at__isnull=False, provisioning_at__lt=until,
    ).filter(Q(ended_at__isnull=True) | Q(ended_at__gt=since))
    if agent:
        q = q.filter(agent__slug=agent)
    if capability:
        q = q.filter(capability__name=capability)
    now = timezone.now()
    rows: dict[tuple[str, str], dict] = {}
    for lease in q.select_related("agent", "capability"):
        start = max(lease.provisioning_at, since)
        stop = min(lease.ended_at or now, until)
        seconds = max(0, int((stop - start).total_seconds()))
        row = rows.setdefault((lease.agent.slug, lease.capability.name), {
            "agent": lease.agent.slug, "capability": lease.capability.name, "leases": 0,
            "machine_seconds": 0, "live": 0})
        row["leases"] += 1
        row["machine_seconds"] += seconds
        row["live"] += lease.status in CapabilityLease.HOLDING
    return sorted(rows.values(), key=lambda r: (-r["machine_seconds"], r["agent"], r["capability"]))


# ---- the sweep ----------------------------------------------------------------

_local_last_sweep = 0.0


def sweep_tick(*, trigger: str = "claim") -> bool:
    """Run the sweep if it is due fleet-wide. Called by the scheduler
    (scheduler.py, every web process, independent of runners and of any fleet
    hold) and on every runner claim (apps/harness/claim.py) — the claim only as
    a fallback, when no sweep has finished for SWEEP_STALE_S. It can never
    raise. The cache key makes it at most once per SWEEP_EVERY_S across the
    fleet; if the cache itself fails, this process falls back to its own
    clock rather than skipping — a cost guard must not depend on Redis.
    True when it ran."""
    global _local_last_sweep
    try:
        if trigger == "claim" and _scheduler_keeping_up():
            return False
        try:
            due = cache.add(SWEEP_CACHE_KEY, timezone.now().isoformat(), SWEEP_EVERY_S)
        except Exception as e:  # noqa: BLE001 — Redis down: run on this process's clock
            logger.warning("capability sweep: the cache is unavailable (%s); using this process's clock", e)
            due = _monotonic() - _local_last_sweep >= SWEEP_EVERY_S
        if not due:
            return False
        _local_last_sweep = _monotonic()
        sweep()
        try:
            cache.set(SWEEP_RAN_KEY, timezone.now().isoformat(), 3600)
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception("capability sweep failed (%s)", trigger)
        _record_sweep_failure(e, trigger)
        return False


def _scheduler_keeping_up() -> bool:
    try:
        ran = cache.get(SWEEP_RAN_KEY)
        if not ran:
            return False
        return (timezone.now() - dt.datetime.fromisoformat(ran)).total_seconds() < SWEEP_STALE_S
    except Exception:  # noqa: BLE001 — unknown: let the claim tick try
        return False


def _record_sweep_failure(err: Exception, trigger: str) -> None:
    """A sweep that dies must be visible: one coalescing error event in every
    workspace whose agents declare a capability."""
    from apps.events import services as events

    try:
        slugs = set(AgentCapability.objects.values_list("agent__workspace_id", flat=True))
        from apps.workspaces.models import Workspace

        for ws in Workspace.objects.filter(slug__in=slugs):
            events.record([{"source": SOURCE, "kind": "capability.sweep.failed", "level": "error",
                            "key": "sweep:failed", "summary": f"the capability sweep failed: {err}"[:500],
                            "payload": {"error": repr(err)[:1000], "trigger": trigger}}], workspace=ws)
    except Exception:  # noqa: BLE001 — the DB may be what failed
        logger.exception("capability sweep: could not record the failure")


def _sweep_capabilities():
    recent = timezone.now() - dt.timedelta(days=RETIRED_SWEEP_DAYS)
    return (AgentCapability.objects.filter(kind=KIND)
            .filter(Q(retired_at__isnull=True) | Q(retired_at__gte=recent))
            .select_related("agent", "agent__workspace"))


def sweep() -> dict:
    """Every cost guard canopy-web owns, in one pass. Each step is independent:
    a failure in one lease or one capability's AWS calls is recorded as an event
    and skips that lease or capability, not the pass."""
    counts = {"expired": 0, "idle": 0, "turn_ended": 0, "checked": 0, "orphans": 0, "gone": 0,
              "refreshed": 0, "errors": 0}
    now = timezone.now()
    from apps.harness.models import Turn

    live = (CapabilityLease.objects.filter(status__in=CapabilityLease.LIVE)
            .select_related("capability", "agent", "turn", "workspace"))
    for lease in live:
        try:
            if lease.expires_at and lease.expires_at < now:
                counts["expired"] += end(lease, CapabilityLease.EXPIRED, f"reached max_lease_s ({lease.max_lease_s}s)")
            elif last_activity(lease) < now - dt.timedelta(seconds=lease.idle_release_s):
                counts["idle"] += end(lease, CapabilityLease.RELEASED,
                                      f"idle: no lease call or running exec for {lease.idle_release_s}s")
            elif lease.turn_id and lease.turn and lease.turn.status in Turn.TERMINAL:
                counts["turn_ended"] += end(lease, CapabilityLease.RELEASED, "its turn ended")
            elif lease.status == CapabilityLease.PROVISIONING:
                check_ready(lease)
                counts["checked"] += 1
        except Exception as e:  # noqa: BLE001
            counts["errors"] += 1
            logger.exception("capability sweep: lease %s", lease.pk)
            _event(lease, "sweep.error", f"the sweep could not process lease {lease.pk}: {e}", level="error",
                   key=f"sweep:lease:{lease.pk}", error=repr(e)[:1000])

    # Every published capability (and any retired in the last few days): a
    # machine can outlive the lease rows that would have led the sweep to it.
    for cap in _sweep_capabilities():
        try:
            stale = cap.outputs_refreshed_at is None or (
                now - cap.outputs_refreshed_at > dt.timedelta(seconds=OUTPUTS_REFRESH_S))
            if stale and cap.retired_at is None:
                counts["refreshed"] += refresh_outputs(cap, why="periodic re-read")
            o, g = _sweep_machines(cap)
            counts["orphans"] += o
            counts["gone"] += g
            promote(cap)
        except Exception as e:  # noqa: BLE001
            counts["errors"] += 1
            logger.exception("capability sweep: capability %s", cap.pk)
            _cap_event(cap, "sweep.error", f"the sweep could not process {cap.agent.slug}/{cap.name}: {e}",
                       level="error", key=f"sweep:cap:{cap.pk}", error=repr(e)[:1000])
    return counts


def _sweep_machines(cap: AgentCapability) -> tuple[int, int]:
    """Terminate every tagged instance with no live lease; end every ready lease
    whose instance is no longer running. Only this deployment's machines."""
    b, t = broker(cap), target(cap)
    try:
        tagged = [d for d in b.list_tagged(t) if _ours(d)]
    except aws.ERRORS as e:
        logger.warning("capability sweep: cannot list %s instances: %s", cap.pk, e)
        _cap_event(cap, "sweep.unreachable", f"{cap.agent.slug}/{cap.name}: the sweep cannot list the "
                   f"capability's instances: {e}", level="warn", key=f"sweep:list:{cap.pk}", error=str(e)[:500])
        return 0, 0
    holding = list(cap.leases.filter(status__in=CapabilityLease.HOLDING))
    by_id = {str(lease.pk): lease for lease in holding}
    by_instance = {lease.instance_id: lease for lease in holding if lease.instance_id}
    unmatched = []
    for d in tagged:
        lease = by_id.get(d.lease_id) or by_instance.get(d.instance_id)
        if lease is None:
            unmatched.append(d)
        elif not lease.instance_id:
            # Launched, but the instance id never reached the DB (a crash
            # between RunInstances and the write): adopt it.
            _record_instance(lease, t, d.instance_id, adopted=True)
    # Re-read before killing anything: a lease promoted after `holding` was read
    # may own one of these.
    if unmatched:
        ids = [d.lease_id for d in unmatched if _is_uuid(d.lease_id)]
        alive = {str(pk) for pk in CapabilityLease.objects.filter(
            pk__in=ids, status__in=CapabilityLease.HOLDING).values_list("pk", flat=True)}
        alive_instances = set(CapabilityLease.objects.filter(
            capability=cap, status__in=CapabilityLease.HOLDING,
            instance_id__in=[d.instance_id for d in unmatched]).values_list("instance_id", flat=True))
        orphans = [d.instance_id for d in unmatched
                   if d.lease_id not in alive and d.instance_id not in alive_instances]
    else:
        orphans = []
    if orphans:
        try:
            b.terminate(t, orphans)
            _cap_event(cap, "orphans.terminated", f"{cap.agent.slug}/{cap.name}: terminated "
                       f"{len(orphans)} instance(s) with no live lease", level="warn", instance_ids=orphans)
        except aws.ERRORS as e:
            logger.warning("capability sweep: cannot terminate orphans %s: %s", orphans, e)
            _cap_event(cap, "orphans.terminate_failed", f"{cap.agent.slug}/{cap.name}: could not terminate "
                       f"{len(orphans)} orphan(s): {e}", level="error", key=f"sweep:orphans:{cap.pk}",
                       instance_ids=orphans)
            orphans = []
    running = {d.instance_id for d in tagged}
    gone = 0
    for lease in holding:
        if lease.status == CapabilityLease.READY and lease.instance_id and lease.instance_id not in running:
            gone += end(lease, CapabilityLease.FAILED, f"instance {lease.instance_id} is no longer running",
                        terminate=False)
    return len(orphans), gone


def terminate_orphans_now(cap: AgentCapability) -> dict:
    """An admin's "terminate orphans now": the sweep's machine pass for one
    capability, run at once. Raises CapabilityError(502) when EC2 cannot be listed."""
    b, t = broker(cap), target(cap)
    try:
        b.list_tagged(t)
    except aws.ERRORS as e:
        raise CapabilityError(502, f"cannot list {cap.name}'s instances: {e}") from e
    orphans, gone = _sweep_machines(cap)
    promote(cap)
    return {"orphans": orphans, "gone": gone}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True
