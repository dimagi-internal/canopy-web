"""Runner fleets: interchangeable cloud runners that canopy-web grows and shrinks
with the work waiting for them (spec `docs/superpowers/specs/2026-10-24-runner-fleets-design.md`).

A fleet is ONE EC2 Auto Scaling group. A person approves it once — its
`RunnerInvitation`, from a browser behind a passkey; no token can — and from then
on an instance joins by itself when AWS says it is the group's:

* STS: the instance signs as `assumed-role/<the fleet's role>/<i-…>`. The role
  trusts only `ec2.amazonaws.com`, so the session name is the instance id, set by
  EC2 — not by anything the box chose.
* EC2: that instance is pending or running and carries the reserved tag
  `aws:autoscaling:groupName = <the fleet's group>`. No caller can set an `aws:`
  tag; only Auto Scaling does.

Each member is its own runner: its own device key (role + instance,
`aws_iam_proof.member_jkt`), its own `machine_id`, its own name
(`<fleet>-<instance id>`). They share what makes them interchangeable: the
fleet's KMS ECDH key (only the role can use it), so canopy-web hands a new
member the fleet's copy of each vault key, ciphertext it cannot open
(`_give_wraps`); and the fleet's credential set, which every member reads as its
own list (`runners.list_owner`).

One job on the off-request clock (`apps/common/ticks.py`), `tick`, every
RUNNER_SCALER_TICK_S (default 30 s), per active fleet:

1. read the group; a member's `power` follows its instance (`InService` on,
   `Pending` waking, `Warmed:*` stopped). A member whose instance left the group is
   retired, and a turn it was running is queued again;
2. **fail over**: a chat bound to a member that is gone, with a turn waiting, is
   moved to a live member (`placement.move_queued_turns`, carrying its work when the
   old member still answers, else from its history) — before demand is measured,
   so a moved turn counts against the member it moved to;
3. **grow** when turns the fleet could take have waited past RUNNER_WAKE_AFTER_S
   (60 s), not counting a turn queued only behind its own agent's or session's
   current turn: `ceil(starving / member_turns)` more members, up to `max_members`;
4. **shrink** by one member that has been idle RUNNER_IDLE_STOP_S (30 min) while
   the fleet is above `min_members`: drained on one tick (the claim refuses it),
   then — still idle — its scale-in protection lifted and the desired capacity
   lowered by one, which puts it back in the warm pool. AWS never picks a busy
   member: every member is protected until canopy-web lifts it;
5. refresh the fleet's copy of each vault key from a member that holds it.

Members are routed AS THE FLEET (`sync_routing`): a member joins with the rows its
fleet holds in agents' runner lists and workspaces' runner orders, and each tick
first carries a change made to one member's rows (adding it to an agent, taking it
off one, switching it off) to every member. Without it a member the scaler added
would be in no list, and the work it was added for would never reach it.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import math
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from apps.common import ticks
from apps.events import activity
from apps.workspaces import permissions as perms

from . import aws_iam_proof, credential_sets, device_keys
from .claim import _assignment_rows_for_turns, _coverage
from .models import (
    SERVES,
    FleetHold,
    Runner,
    RunnerAccessToken,
    RunnerAssignment,
    RunnerCredentialSet,
    RunnerDeviceKey,
    RunnerFleet,
    RunnerInvitation,
    Turn,
    WorkspaceRunnerOrder,
)
from .routing import EXECUTING, blocking_turn, prime_machine_runners, runner_tenant_slugs_of

logger = logging.getLogger(__name__)

#: A fleet's name: a slug short enough that `<name>-<17 hex>` is a runner name.
NAME = re.compile(r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$")


def tick_every_s() -> float:
    return float(getattr(settings, "RUNNER_SCALER_TICK_S", 30) or 0)


def _seconds(name: str, default: int) -> dt.timedelta:
    return dt.timedelta(seconds=int(getattr(settings, name, default)))


def wake_after() -> dt.timedelta:
    return _seconds("RUNNER_WAKE_AFTER_S", 60)


def idle_stop_after() -> dt.timedelta:
    return _seconds("RUNNER_IDLE_STOP_S", 30 * 60)


def failover_after() -> dt.timedelta:
    return _seconds("RUNNER_FAILOVER_AFTER_S", 3 * 60)


#: How many times a turn whose member died is queued again before it is left lost.
MAX_REQUEUES = 2


def member_name(fleet: RunnerFleet, instance_id: str) -> str:
    return f"{fleet.name}-{instance_id.removeprefix('i-')}"


# ── AWS, behind one seam ────────────────────────────────────────────────────
# Tests replace `get` (monkeypatch.setattr(fleets, "get", lambda region: fake)),
# so no test reaches AWS. The credentials are canopy-web's task role; each fleet's
# own stack grants it its group, and nothing else (runner/ec2/fleet.cfn.yaml).

class AwsError(Exception):
    """An AWS call failed. The message is safe to log."""


class Aws:
    def __init__(self, region: str):
        # boto3 is imported only by a process that makes the call (a web task's
        # tick, a join), not by every import of the harness.
        import boto3  # noqa: PLC0415 — see above
        from botocore.config import Config  # noqa: PLC0415 — see above

        cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2, "mode": "standard"})
        self._ec2 = boto3.client("ec2", region_name=region, config=cfg)
        self._asg = boto3.client("autoscaling", region_name=region, config=cfg)

    def _call(self, what: str, fn, **kw):
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415 — as boto3

        try:
            return fn(**kw)
        except (ClientError, BotoCoreError) as e:
            raise AwsError(f"{what}: {e}") from e

    def instance(self, instance_id: str) -> dict | None:
        """`{"state", "group", "lifecycle"}` for one instance, or None when EC2 has
        no such instance. `group` is its reserved `aws:autoscaling:groupName` tag;
        `lifecycle` its state in that group ("" when it is in none)."""
        out = self._call("describe instance", self._ec2.describe_instances, InstanceIds=[instance_id])
        found = None
        for res in out.get("Reservations") or []:
            for inst in res.get("Instances") or []:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags") or []}
                found = {"state": (inst.get("State") or {}).get("Name", ""),
                         "group": tags.get("aws:autoscaling:groupName", "")}
        if found is None:
            return None
        asg = self._call("describe group instance", self._asg.describe_auto_scaling_instances,
                         InstanceIds=[instance_id])
        rows = asg.get("AutoScalingInstances") or []
        found["lifecycle"] = rows[0].get("LifecycleState", "") if rows else ""
        return found

    def group(self, name: str) -> dict | None:
        """`{"desired", "instances": [{"id", "lifecycle", "protected"}]}` for the
        group and its warm pool, or None when there is no such group."""
        out = self._call("describe group", self._asg.describe_auto_scaling_groups,
                         AutoScalingGroupNames=[name])
        groups = out.get("AutoScalingGroups") or []
        if not groups:
            return None
        g = groups[0]
        rows = [{"id": i["InstanceId"], "lifecycle": i.get("LifecycleState", ""),
                 "protected": bool(i.get("ProtectedFromScaleIn"))} for i in g.get("Instances") or []]
        warm = self._call("describe warm pool", self._asg.describe_warm_pool, AutoScalingGroupName=name)
        rows += [{"id": i["InstanceId"], "lifecycle": i.get("LifecycleState", ""), "protected": False}
                 for i in warm.get("Instances") or []]
        return {"desired": int(g.get("DesiredCapacity") or 0), "instances": rows}

    def set_desired(self, name: str, n: int) -> None:
        self._call("set desired capacity", self._asg.set_desired_capacity,
                   AutoScalingGroupName=name, DesiredCapacity=n, HonorCooldown=False)

    def protect(self, name: str, instance_id: str, protected: bool) -> None:
        self._call("set instance protection", self._asg.set_instance_protection,
                   AutoScalingGroupName=name, InstanceIds=[instance_id], ProtectedFromScaleIn=protected)


@functools.lru_cache(maxsize=8)
def get(region: str) -> Aws:
    return Aws(region)


# ── making and approving a fleet ────────────────────────────────────────────

class FleetRefused(ValueError):
    """A fleet that cannot be made as asked; the message says why."""


def create(*, user, name: str, workspace: str, role_arn: str, ecdh_public_jwk: dict | None, region: str,
           group_name: str, min_members: int, max_members: int, member_turns: int,
           credential_set: RunnerCredentialSet | None = None) -> RunnerInvitation:
    """A PENDING fleet, its credential set, and the invitation its creator approves
    (the existing approve page). Nothing joins until that approval. `credential_set`:
    one of the creator's existing sets for its members to read (others' runners may
    already read it, in this tree or another); else a new one, `<name> credentials`."""
    if not NAME.fullmatch(name):
        raise FleetRefused("a fleet's name is a lowercase slug of at most 40 characters")
    if not 1 <= min_members <= max_members:
        raise FleetRefused("min_members must be at least 1 and at most max_members")
    role = aws_iam_proof.canonical_role_arn(role_arn)
    jkt = aws_iam_proof.role_jkt(role)
    # A role is ONE thing: a fleet's (every member signs with it) or one box's
    # identity. Never both, or the box's key would sign in as the fleet.
    if RunnerDeviceKey.objects.filter(jkt=jkt).exists() or RunnerInvitation.objects.filter(
            role_jkt=jkt, fleet__isnull=True, consumed_at__isnull=True).exists():
        raise FleetRefused(f"{role} already belongs to a runner; a fleet needs a role of its own")
    ecdh = device_keys.normalize_public_jwk(ecdh_public_jwk) if ecdh_public_jwk else {}
    if credential_set is not None and credential_set.owner_id != user.pk:
        raise FleetRefused("a fleet reads only a credential set its creator owns")
    with transaction.atomic():
        existing = RunnerFleet.objects.select_for_update().filter(name=name).first()
        if existing is not None:
            if existing.status != RunnerFleet.PENDING or existing.owner_id != user.pk:
                raise FleetRefused(f"there is already a fleet named {name}")
            # Asking again before approving: the same fleet, as asked now.
            fleet = existing
            if fleet.role_jkt != jkt and RunnerFleet.objects.filter(role_jkt=jkt).exists():
                raise FleetRefused(f"{role} is another fleet's role")
            fleet.invitations.all().delete()
            if credential_set is not None:
                fleet.credential_set = credential_set
        elif RunnerFleet.objects.filter(role_jkt=jkt).exists():
            raise FleetRefused(f"{role} is another fleet's role")
        else:
            cset = credential_set or RunnerCredentialSet.objects.create(
                name=f"{name} credentials", workspace_id=workspace, owner=user)
            fleet = RunnerFleet(name=name, owner=user, credential_set=cset)
        fleet.workspace_id, fleet.role_arn, fleet.role_jkt = workspace, role, jkt
        fleet.ecdh_public_jwk, fleet.region, fleet.group_name = ecdh, region, group_name
        fleet.min_members, fleet.max_members, fleet.member_turns = min_members, max_members, member_turns
        fleet.save()
        return RunnerInvitation.objects.create(
            name=name, workspace_id=workspace, created_by=user, role_arn=role, role_jkt=jkt,
            ecdh_public_jwk=ecdh, fleet=fleet)


def approve(invitation: RunnerInvitation, user) -> None:
    """The creator approved a fleet's invitation (behind the passkey the route
    asked for): the fleet is active, and the invitation is used up — its members
    join on AWS's word (`join`), never on the invitation."""
    now = timezone.now()
    with transaction.atomic():
        fleet = RunnerFleet.objects.select_for_update().get(pk=invitation.fleet_id)
        if fleet.status == RunnerFleet.PENDING:
            fleet.status, fleet.approved_at, fleet.approved_by = RunnerFleet.ACTIVE, now, user
            fleet.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
        RunnerInvitation.objects.filter(pk=invitation.pk, consumed_at__isnull=True).update(consumed_at=now)


