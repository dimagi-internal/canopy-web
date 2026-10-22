"""A box of isolation units pairs every unit at once
(`docs/architecture/runners.md § Isolation units`).

One cloud box can run several isolation units: a workspace tree, or a single
agent, each its own Unix user and its own runner. The box's instance role belongs
to root (IMDS is root-only), so a unit signs in with a SOFTWARE key of its own,
made in its home. Pairing extends the invitation (`api_runner_invitations`):

* the invitation `up.sh` makes lists the units (`RunnerInvitation.units`), and the
  creator's ONE passkey approval covers every one;
* the box's root-only `canopy-pair` makes ONE `pair-by-role` call with the
  instance-role proof and every unit's public keys;
* canopy-web creates one `Runner` per unit (owned by the invitation's creator, in
  the unit's workspace, `machine_id` = the role) with an ACTIVE software device key.

A replaced instance re-attaches: the same role and unit names. Each unit's new
signing key REPLACES its runner's key in place — the row is kept, so the runner id,
its bindings and every zero-knowledge wrap (which hang off that row and name its
ECDH key, which lives in KMS and survives the instance) are kept. That takes no
approval: the role is already approved. The same in-place re-key adopts a box
paired before units (one runner, an aws-iam key): its runner becomes the unit of
that name, its role key becomes the unit's software key, and its KMS ECDH key —
the same key, served by the box's broker — keeps its approval and its wraps.

Every refusal is the one uniform 401 of `pair-by-role`; the reason goes to the log.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.events import activity
from apps.workspaces import permissions as perms

from . import aws_iam_proof, device_keys
from .api_device_keys import ECDH_STORES
from .models import Runner, RunnerAccessToken, RunnerDeviceKey, RunnerInvitation

#: Where a unit's ECDH key lives: the box's root-only KMS broker (canopy's
#: `kms-broker` store). Display only, like every `ecdh_store`.
BROKER_STORE = "kms-broker"


def units_of(invitation: RunnerInvitation) -> list[dict]:
    """The units an invitation pairs. [] for a one-runner invitation."""
    return [u for u in (invitation.units or []) if isinstance(u, dict) and u.get("name")]


def unit_capability(unit: dict, root: str) -> dict:
    """`capabilities.unit` for a runner paired as `unit` — set by canopy-web from the
    invitation a person approved, not taken from what the box says."""
    if unit.get("kind") == "agent":
        return {"kind": "agent", "slug": unit.get("slug") or ""}
    return {"kind": "tree", "root": root}


def _machine_runner(machine: str, role_jkt: str, name: str) -> Runner | None:
    """The live runner `name` on this machine: by `machine_id`, or — for a box paired
    before units, whose machine id was never recorded — by its role's aws-iam key."""
    runner = (Runner.objects.filter(machine_id=machine, name=name, kind=Runner.CLOUD)
              .exclude(status=Runner.RETIRED).order_by("paired_at").first())
    if runner is not None:
        return runner
    key = (RunnerDeviceKey.objects.select_related("runner")
           .filter(jkt=role_jkt, kind=RunnerDeviceKey.AWS_IAM).first())
    if key is not None and key.runner.name == name and key.runner.status != Runner.RETIRED:
        return key.runner
    return None


def _active_key(runner: Runner) -> RunnerDeviceKey | None:
    return (RunnerDeviceKey.objects.filter(runner=runner, status=RunnerDeviceKey.ACTIVE)
            .order_by("-approved_at", "-created_at").first())


def _rekey(key: RunnerDeviceKey, name: str, jwk: dict, jkt: str,
           ecdh_jwk: dict | None, ecdh_jkt: str, store: str) -> None:
    """Replace a unit runner's signing key IN PLACE (the row, and every wrap on it,
    is kept). The same ECDH key keeps its approval; a different one waits for a
    person and its old wraps go, exactly as a re-registered device key's do. The
    same key again (a restart before the box wrote its state) changes nothing."""
    from .api_device_keys import set_ecdh_and_store

    if key.jkt != jkt:
        key.kind, key.public_jwk, key.jkt, key.role_arn = RunnerDeviceKey.SOFTWARE, jwk, jkt, ""
        key.label = f"{name} (isolation unit key)"
        key.save(update_fields=["kind", "public_jwk", "jkt", "role_arn", "label"])
        # Tokens bought with the old key were the old instance's: they end with it.
        RunnerAccessToken.objects.filter(device_key=key).delete()
    if ecdh_jwk is not None:
        set_ecdh_and_store(key, ecdh_jwk, ecdh_jkt, store)


