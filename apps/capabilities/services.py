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
- **Four cost guards, any one sufficient**: explicit release; the bound turn
  ending, or no heartbeat for idle_release_s; the sweep on the claim tick
  (expiry, idleness, the queue, and terminating every tagged instance with no
  live lease); and the machine's own halt-at-max-lifetime + terminate-on-halt.
- **Every transition is an `events.record` row** (`capability.lease.<status>`).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import shlex
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from canopy_runtime.schema import CapabilityManifest
from canopy_sdk.ondemand import LaunchFailed
from pydantic import ValidationError

from . import aws
from .models import AgentCapability, CapabilityGrant, CapabilityLease

logger = logging.getLogger(__name__)

SOURCE = "capabilities"
KIND = "ec2-ephemeral"
#: How often the claim tick may run the sweep, per process.
SWEEP_EVERY_S = 30
SWEEP_CACHE_KEY = "capabilities:sweep:last"
#: Presigned URLs (uploads, artifacts, exec output) live this long.
PRESIGN_TTL_S = 3600
STDOUT_TAIL = 4000
MAX_EXEC_TIMEOUT_S = 172800
MAX_UPLOADS = 50
MAX_ARTIFACTS = 1000
_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_PREFIX = re.compile(r"^([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]*$")


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
                      bucket=o.get("ArtifactsBucketName", ""), role_id=o.get("InstanceRoleId", ""))


def broker(cap: AgentCapability) -> aws.Broker:
    return aws.get(cap.region)


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

def _event(lease: CapabilityLease, kind: str, summary: str, *, level: str = "info", **payload) -> None:
    from apps.events import services as events

    try:
        events.record([{
            "source": SOURCE, "kind": f"capability.{kind}", "level": level, "summary": summary[:500],
            "payload": {"lease_id": str(lease.pk), "agent": lease.agent.slug,
                        "capability": lease.capability.name, "instance_id": lease.instance_id,
                        "status": lease.status, **payload},
        }], workspace=lease.workspace)
    except Exception:  # noqa: BLE001 — the audit row must never undo the transition
        logger.exception("could not record capability event %s for %s", kind, lease.pk)