def retire(fleet: RunnerFleet) -> None:
    """No more members join, and the scaler leaves its group alone. Its members
    are retired as their instances go (`fleet.sh down` sets the group to zero)."""
    RunnerFleet.objects.filter(pk=fleet.pk).update(status=RunnerFleet.RETIRED, updated_at=timezone.now())


def fleet_of_role(role_arn: str) -> RunnerFleet | None:
    return RunnerFleet.objects.filter(role_jkt=aws_iam_proof.role_jkt(role_arn)).first()


# ── an instance joins: `POST /api/harness/runners/pair-by-role` ──────────────

#: The lifecycle states that mean "in the warm pool" (stopped, or getting there).
_WARM = "Warmed:"


def power_of(lifecycle: str) -> str:
    """A member's `power` from its instance's state in the group."""
    if lifecycle == "InService":
        return Runner.POWER_ON
    if lifecycle.startswith(_WARM) or lifecycle.startswith("Terminat") or lifecycle in ("Standby", "Detached"):
        return Runner.POWER_STOPPED
    return Runner.POWER_WAKING


@dataclass(frozen=True)
class Joined:
    status: str            # "joined" | "reattached" | "waiting"
    runner: Runner | None = None
    #: A member whose runner has its own Unix user: that runner's own signing key, which it signs in with.
    runner_key: RunnerDeviceKey | None = None