def _announce_rekey(runner: Runner, adopted: bool) -> None:
    """Every re-key is a fact a person can see: an activity, and a notice to whoever
    can approve the runner's keys (its owner and runner admins)."""
    from . import device_key_notify

    key = _active_key(runner)
    activity.emit(activity.RUNNER_REKEYED, workspace=runner.workspace_id, actor=runner.owner,
                  surface="runner", ref=str(runner.pk),
                  meta={"name": runner.name, "adopted": adopted, "jkt": (key.jkt[:12] if key else "")})
    if key is not None:
        device_key_notify.notify_rekeyed_on_commit(key, adopted=adopted)


def pair_units(request: HttpRequest, payload, role_arn: str, *, refuse, out_cls, status_cls):
    """`pair-by-role` for a box of units. `refuse(why)` builds the uniform refusal;
    `out_cls`/`status_cls` are the route's response types (kept out of this module's
    imports so it has no Ninja coupling)."""
    from .api import declared_capabilities

    machine = aws_iam_proof.canonical_role_arn(role_arn)
    role_jkt = aws_iam_proof.role_jkt(role_arn)
    units = list(payload.units or [])
    names = [u.name for u in units]
    if not units or len(set(names)) != len(names):
        raise refuse("units: empty, or a name twice")

    # Each unit's keys, checked before anything is decided.
    keys: dict[str, tuple] = {}
    for u in units:
        try:
            jwk = device_keys.normalize_public_jwk(u.public_jwk)
            ecdh = device_keys.normalize_public_jwk(u.ecdh_public_jwk) if u.ecdh_public_jwk else None
        except device_keys.KeyError_ as exc:
            raise refuse(f"unit {u.name}: {exc}") from exc
        store = u.ecdh_store if ecdh is not None and u.ecdh_store in ECDH_STORES else ""
        keys[u.name] = (jwk, device_keys.thumbprint(jwk), ecdh,
                        device_keys.thumbprint(ecdh) if ecdh else "", store)
    if len({k[1] for k in keys.values()}) != len(keys):
        raise refuse("two units sent the same signing key")

    existing: dict[str, Runner] = {}
    for name in names:
        runner = _machine_runner(machine, role_jkt, name)
        if runner is not None:
            existing[name] = runner
    for name, runner in existing.items():
        if _active_key(runner) is None:
            # A key revoked on a live runner was a deliberate act its owner undoes —
            # never something re-pairing launders (PR 3's rule, per unit).
            raise refuse(f"{runner.name} on {machine} has no active key (revoked?)")
    # Which existing runners may be re-keyed WITHOUT a person: only a unit already
    # paired as one — its active key is a unit software key and the runner is on this
    # machine. Anything else (adopting a box paired before units: its key is the
    # instance role itself; a runner whose machine was never proved) re-keys only
    # under an approved invitation that lists it. Otherwise a bare role proof —
    # which any process on a box without the IMDS rule can make — would take the
    # runner's identity for good.
    adopt = [name for name, runner in existing.items()
             if not (_active_key(runner).kind == RunnerDeviceKey.SOFTWARE and runner.machine_id == machine)]
    new = [u for u in units if u.name not in existing]
    for u in new:
        clash = (Runner.objects.filter(name=u.name, kind=Runner.CLOUD).exclude(status=Runner.RETIRED)
                 .exclude(machine_id=machine).first())
        if clash is not None:
            raise refuse(f"{u.name} is a live runner on another machine ({clash.machine_id or 'unknown'})")

    invitation = None
    if new or adopt:
        invitation = (RunnerInvitation.objects.select_related("created_by", "approved_by")
                      .filter(role_jkt=role_jkt, name=payload.name, consumed_at__isnull=True).first())
        if invitation is None:
            raise refuse(f"no live invitation for {role_arn} as {payload.name} "
                         f"(new units: {', '.join(u.name for u in new)}; to adopt: {', '.join(adopt)})")
        if invitation.expired:
            raise refuse(f"invitation {invitation.pk} expired")
        invited = {u["name"]: u for u in units_of(invitation)}
        if not invited:
            raise refuse(f"invitation {invitation.pk} names no units")
        missing = [u.name for u in new if u.name not in invited] + [n for n in adopt if n not in invited]
        if missing:
            raise refuse(f"invitation {invitation.pk} does not name units {missing}")
        absent = [n for n in invited if n not in names]
        if absent:
            raise refuse(f"the box did not bring invited units {absent}")
        if invitation.approved_at is None:
            return status_cls(202, out_cls(status="waiting"))
        owner = invitation.created_by
        # The creator must still be someone who may run work in EVERY unit's
        # workspace: one unit they may not run work in refuses the whole box.
        for u in invited.values():
            if not owner.is_active or not perms.can(owner, u["workspace"], perms.AGENT_WORK):
                raise refuse(f"invitation {invitation.pk}: its creator may no longer run work in "
                              f"{u['workspace']}")
        from apps.workspaces import services as wsvc

        roots = wsvc.tree_roots({u["workspace"] for u in invited.values()})

    changed = {n for n, r in existing.items() if _active_key(r).jkt != keys[n][1]}
    created: list[Runner] = []
    try:
        with transaction.atomic():
            if invitation is not None:
                locked = (RunnerInvitation.objects.select_for_update()
                          .filter(pk=invitation.pk, consumed_at__isnull=True).first())
                if locked is None:
                    raise refuse(f"invitation {invitation.pk} was consumed concurrently")
            for name, runner in existing.items():
                jwk, jkt, ecdh, ecdh_jkt, store = keys[name]
                _rekey(_active_key(runner), name, jwk, jkt, ecdh, ecdh_jkt, store)
                u = next(x for x in units if x.name == name)
                caps = declared_capabilities(u.capabilities)
                caps.pop("unit", None)
                prior_unit = runner.unit()
                if name in adopt:
                    # Adopted under the invitation a person approved: its unit is the
                    # one that invitation names — an agent unit is restricted at once.
                    spec = next(x for x in units_of(invitation) if x["name"] == name)
                    caps["unit"] = unit_capability(spec, roots.get(spec["workspace"], spec["workspace"]))
                elif prior_unit:
                    caps["unit"] = prior_unit
                Runner.objects.filter(pk=runner.pk).update(
                    host=u.host or runner.host, machine_id=machine,
                    capabilities={**(runner.capabilities or {}), **caps})
            if invitation is not None:
                invited = {x["name"]: x for x in units_of(invitation)}
                now = timezone.now()
                for u in new:
                    spec = invited[u.name]
                    jwk, jkt, ecdh, ecdh_jkt, store = keys[u.name]
                    caps = declared_capabilities(u.capabilities)
                    caps["unit"] = unit_capability(spec, roots.get(spec["workspace"], spec["workspace"]))
                    runner = Runner.objects.create(
                        name=u.name, kind=Runner.CLOUD, host=u.host, capabilities=caps,
                        owner=invitation.created_by, workspace_id=spec["workspace"], machine_id=machine)
                    # The unit's ECDH key is approved with the invitation when the
                    # invitation named it (up.sh read it from KMS); any other waits
                    # for a person, as a new ECDH key always does.
                    named = spec.get("ecdh_public_jwk") or {}
                    ecdh_approved = bool(ecdh_jkt) and bool(named) and \
                        device_keys.thumbprint(named) == ecdh_jkt
                    if not ecdh_jkt and named:
                        ecdh, ecdh_jkt, store, ecdh_approved = named, device_keys.thumbprint(named), \
                            BROKER_STORE, True
                    RunnerDeviceKey.objects.create(
                        runner=runner, kind=RunnerDeviceKey.SOFTWARE, public_jwk=jwk, jkt=jkt,
                        label=f"{u.name} (isolation unit key)", status=RunnerDeviceKey.ACTIVE,
                        created_by=invitation.created_by, approved_by=invitation.approved_by,
                        approved_at=invitation.approved_at,
                        ecdh_public_jwk=ecdh or {}, ecdh_jkt=ecdh_jkt, ecdh_store=store,
                        ecdh_approved_at=invitation.approved_at if ecdh_approved else None,
                        ecdh_approved_by=invitation.approved_by if ecdh_approved else None,
                    )
                    spec["runner_id"] = str(runner.pk)
                    created.append(runner)
                for name, runner in existing.items():   # re-attached in the same call
                    if name in invited:
                        invited[name]["runner_id"] = str(runner.pk)
                locked.units = [invited[x["name"]] for x in units_of(invitation)]
                locked.consumed_at = now
                locked.runner = created[0] if created else None
                locked.save(update_fields=["units", "consumed_at", "runner"])
    except IntegrityError:
        # A unit's key is already some other runner's (unique jkt), or a concurrent
        # pairing of the same box won.
        raise refuse(f"{role_arn}: a unit key is already registered, or a concurrent pairing won") from None

    for name, runner in existing.items():
        if name in changed:
            _announce_rekey(runner, name in adopt)
    for runner in created:
        activity.emit(activity.RUNNER_PAIRED, workspace=runner.workspace_id, actor=runner.owner,
                      surface="runner", ref=str(runner.pk),
                      meta={"name": runner.name, "invitation": str(invitation.pk), "unit": True})
    ids = {name: r.pk for name, r in existing.items()}
    ids.update({r.name: r.pk for r in created})
    key_ids = {r.name: k for r in [*existing.values(), *created]
               for k in [getattr(_active_key(r), "pk", None)] if k}
    first = created[0].pk if created else next(iter(ids.values()))
    status = "paired" if created else "reattached"
    return status_cls(201 if created else 200,
                      out_cls(status=status, runner_id=first, units=ids, device_keys=key_ids))