def _cap_event(cap: AgentCapability, kind: str, summary: str, *, level: str = "info", **payload) -> None:
    from apps.events import services as events

    try:
        events.record([{"source": SOURCE, "kind": f"capability.{kind}", "level": level,
                        "summary": summary[:500],
                        "payload": {"agent": cap.agent.slug, "capability": cap.name, **payload}}],
                      workspace=cap.agent.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("could not record capability event %s for %s", kind, cap.pk)


# ---- publish ------------------------------------------------------------------

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
    resolved: list[tuple[CapabilityManifest, dict]] = []
    for m in manifests:
        try:
            outputs = aws.get(m.aws.region).stack_outputs(m.aws.stack)
        except aws.ERRORS as e:
            raise CapabilityError(422, (
                f"{m.name}: {e}. canopy-web reads the stack with the permissions the stack itself "
                "grants it (BrokerPolicy, via BrokerRoleName): is it deployed in that region with "
                "Mode=ephemeral and canopy-web's task role as BrokerRoleName?")) from e
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
        resolved.append((m, {k: outputs[k] for k in (*aws.REQUIRED_OUTPUTS, *aws.CHECKED_OUTPUTS)}))

    now = timezone.now()
    with transaction.atomic():
        rows = []
        for m, outputs in resolved:
            row, _ = AgentCapability.objects.update_or_create(
                agent=agent, name=m.name,
                defaults={"kind": m.kind, "manifest": m.model_dump(mode="json"), "region": m.aws.region,
                          "stack": m.aws.stack, "stack_outputs": outputs, "published_at": now,
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
    lease = CapabilityLease.objects.create(
        workspace_id=agent.workspace_id, agent=agent, capability=cap, status=CapabilityLease.QUEUED,
        params=resolved, turn=turn, requested_by=by if getattr(by, "is_authenticated", False) else None,
        max_lease_s=limits["max_lease_s"], idle_release_s=limits["idle_release_s"],
        ready_timeout_s=limits["ready_timeout_s"], last_heartbeat_at=now,
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

def _holding_count(cap: AgentCapability, b: aws.Broker, t: aws.Target) -> int | None:
    """Machines held against the cap: holding leases, plus any tagged instance no
    holding lease accounts for (an orphan still costs a slot until the sweep
    kills it). None when EC2 cannot be asked — then nothing launches."""
    holding = {str(pk): iid for pk, iid in cap.leases.filter(status__in=CapabilityLease.HOLDING)
               .values_list("pk", "instance_id")}
    try:
        tagged = b.list_tagged(t)
    except aws.ERRORS as e:
        logger.warning("capability %s: cannot count tagged instances: %s", cap.pk, e)
        return None
    known_instances = {iid for iid in holding.values() if iid}
    extra = [d for d in tagged if d.lease_id not in holding and d.instance_id not in known_instances]
    return len(holding) + len(extra)


def promote(cap: AgentCapability) -> list[CapabilityLease]:
    """Move the oldest queued leases to provisioning while under the cap (FIFO),
    then launch each. Safe to call any time; serialized per capability."""
    if cap.retired_at is not None or not CapabilityLease.objects.filter(
            capability=cap, status=CapabilityLease.QUEUED).exists():
        return []
    grant = grant_for(cap.agent, cap.name)
    if grant is None:
        return []
    b, t = broker(cap), target(cap)
    now = timezone.now()
    with transaction.atomic():
        AgentCapability.objects.select_for_update().filter(pk=cap.pk).first()
        queued = list(CapabilityLease.objects.filter(capability=cap, status=CapabilityLease.QUEUED)
                      .order_by("created_at"))
        holding = _holding_count(cap, b, t)
        if holding is None:
            return []
        free = effective_limits(cap, grant)["max_concurrent"] - holding
        promoted = queued[:max(0, free)]
        for lease in promoted:
            CapabilityLease.objects.filter(pk=lease.pk, status=CapabilityLease.QUEUED).update(
                status=CapabilityLease.PROVISIONING, provisioning_at=now, reason="",
                expires_at=now + dt.timedelta(seconds=lease.max_lease_s))
            lease.refresh_from_db()
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
        instance_id = broker(cap).launch(t, lease_id=str(lease.pk), user_data=user_data)
    except LaunchFailed as e:
        if not e.transient:
            end(lease, CapabilityLease.FAILED, f"launch refused: {e}", terminate=False)
            return
        _retrying(lease, e)
        return
    except aws.ERRORS as e:
        _retrying(lease, e)
        return
    updated = CapabilityLease.objects.filter(pk=lease.pk, status=CapabilityLease.PROVISIONING).update(
        instance_id=instance_id, reason="")
    if not updated:
        # Released while the launch was in flight: the machine has no lease.
        try:
            broker(cap).terminate(t, [instance_id])
        except aws.ERRORS:
            logger.warning("lease %s: could not terminate %s (the sweep will)", lease.pk, instance_id)
        return
    if lease.instance_id != instance_id:
        lease.instance_id = instance_id
        _event(lease, "lease.launched", f"{cap.agent.slug}/{cap.name} instance {instance_id} launched")


def _retrying(lease: CapabilityLease, err: Exception) -> None:
    CapabilityLease.objects.filter(pk=lease.pk).update(reason=f"launch retrying: {err}"[:500])
    logger.warning("lease %s: launch will be retried: %s", lease.pk, err)


# ---- heartbeat / readiness ----------------------------------------------------

def heartbeat(lease: CapabilityLease) -> CapabilityLease:
    """GET on a lease: proves the holder is alive and, while provisioning, looks
    for the machine's ready.json (one S3 read; no SSM, no worker)."""
    if lease.status in CapabilityLease.LIVE:
        CapabilityLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=timezone.now())
    if lease.status == CapabilityLease.PROVISIONING:
        check_ready(lease)
    lease.refresh_from_db()
    return lease


def check_ready(lease: CapabilityLease) -> None:
    lease.refresh_from_db()
    if lease.status != CapabilityLease.PROVISIONING:
        return
    cap = lease.capability
    b, t = broker(cap), target(cap)
    if not lease.instance_id:
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
        end(lease, CapabilityLease.FAILED, f"not ready after {lease.ready_timeout_s}s")


# ---- ending -------------------------------------------------------------------

def end(lease: CapabilityLease, status: str, reason: str, *, terminate: bool = True) -> bool:
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
    promote(lease.capability)
    return True


def release(lease: CapabilityLease, *, reason: str = "released by its holder") -> CapabilityLease:
    end(lease, CapabilityLease.RELEASED, reason)
    return lease


def release_for_turn(turn_id) -> int:
    """Release every live lease bound to `turn_id` (its turn reached a terminal status)."""
    n = 0
    for lease in CapabilityLease.objects.filter(turn_id=turn_id, status__in=CapabilityLease.LIVE):
        n += end(lease, CapabilityLease.RELEASED, "its turn ended")
    return n


# ---- exec ---------------------------------------------------------------------

def _lease_prefix(lease: CapabilityLease) -> str:
    return f"leases/{lease.pk}"


def out_prefix(lease: CapabilityLease) -> str:
    """s3://<bucket>/leases/<id>/out/ — CANOPY_LEASE_OUT, as a literal for argv."""
    return f"s3://{target(lease.capability).bucket}/{_lease_prefix(lease)}/out/"


def exec_(lease: CapabilityLease, argv: list[str], timeout_s: int) -> tuple[str, int]:
    """Send `argv` to the lease's machine; return (the SSM CommandId, the timeout used).

    argv[0] must be exactly one of the manifest's `exec.allow`; the whole argv is
    shell-quoted, so no argument can become shell syntax."""
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
    base = f"s3://{t.bucket}/{_lease_prefix(lease)}"
    script = [
        f"export CANOPY_LEASE_ID={shlex.quote(str(lease.pk))}",
        f"export CANOPY_LEASE_IN={shlex.quote(base + '/in/')}",
        f"export CANOPY_LEASE_OUT={shlex.quote(out_prefix(lease))}",
        "exec " + shlex.join(argv),
    ]
    try:
        command_id = broker(lease.capability).run_async(t, lease.instance_id, script, timeout_s=timeout_s,
                                                        output_prefix=f"{_lease_prefix(lease)}/exec")
    except aws.ERRORS as e:
        raise CapabilityError(502, f"could not send the command: {e}") from e
    CapabilityLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=timezone.now())
    _event(lease, "exec.sent", f"{lease.agent.slug}/{lease.capability.name}: {argv[0]} {argv[1:2]}",
           exec_id=command_id, argv=argv[:20], timeout_s=timeout_s)
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


def exec_status(lease: CapabilityLease, exec_id: str) -> ExecStatus:
    """Read an exec back from SSM — no process state, so it answers the same
    after a canopy-web deploy as before it."""
    try:
        uuid.UUID(exec_id)
    except ValueError:
        raise CapabilityError(404, f"no exec {exec_id} on lease {lease.pk}") from None
    if not lease.instance_id:
        raise CapabilityError(404, f"no exec {exec_id} on lease {lease.pk}")
    cap = lease.capability
    b, t = broker(cap), target(cap)
    try:
        inv = b.invocation(t, exec_id, lease.instance_id)
    except aws.ERRORS as e:
        raise CapabilityError(502, f"could not read the command: {e}") from e
    if inv is None:
        raise CapabilityError(404, f"no exec {exec_id} on lease {lease.pk}")
    if lease.is_live:
        CapabilityLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=timezone.now())
    urls: dict[str, str | None] = {"stdout": None, "stderr": None}
    if inv.done:
        for stream, raw in (("stdout", inv.stdout_url), ("stderr", inv.stderr_url)):
            key = _output_key(lease, t, exec_id, stream, raw)
            try:
                urls[stream] = b.presign_get(t.bucket, key, PRESIGN_TTL_S)
            except aws.ERRORS:
                urls[stream] = None
    return ExecStatus(exec_id=exec_id, status=inv.status, done=inv.done, exit_code=inv.exit_code,
                      stdout_tail=inv.stdout[-STDOUT_TAIL:], stderr_tail=inv.stderr[-STDOUT_TAIL:],
                      stdout_url=urls["stdout"], stderr_url=urls["stderr"])


def _output_key(lease, t: aws.Target, exec_id: str, stream: str, url: str) -> str:
    """The S3 key SSM wrote a stream to: parsed from the URL SSM reports, else
    SSM's documented layout. Never outside this lease's exec/ prefix."""
    prefix = f"{_lease_prefix(lease)}/exec/"
    fallback = f"{prefix}{exec_id}/{lease.instance_id}/awsrunShellScript/0.awsrunShellScript/{stream}"
    if url:
        p = urlparse(url)
        path = unquote(p.path).lstrip("/")
        if path.startswith(t.bucket + "/"):
            path = path[len(t.bucket) + 1:]
        if path.startswith(prefix + exec_id + "/") and ".." not in path:
            return path
    return fallback


# ---- uploads / artifacts ------------------------------------------------------

def uploads(lease: CapabilityLease, names: list[str]) -> list[dict]:
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
    b, t = broker(lease.capability), target(lease.capability)
    out = []
    for n in names:
        key = f"{_lease_prefix(lease)}/in/{n}"
        try:
            url = b.presign_put(t.bucket, key, PRESIGN_TTL_S)
        except aws.ERRORS as e:
            raise CapabilityError(502, f"could not presign an upload: {e}") from e
        out.append({"name": n, "put_url": url, "s3_uri": f"s3://{t.bucket}/{key}"})
    CapabilityLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=timezone.now())
    return out