def join(*, fleet: RunnerFleet, caller_arn: str, name: str, host: str, capabilities: dict,
         ecdh_public_jwk: dict | None, ecdh_store: str, refuse, runner_jwk: dict | None = None) -> Joined:
    """An instance of `fleet`'s group joins as its own runner. `caller_arn` is the
    verified STS ARN (`aws_iam_proof.verify_caller`). `refuse(why)` builds the
    uniform 401 every refusal is; the reason goes to the log only.

    A member built like a machine of several runners (`runner.cfn.yaml`'s cloud-init,
    one runner for a workspace tree) sends `runner_jwk`: that runner's own signing key. The instance
    role belongs to root, so the runner signs in with that key instead; it is made
    active under the fleet's approval, and it carries the fleet's ECDH key, so it is
    the key the fleet's vault copies are given to. The role + instance key is still
    the member's identity, for a rejoin."""
    from .api import declared_capabilities  # noqa: PLC0415 — the api module imports this one
    from .api_device_keys import ECDH_STORES  # noqa: PLC0415 — as above

    instance = aws_iam_proof.session_name(caller_arn)
    if not aws_iam_proof.INSTANCE_ID.fullmatch(instance):
        raise refuse(f"{caller_arn} is not an EC2 instance's session")
    if fleet.status == RunnerFleet.PENDING:
        return Joined("waiting")
    if fleet.status != RunnerFleet.ACTIVE:
        raise refuse(f"fleet {fleet.name} is {fleet.status}")
    if name != member_name(fleet, instance):
        raise refuse(f"{instance} joins {fleet.name} as {member_name(fleet, instance)}, not {name}")
    owner = fleet.owner
    # As for an invitation: approving a fleet is not a standing grant that
    # outlives its owner's access.
    if not owner.is_active or not perms.can(owner, fleet.workspace_id, perms.AGENT_WORK):
        raise refuse(f"fleet {fleet.name}: its owner may no longer run work in {fleet.workspace_id}")
    try:
        seen = get(fleet.region).instance(instance)
    except AwsError as exc:
        raise refuse(f"fleet {fleet.name}: could not ask EC2 about {instance}: {exc}") from exc
    if seen is None or seen["state"] not in ("pending", "running"):
        raise refuse(f"{instance} is not a pending or running instance in {fleet.region}")
    if seen["group"] != fleet.group_name:
        raise refuse(f"{instance} is not in group {fleet.group_name} (its group: {seen['group'] or 'none'})")
    ecdh = dict(fleet.ecdh_public_jwk or {})
    if ecdh_public_jwk:
        try:
            sent = device_keys.normalize_public_jwk(ecdh_public_jwk)
        except device_keys.KeyError_ as exc:
            raise refuse(f"ecdh_public_jwk: {exc}") from exc
        if ecdh and device_keys.thumbprint(sent) != device_keys.thumbprint(ecdh):
            raise refuse(f"{instance}: its ECDH key is not fleet {fleet.name}'s")
        ecdh = ecdh or sent
    role = aws_iam_proof.canonical_role_arn(caller_arn)
    jkt = aws_iam_proof.member_jkt(role, instance)
    power = power_of(seen.get("lifecycle", ""))
    own_jwk = None
    if runner_jwk is not None:
        try:
            own_jwk = device_keys.normalize_public_jwk(runner_jwk)
        except device_keys.KeyError_ as exc:
            raise refuse(f"runner key: {exc}") from exc

    existing = RunnerDeviceKey.objects.select_related("runner").filter(jkt=jkt).first()
    if existing is not None:
        if existing.status != RunnerDeviceKey.ACTIVE or existing.runner.status == Runner.RETIRED:
            raise refuse(f"{instance} was {existing.runner.name}, which is retired")
        _set_power(existing.runner, power)
        runner_key = _runner_key(fleet, existing.runner, own_jwk, name, ecdh_public_jwk) if own_jwk else None
        return Joined("reattached", existing.runner, runner_key)

    ecdh_jkt = device_keys.thumbprint(ecdh) if ecdh else ""
    store = ecdh_store if ecdh_store in ECDH_STORES else ("aws-kms" if fleet.ecdh_public_jwk else "")
    caps = declared_capabilities(capabilities)
    if own_jwk is not None:
        # canopy-web says what it serves, as for any such runner: the fleet's tree.
        from apps.workspaces import services as wsvc  # noqa: PLC0415 — as above

        caps[SERVES] = {"kind": "tree", "root": wsvc.tree_roots({fleet.workspace_id}).get(
            fleet.workspace_id, fleet.workspace_id)}
        # The runner's key carries the ECDH key; the role + instance key is identity only.
        ecdh, ecdh_jkt, store = {}, "", ""
    runner_key = None
    try:
        with transaction.atomic():
            runner = Runner.objects.create(
                name=name, kind=Runner.CLOUD, host=host, capabilities=caps,
                owner=owner, workspace_id=fleet.workspace_id, machine_id=f"{role}/{instance}",
                fleet=fleet, instance_id=instance, credential_set=fleet.credential_set,
                power=power, power_changed_at=timezone.now())
            key = RunnerDeviceKey.objects.create(
                runner=runner, kind=RunnerDeviceKey.AWS_IAM, role_arn=role, jkt=jkt,
                label=f"{name} (fleet {fleet.name} instance)", status=RunnerDeviceKey.ACTIVE,
                created_by=owner, approved_by=fleet.approved_by, approved_at=fleet.approved_at,
                ecdh_public_jwk=ecdh, ecdh_jkt=ecdh_jkt, ecdh_store=store,
                # The fleet's own key, approved with the fleet; a key it did not
                # name waits for a person, as a new ECDH key always does.
                ecdh_approved_at=fleet.approved_at if fleet.ecdh_public_jwk else None,
                ecdh_approved_by=fleet.approved_by if fleet.ecdh_public_jwk else None,
            )
            if own_jwk is not None:
                runner_key = _runner_key(fleet, runner, own_jwk, name, ecdh_public_jwk)
    except IntegrityError:
        raise refuse(f"{instance}: a concurrent join won, or its runner key is another runner's") from None
    _attach_vault(fleet, runner)
    _give_wraps(fleet, runner_key or key)
    _put_routes(runner, _current_routing(fleet, exclude=runner))
    activity.emit(activity.RUNNER_PAIRED, workspace=fleet.workspace_id, actor=owner, surface="runner",
                  ref=str(runner.pk), meta={"name": runner.name, "fleet": fleet.name})
    return Joined("joined", runner, runner_key)


def _runner_key(fleet: RunnerFleet, runner: Runner, jwk: dict, name: str,
              ecdh_public_jwk: dict | None) -> RunnerDeviceKey:
    """The member runner's own signing key: active under the fleet's approval, with the
    fleet's ECDH key (its broker serves the fleet's KMS key). A rejoin with a new
    runner key (the instance's disk was replaced) replaces the old one in place, so the
    vault copies on it are kept; the same key again changes nothing."""
    jkt = device_keys.thumbprint(jwk)
    ecdh = dict(fleet.ecdh_public_jwk or {})
    if ecdh_public_jwk and ecdh and device_keys.thumbprint(
            device_keys.normalize_public_jwk(ecdh_public_jwk)) != device_keys.thumbprint(ecdh):
        ecdh = {}  # not the fleet's key: it gets no copy, and waits for a person like any new ECDH key
    fields = dict(kind=RunnerDeviceKey.SOFTWARE, public_jwk=jwk, jkt=jkt, label=f"{name} (fleet runner key)",
                  status=RunnerDeviceKey.ACTIVE, created_by=fleet.owner, approved_by=fleet.approved_by,
                  approved_at=fleet.approved_at, ecdh_public_jwk=ecdh,
                  ecdh_jkt=device_keys.thumbprint(ecdh) if ecdh else "", ecdh_store="kms-broker" if ecdh else "",
                  ecdh_approved_at=fleet.approved_at if ecdh else None,
                  ecdh_approved_by=fleet.approved_by if ecdh else None)
    key = (RunnerDeviceKey.objects.filter(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                          status=RunnerDeviceKey.ACTIVE).first())
    if key is None:
        return RunnerDeviceKey.objects.create(runner=runner, **fields)
    if key.jkt != jkt:
        for f, v in fields.items():
            setattr(key, f, v)
        key.save()
        RunnerAccessToken.objects.filter(device_key=key).delete()
    return key