def artifacts(lease: CapabilityLease, prefix: str = "") -> list[dict]:
    prefix = prefix or ""
    if len(prefix) > 200 or not _PREFIX.match(prefix) or ".." in prefix.split("/"):
        raise CapabilityError(422, f"prefix {prefix!r} must be a relative path (no '..')")
    b, t = broker(lease.capability), target(lease.capability)
    base = f"{_lease_prefix(lease)}/out/"
    try:
        objs = b.list_objects(t.bucket, base + prefix, MAX_ARTIFACTS)
        out = [{"path": o["key"][len(base):], "size": o.get("size", 0),
                "url": b.presign_get(t.bucket, o["key"], PRESIGN_TTL_S)}
               for o in objs if o["key"].startswith(base)]
    except aws.ERRORS as e:
        raise CapabilityError(502, f"could not list artifacts: {e}") from e
    if lease.is_live:
        CapabilityLease.objects.filter(pk=lease.pk).update(last_heartbeat_at=timezone.now())
    return out


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


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        logger.exception("capabilities: %s failed", getattr(fn, "__name__", fn))


# ---- the sweep ----------------------------------------------------------------

def sweep_tick() -> None:
    """Called on every runner claim (apps/harness/claim.py). At most once per
    SWEEP_EVERY_S per process, and it can never raise: an AWS outage must not
    stop a single turn from being claimed."""
    try:
        if not cache.add(SWEEP_CACHE_KEY, timezone.now().isoformat(), SWEEP_EVERY_S):
            return
        sweep()
    except Exception:  # noqa: BLE001
        logger.exception("capability sweep failed")