def _attach_vault(fleet: RunnerFleet, runner: Runner) -> None:
    """Serve a new member from its credential set's vault for the fleet's tree — made
    with the fleet's first member there, since a vault for runners is made attached to
    one (`credential_sets.attach_member`)."""
    from apps.agents.credentials.zk import ZkError  # noqa: PLC0415 — agents imports harness

    try:
        credential_sets.attach_member(fleet.credential_set, runner, by=fleet.owner)
    except ZkError as exc:
        # The member still runs; it reads no credentials until this is fixed, and
        # its health says so (`claude.credentials`).
        logger.warning("fleet %s: could not attach %s to its credential vault: %s", fleet.name, runner.name, exc)


def _give_wraps(fleet: RunnerFleet, key: RunnerDeviceKey) -> int:
    """Hand a new member the fleet's copy of each vault key. The copy is wrapped to
    the fleet's ECDH key, which the member's key IS (and only the fleet's role can
    use), so it opens there: ciphertext copied from one row to another, never
    opened here. A copy for an older key version is skipped — the vault was
    rotated, and its next holder gives the live members the new one."""
    from apps.agents.models import Vault, VaultKeyWrap  # noqa: PLC0415 — agents imports harness

    fleet_jkt = device_keys.thumbprint(fleet.ecdh_public_jwk) if fleet.ecdh_public_jwk else ""
    if not fleet_jkt or key.ecdh_jkt != fleet_jkt:
        return 0
    given = 0
    for vault_id, copy in (fleet.vault_wraps or {}).items():
        vault = Vault.objects.filter(pk=vault_id).first()
        if vault is None or vault.kv != copy.get("kv"):
            continue
        _, made = VaultKeyWrap.objects.get_or_create(
            vault=vault, kv=vault.kv, kind=VaultKeyWrap.DEVICE, device_key=key,
            defaults={"wrap": copy["wrap"], "created_by": fleet.owner})
        given += made
    return given


def refresh_wraps(fleet: RunnerFleet) -> None:
    """Keep the fleet's copy of each vault key at that vault's current version,
    from any member that holds it — so the fleet keeps it when no member is left,
    and a member that joins after a rotation gets the new key."""
    from apps.agents.models import VaultKeyWrap  # noqa: PLC0415 — agents imports harness

    if not fleet.ecdh_public_jwk:
        return
    jkt = device_keys.thumbprint(fleet.ecdh_public_jwk)
    copies = dict(fleet.vault_wraps or {})
    changed = False
    rows = (VaultKeyWrap.objects.filter(kind=VaultKeyWrap.DEVICE, device_key__runner__fleet=fleet,
                                        device_key__ecdh_jkt=jkt, kv=F("vault__kv"))
            .select_related("vault").order_by("vault_id", "-created_at"))
    for w in rows:
        have = copies.get(str(w.vault_id))
        if have is None or have.get("kv") != w.kv:
            copies[str(w.vault_id)] = {"kv": w.kv, "wrap": w.wrap}
            changed = True
    if changed:
        RunnerFleet.objects.filter(pk=fleet.pk).update(vault_wraps=copies)
        fleet.vault_wraps = copies
    # Members that joined before the fleet had a copy get theirs now.
    for key in RunnerDeviceKey.objects.filter(runner__fleet=fleet, status=RunnerDeviceKey.ACTIVE,
                                              ecdh_jkt=jkt).exclude(runner__status=Runner.RETIRED):
        _give_wraps(fleet, key)


# ── routing: a member is routed as its fleet ────────────────────────────────
# A route is one routing row naming a member, keyed without the member:
# ["a", agent id, source, actor] for an agent's runner list (`RunnerAssignment`),
# ["w", workspace] for a workspace's runner order (`WorkspaceRunnerOrder`).

_A_FIELDS = ("rank", "enabled", "strict", "turn_mode")


def _routes_of(runner: Runner) -> dict:
    out = {}
    for a in RunnerAssignment.objects.filter(runner=runner).values("agent_id", "source", "actor", *_A_FIELDS):
        out[json.dumps(["a", a["agent_id"], a["source"], a["actor"]])] = {f: a[f] for f in _A_FIELDS}
    for o in WorkspaceRunnerOrder.objects.filter(runner=runner).values("workspace_id", "rank", "enabled"):
        out[json.dumps(["w", o["workspace_id"]])] = {"rank": o["rank"], "enabled": o["enabled"]}
    return out


def _put_routes(runner: Runner, want: dict) -> int:
    """Make `runner`'s rows exactly `want`. Returns how many rows it wrote or removed."""
    have = _routes_of(runner)
    n = 0
    for key in set(have) - set(want):
        kind, *k = json.loads(key)
        if kind == "a":
            RunnerAssignment.objects.filter(runner=runner, agent_id=k[0], source=k[1], actor=k[2]).delete()
        else:
            WorkspaceRunnerOrder.objects.filter(runner=runner, workspace_id=k[0]).delete()
        n += 1
    for key, fields in want.items():
        if have.get(key) == fields:
            continue
        kind, *k = json.loads(key)
        if kind == "a":
            RunnerAssignment.objects.update_or_create(
                runner=runner, agent_id=k[0], source=k[1], actor=k[2], defaults=fields)
        else:
            WorkspaceRunnerOrder.objects.update_or_create(runner=runner, workspace_id=k[0], defaults=fields)
        n += 1
    return n


def _current_routing(fleet: RunnerFleet, *, exclude: Runner | None = None) -> dict:
    """What a joining member is given: a live member's rows (they may hold a change
    the next tick has not carried yet), else the fleet's own copy."""
    sibling = (fleet.members.exclude(status=Runner.RETIRED).exclude(pk=getattr(exclude, "pk", None))
               .order_by("-power_changed_at").first())
    return _routes_of(sibling) if sibling is not None else dict(fleet.routing or {})


def sync_routing(fleet: RunnerFleet, members: list[Runner]) -> list[str]:
    """Carry each member's routing changes to every member, against the fleet's copy:
    a route a member has that the copy lacks or holds differently is added or
    changed for all; a route the copy has that a member lacks was taken off it, so
    it is taken off all (a change elsewhere to the same route loses to the removal).
    A member that joined was given the copy, so it differs only where a person
    changed something."""
    if not members:
        return []
    base = dict(fleet.routing or {})
    seen = {m.pk: _routes_of(m) for m in members}
    want = dict(base)
    removed = set()
    for rows in seen.values():
        for key, fields in rows.items():
            if base.get(key) != fields:
                want[key] = fields
        removed |= set(base) - set(rows)
    for key in removed:
        want.pop(key, None)
    did = []
    with transaction.atomic():
        for m in members:
            if seen[m.pk] != want and _put_routes(m, want):
                did.append(f"routed {m.name} as the fleet")
        if want != base:
            RunnerFleet.objects.filter(pk=fleet.pk).update(routing=want)
            fleet.routing = want
    return did


# ── the scaler's view of the work ───────────────────────────────────────────

@dataclass(frozen=True)
class Facts:
    """What the tick knows about one member."""

    #: Queued turns it could take that have waited past `wake_after` and are not
    #: behind their agent's or session's current turn.
    starving: frozenset
    #: Queued turns of any age it could take.
    queued: int
    #: Turns it is executing (claimed, running, or blocked on a person).
    executing: int
    #: The last time it did anything: its last claim or finish, or its last power change.
    idle_since: dt.datetime | None


def _turn_workspace(t: Turn) -> str:
    if t.chat_session_id:
        return t.chat_session.workspace_id
    if t.agent_id:
        return t.agent.workspace_id
    return t.workspace_id


def _covered(runners: list[Runner], queued: list[Turn]) -> dict:
    """{runner pk: {turn pk}} — the queued turns each runner would claim, by the
    claim's coverage AND the tenant gate (`runner_tenant_slugs`), which the
    coverage alone does not apply."""
    if not runners or not queued:
        return {r.pk: set() for r in runners}
    by_pk = {t.pk: t for t in queued}
    prime_machine_runners(runners)
    defaults, priorities = _assignment_rows_for_turns(queued)
    tenants = runner_tenant_slugs_of(runners)
    cover = _coverage(set(by_pk), runners, defaults, priorities)
    return {r.pk: {pk for pk in pks if _turn_workspace(by_pk[pk]) in tenants[r.pk]}
            for r, pks in cover.items()}


def facts(runners: list[Runner], now: dt.datetime) -> dict:
    queued = list(Turn.objects.filter(status=Turn.QUEUED).select_related(
        "agent", "chat_session", "chat_session__runner_binding", "chat_session__agent", "enqueued_by"))
    covered = _covered(runners, queued)
    cutoff = now - wake_after()
    # While the fleet is held nothing is claimed, so nothing is worth growing for.
    starving = set() if FleetHold.is_held() else {
        t.pk for t in queued if t.created_at <= cutoff and blocking_turn(t) is None}
    out = {}
    for r in runners:
        mine = Turn.objects.filter(claimed_by=r)
        last = mine.aggregate(claimed=Max("claimed_at"), finished=Max("finished_at"))
        seen = [x for x in (last["claimed"], last["finished"], r.power_changed_at) if x is not None]
        out[r.pk] = Facts(
            starving=frozenset(covered[r.pk] & starving),
            queued=len(covered[r.pk]),
            executing=mine.filter(status__in=EXECUTING).count(),
            idle_since=max(seen) if seen else None,
        )
    return out


# ── the tick ────────────────────────────────────────────────────────────────

def _set_power(r: Runner, power: str, now: dt.datetime | None = None, *, note: str | None = None) -> None:
    if r.power == power and note is None:
        return
    r.power, r.power_changed_at = power, now or timezone.now()
    fields = ["power", "power_changed_at"]
    if note is not None:
        # The box's own heartbeat overwrites it, so it reads only while the box is down.
        r.status_note = note[:255]
        fields.append("status_note")
    r.save(update_fields=fields)


def tick(now: dt.datetime | None = None) -> dict[str, list[str]]:
    """One pass over every active fleet. Returns {fleet name: [what it did]}.
    One fleet's failure (an AWS error) never stops the rest."""
    now = now or timezone.now()
    done: dict[str, list[str]] = {}
    # A retired fleet is still read while it has members: they are retired, and
    # their chats move, as `fleet.sh down` takes its instances away.
    due = RunnerFleet.objects.filter(
        Q(status=RunnerFleet.ACTIVE)
        | Q(status=RunnerFleet.RETIRED, members__in=Runner.objects.exclude(status=Runner.RETIRED)))
    for fleet in due.distinct().select_related("owner"):
        try:
            did = tick_fleet(fleet, now)
        except Exception:  # noqa: BLE001 — one fleet's AWS error must not stop the rest
            logger.exception("fleet %s: tick failed", fleet.name)
            continue
        if did:
            logger.info("fleet %s: %s", fleet.name, "; ".join(did))
            done[fleet.name] = did
    return done