def sweep() -> dict:
    """Every cost guard canopy-web owns, in one pass. Each step is independent:
    a failure in one capability's AWS calls skips that capability, not the pass."""
    counts = {"expired": 0, "idle": 0, "turn_ended": 0, "checked": 0, "orphans": 0, "gone": 0}
    now = timezone.now()
    from apps.harness.models import Turn

    live = (CapabilityLease.objects.filter(status__in=CapabilityLease.LIVE)
            .select_related("capability", "agent", "turn"))
    for lease in live:
        try:
            if lease.expires_at and lease.expires_at < now:
                counts["expired"] += end(lease, CapabilityLease.EXPIRED, f"reached max_lease_s ({lease.max_lease_s}s)")
            elif lease.last_heartbeat_at < now - dt.timedelta(seconds=lease.idle_release_s):
                counts["idle"] += end(lease, CapabilityLease.RELEASED,
                                      f"idle: no heartbeat for {lease.idle_release_s}s")
            elif lease.turn_id and lease.turn and lease.turn.status in Turn.TERMINAL:
                counts["turn_ended"] += end(lease, CapabilityLease.RELEASED, "its turn ended")
            elif lease.status == CapabilityLease.PROVISIONING:
                check_ready(lease)
                counts["checked"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("capability sweep: lease %s", lease.pk)

    # Capabilities that could have a machine running: any with a lease in the
    # last 3 days (the longest a lease may live is 2).
    recent = now - dt.timedelta(days=3)
    caps = (AgentCapability.objects.filter(kind=KIND, leases__created_at__gte=recent)
            .select_related("agent", "agent__workspace").distinct())
    for cap in caps:
        try:
            o, g = _sweep_machines(cap)
            counts["orphans"] += o
            counts["gone"] += g
            promote(cap)
        except Exception:  # noqa: BLE001
            logger.exception("capability sweep: capability %s", cap.pk)
    return counts


def _sweep_machines(cap: AgentCapability) -> tuple[int, int]:
    """Terminate every tagged instance with no live lease; end every ready lease
    whose instance is no longer running."""
    b, t = broker(cap), target(cap)
    try:
        tagged = b.list_tagged(t)
    except aws.ERRORS as e:
        logger.warning("capability sweep: cannot list %s instances: %s", cap.pk, e)
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
            CapabilityLease.objects.filter(pk=lease.pk, instance_id="").update(instance_id=d.instance_id)
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
            orphans = []
    running = {d.instance_id for d in tagged}
    gone = 0
    for lease in holding:
        if lease.status == CapabilityLease.READY and lease.instance_id and lease.instance_id not in running:
            gone += end(lease, CapabilityLease.FAILED, f"instance {lease.instance_id} is no longer running",
                        terminate=False)
    return len(orphans), gone


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True