def tick_fleet(fleet: RunnerFleet, now: dt.datetime) -> list[str]:
    did: list[str] = []
    aws = get(fleet.region)
    try:
        group = aws.group(fleet.group_name)
    except AwsError:
        if fleet.status != RunnerFleet.RETIRED:
            raise
        # Its stack carried canopy-web's permission to read the group, so once
        # `fleet.sh down` deletes it the read is refused rather than empty.
        logger.info("fleet %s: retired and its group is unreadable: treating it as gone", fleet.name)
        group = None
    if group is None and fleet.status != RunnerFleet.RETIRED:
        logger.warning("fleet %s: no group %s in %s", fleet.name, fleet.group_name, fleet.region)
        return did
    # A retired fleet's group is deleted by `fleet.sh down`: every member left with it.
    in_group = {i["id"]: i for i in group["instances"]} if group else {}
    members = list(fleet.members.exclude(status=Runner.RETIRED)
                   .select_related("owner", "fleet").prefetch_related("declared_flags", "enrollments"))

    # 1. what AWS says each member is
    live: list[Runner] = []
    for m in members:
        inst = in_group.get(m.instance_id)
        if inst is None:
            _retire_gone(m, now)
            did.append(f"retired {m.name} (its instance left the group)")
            continue
        power = power_of(inst["lifecycle"])
        if m.power == Runner.POWER_DRAINING and power == Runner.POWER_ON:
            power = Runner.POWER_DRAINING  # the scaler's own state, until it acts on it
        elif power == Runner.POWER_ON and not inst["protected"]:
            # In service and unprotected: one back from the warm pool. Protected
            # again, so the only member AWS can ever scale in is one canopy-web drained.
            aws.protect(fleet.group_name, m.instance_id, True)
        _set_power(m, power, now)
        live.append(m)
    # Routing before demand: a member is counted only for work routed to it.
    did += sync_routing(fleet, live)
    # 2. fail over the chats of members that are gone FIRST: a moved turn is then
    # counted against the member it moved to, not as demand for one more.
    did += fail_over(fleet, now)
    if fleet.status != RunnerFleet.ACTIVE:
        return did  # retired: canopy-web no longer sizes its group
    f = facts(live, now)

    # 3. grow
    in_service = [m for m in live if m.power in (Runner.POWER_ON, Runner.POWER_DRAINING)]
    coming = [m for m in live if m.power == Runner.POWER_WAKING]
    # Members AWS has not joined yet (launching, still installing) count as coming too.
    unjoined = sum(1 for i in in_group if i not in {m.instance_id for m in live}
                   and power_of(in_group[i]["lifecycle"]) == Runner.POWER_WAKING)
    starving = set().union(*(f[m.pk].starving for m in live)) if live else set()
    desired = group["desired"]
    want = desired
    if starving:
        absorbs = (len(coming) + unjoined) * fleet.member_turns
        extra = math.ceil(max(0, len(starving) - absorbs) / fleet.member_turns)
        want = min(fleet.max_members, max(desired, len(in_service) + len(coming) + unjoined + extra))
    want = max(want, fleet.min_members)
    if want > desired:
        aws.set_desired(fleet.group_name, want)
        did.append(f"desired {desired} → {want} for {len(starving)} waiting turn(s)")
        activity.emit(activity.RUNNER_FLEET_SCALED, workspace=fleet.workspace_id, surface="runner",
                      ref=str(fleet.pk), meta={"fleet": fleet.name, "from": desired, "to": want,
                                               "waiting": len(starving)})
        desired = want

    # 4. shrink (never while anything waits)
    elif not starving:
        did += _shrink(fleet, aws, in_service, f, desired, now)

    # 5. the fleet's copy of each vault key
    refresh_wraps(fleet)
    return did


def _idle(fx: Facts, now: dt.datetime) -> bool:
    if fx.executing or fx.queued:
        return False
    return fx.idle_since is None or now - fx.idle_since >= idle_stop_after()


def _shrink(fleet: RunnerFleet, aws: Aws, in_service: list[Runner], f: dict, desired: int,
            now: dt.datetime) -> list[str]:
    did: list[str] = []
    draining = [m for m in in_service if m.power == Runner.POWER_DRAINING]
    for m in draining:
        fx = f[m.pk]
        if fx.executing or fx.queued or len(in_service) <= fleet.min_members or desired <= fleet.min_members:
            _set_power(m, Runner.POWER_ON, now)
            did.append(f"undrained {m.name}")
            continue
        # Still idle a whole tick after the claim stopped giving it work: lift its
        # protection and take one off the group — the one unprotected instance is
        # the one AWS scales in, back into the warm pool.
        aws.protect(fleet.group_name, m.instance_id, False)
        aws.set_desired(fleet.group_name, desired - 1)
        minutes = int(idle_stop_after().total_seconds() // 60)
        _set_power(m, Runner.POWER_STOPPED, now,
                   note=f"stopped by canopy-web after {minutes} min idle — the fleet starts a member when work arrives")
        activity.emit(activity.RUNNER_FLEET_SCALED, workspace=fleet.workspace_id, surface="runner",
                      ref=str(fleet.pk), meta={"fleet": fleet.name, "from": desired, "to": desired - 1,
                                               "member": m.name})
        did.append(f"desired {desired} → {desired - 1}: {m.name} back to the warm pool")
        return did  # one member per tick
    if draining or len(in_service) <= fleet.min_members:
        return did
    idle = [m for m in in_service if m.power == Runner.POWER_ON and m.live_status == Runner.ONLINE
            and _idle(f[m.pk], now)]
    if idle:
        m = min(idle, key=lambda r: f[r.pk].idle_since or now)
        _set_power(m, Runner.POWER_DRAINING, now)
        did.append(f"draining {m.name}")
    return did


def _retire_gone(m: Runner, now: dt.datetime) -> None:
    """Its instance is no longer in the group (terminated, or detached by hand): the
    runner is retired, and what it was running is queued again (`requeue_lost`)."""
    from .closing import close_turn  # noqa: PLC0415 — closing imports the services stack

    for turn in Turn.objects.filter(claimed_by=m, status__in=list(EXECUTING)):
        if not requeue_lost(turn):
            close_turn(turn, Turn.LOST, from_states=EXECUTING, event={"reason": "fleet_member_gone"},
                       riders=False, drill_summary="runner lost the turn (its fleet instance is gone)",
                       auto_debug=False)
    with transaction.atomic():
        Runner.objects.filter(pk=m.pk).update(
            status=Runner.RETIRED, power=Runner.POWER_STOPPED, power_changed_at=now,
            status_note="retired: its instance left the fleet's group")
        # As the retire route does: a retired runner's rows would be listed by the
        # Runners tab and 422 its next save. The fleet keeps its routing (`routing`).
        RunnerAssignment.objects.filter(runner=m).delete()
        WorkspaceRunnerOrder.objects.filter(runner=m).delete()


def requeue_lost(turn: Turn) -> bool:
    """Queue a turn a gone member was running again, as itself — no second message
    in the chat. As `services.finish_turn`'s sessionless requeue does; capped at
    MAX_REQUEUES so a turn that kills its box is not retried forever."""
    from .ledger import append_events  # noqa: PLC0415 — ledger imports the services stack
    from .services import _finish_riders  # noqa: PLC0415 — services imports this stack

    if turn.attempts >= MAX_REQUEUES:
        return False
    moved = Turn.objects.filter(pk=turn.pk, status__in=list(EXECUTING)).update(
        status=Turn.QUEUED, attempts=F("attempts") + 1, claimed_by=None, claimed_at=None,
        lease_expires_at=None, started_at=None, cli="")
    if not moved:
        return False
    turn.refresh_from_db()
    _finish_riders(turn, Turn.LOST)
    append_events(turn, [{"kind": "status", "payload": {
        "status": Turn.QUEUED, "requeued_after": "fleet_member_gone", "attempt": turn.attempts}}])
    return True


def _gone(r: Runner, now: dt.datetime) -> bool:
    """A member that will not answer its chats: retired, or no longer reachable
    and either out of service or silent for `failover_after`.

    Never one that still answers. Its chats would then be moved WITH a handoff
    still being packed, and their waiting turns could reach the new member before
    the work does. A member in service with work waiting is not drained (that
    work undrains it), so a live member never needs moving off."""
    if r.status == Runner.RETIRED:
        return True
    if r.is_reachable:
        return False
    if r.power in (Runner.POWER_STOPPED, Runner.POWER_WAKING):
        return True
    return r.last_heartbeat_at is None or now - r.last_heartbeat_at >= failover_after()


def fail_over(fleet: RunnerFleet, now: dt.datetime) -> list[str]:
    """Move each chat that is bound to a gone member and has a turn waiting to the
    live member with the most room. `placement.move_queued_turns` does the move: a real
    transfer, carrying the work when the old member still answers and the history
    when it does not. Acts as the fleet's owner, who administers every member."""
    from apps.canopy_sessions import placement as session_placement  # noqa: PLC0415 — canopy_sessions imports harness
    from apps.canopy_sessions.models import RunnerBinding  # noqa: PLC0415 — as above

    from . import initiator as who  # noqa: PLC0415 — initiator imports the agents stack
    from . import sensitivity  # noqa: PLC0415 — as above

    members = list(fleet.members.select_related("fleet"))
    gone = {m.pk for m in members if _gone(m, now)}
    if not gone:
        return []
    bindings = list(RunnerBinding.objects.select_related("session", "runner")
                    .filter(runner_id__in=gone,
                            session__turns__status=Turn.QUEUED).distinct())
    if not bindings:
        return []
    targets = [m for m in members if m.pk not in gone and m.power == Runner.POWER_ON
               and m.status != Runner.RETIRED and m.live_status == Runner.ONLINE and not m.paused]
    if not targets:
        return []  # the grow step is already bringing one in for these turns
    load = {m.pk: Turn.objects.filter(claimed_by=m, status__in=list(EXECUTING)).count() for m in targets}
    did = []
    for b in bindings:
        session = b.session
        if sensitivity.is_sensitive(session):
            continue  # a sealed conversation is moved by its own people
        target = min(targets, key=lambda m: load[m.pk])
        try:
            moved = session_placement.move_queued_turns(
                session=session, placement=str(target.id), user=fleet.owner,
                initiator=who.for_user(fleet.owner, via="fleet", assurance=""))
        except (LookupError, ValueError, RuntimeError) as exc:
            logger.info("fleet %s: could not move session %s off %s: %s",
                        fleet.name, session.pk, b.runner.name if b.runner else "?", exc)
            continue
        load[target.pk] += len(moved)
        did.append(f"moved {len(moved)} turn(s) of session {str(session.pk)[:8]} to {target.name}")
    return did


def register() -> None:
    ticks.register("harness.runner_fleets", tick, tick_every_s)
