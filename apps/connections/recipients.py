"""Who may open a connection: THE one rule (`recipients`), read by every seal,
reseal and write-back.

The owner's signed grant statement says which agents a connection is lent to
and which of the owner's own computers may use it (`docs/vault/zk-v1/
connections_v1_reference.py`, contract §0.3). A machine trusts that statement,
never this list; what this module computes can only NARROW it:

- an agent in the statement counts only while its grant is IN FORCE today
  (`grants_in_force`): it is still in the workspace whose app made the
  connection, and — for a person's connection — the person still administers it;
- an agent-owned connection reaches the machines that run each such agent and
  whose owner may hold it (`runner_may_hold_agent`);
- a person's reaches, per such agent, `accounts.delegated_devices` (cloud boxes
  that run it, or the person's own runners) — never another admin's laptop — and
  the person's own enrolled computers they listed in `my_computers`;
- plus the owner's member key, while it is still the key the envelope names
  (`owner_member`).

A recipient that stops qualifying is pruned from both envelopes without
decrypting anything (`prune`), except one active cloud box when no remaining
recipient could revoke it: a revoke still needs one.
"""
from __future__ import annotations

from django.db import transaction

from apps.agents import access
from apps.agents.credentials import accounts, vault_engine, zk_seal
from apps.agents.models import Agent
from apps.agents.services import runner_may_hold_agent
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerDeviceKey
from apps.passkeys.models import UserVaultKeyset

from . import wire
from .models import Connection

Recipient = tuple[str, str, dict]


def _statement_agent_pks(statement: dict | None) -> list[int]:
    return [int(a) for a in (statement or {}).get("agents", []) if str(a).isdigit()]


def owner_agent_usable(conn: Connection) -> bool:
    return conn.agent_id is None or conn.agent.workspace_id == conn.app.workspace_id


def _in_force(conn: Connection, pks) -> list[Agent]:
    """The agents among `pks` a grant of `conn` may reach today: in the app's
    workspace, and for a person's connection still administered by that person.
    An agent-owned connection whose agent has moved out of that workspace reaches
    nobody (`services.agent_connections_usable`)."""
    if not pks or not owner_agent_usable(conn):
        return []
    agents = list(Agent.objects.filter(pk__in=set(pks), workspace_id=conn.app.workspace_id).order_by("pk"))
    if conn.agent_id is not None or not agents:
        return agents
    return administered(conn.user, agents)


def administered(user, agents) -> list[Agent]:
    """The ones of `agents` that `user` administers, read once (`access.Standings`)."""
    agents = list(agents)
    if not agents or user is None:
        return []
    standings = access.Standings([user], agents)
    return [a for a in agents if standings.is_admin(user, a)]


def grants_in_force(conn: Connection) -> list[Agent]:
    """The agents `conn` may serve now: the signed statement's agents that also have
    a `ConnectionGrant` row (the owner agent counts as granted to itself), each
    still in force. A lender who stopped administering an agent drops it here
    while the statement still lists it."""
    granted = set(conn.grants.values_list("agent_id", flat=True))
    if conn.agent_id is not None:
        granted.add(conn.agent_id)
    return _in_force(conn, [pk for pk in _statement_agent_pks(conn.statement) if pk in granted])


def _ready(keys):
    return (keys.filter(status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False)
            .exclude(ecdh_jkt="").select_related("runner__owner"))


def _agent_devices(conn: Connection, agent: Agent) -> list[RunnerDeviceKey]:
    if conn.agent_id is not None:
        # An agent's own: every machine that runs it and whose owner may hold it. A
        # computer with no runner app runs no turn, so it is none of them.
        keys = _ready(device_keys.keys_for_agent(agent)).exclude(runner__kind=Runner.COMPUTER)
        return [k for k in keys if runner_may_hold_agent(k.runner, agent)]
    return accounts.delegated_devices(agent, conn.user)


def my_machines(user, jkts=None) -> list[RunnerDeviceKey]:
    """`user`'s own enrolled machines (ACTIVE, ECDH-approved keys on runners they own,
    any kind), limited to the thumbprints in `jkts` when given."""
    keys = _ready(RunnerDeviceKey.objects.filter(runner__owner=user)).exclude(runner__status=Runner.RETIRED)
    if jkts is not None:
        keys = keys.filter(ecdh_jkt__in=list(jkts))
    return [k for k in keys.order_by("runner__name", "ecdh_jkt")
            if accounts.may_hold_for(k.runner, user, own_use=True)]


def device_reasons(conn: Connection, statement: dict | None = None) -> list[tuple[RunnerDeviceKey, Agent | None]]:
    """Each device `statement` (default: the stored one, through `grants_in_force`)
    lets `conn` reach, with the agent that admits it (None: one of the owner's own
    computers). A device admitted twice is listed twice."""
    agents = _in_force(conn, _statement_agent_pks(statement)) if statement is not None else grants_in_force(conn)
    st = statement if statement is not None else (conn.statement or {})
    out: list[tuple[RunnerDeviceKey, Agent | None]] = [(k, a) for a in agents for k in _agent_devices(conn, a)]
    if conn.user_id is not None and st.get("my_computers"):
        out += [(k, None) for k in my_machines(conn.user, st["my_computers"])]
    return out


def devices(conn: Connection, statement: dict | None = None) -> list[RunnerDeviceKey]:
    """The machines `conn` may be sealed to under `statement` (default: the stored
    one), once each."""
    seen, out = set(), []
    for k, _ in device_reasons(conn, statement):
        if k.ecdh_jkt not in seen:
            seen.add(k.ecdh_jkt)
            out.append(k)
    return out


def owner_member(conn: Connection) -> Recipient | None:
    """`("member", user id, public JWK)` of the signer while their vault key is still
    the one the envelope names (`owner_key`); None once it is not (R10: another
    admin reconnects)."""
    if conn.signer_id is None or not conn.owner_key:
        return None
    ks = UserVaultKeyset.objects.filter(user_id=conn.signer_id).first()
    if ks is None or device_keys.thumbprint(ks.public_jwk) != conn.owner_key:
        return None
    return ("member", str(ks.user_id), ks.public_jwk)


def recipients(conn: Connection, statement: dict | None = None) -> list[Recipient]:
    """THE rule: everyone `conn` may be sealed to — the owner's member key and the
    machines `statement` (default: the stored one) still allows."""
    out = [("device", k.ecdh_jkt, k.ecdh_public_jwk) for k in devices(conn, statement)]
    owner = owner_member(conn)
    return ([owner] if owner else []) + out


def targets(rs: list[Recipient]) -> set[tuple[str, str]]:
    return {(kind, target) for kind, target, _ in rs}


def holders(conn: Connection) -> list[RunnerDeviceKey]:
    """The device keys the refresh envelope is sealed to now."""
    jkts = [t for kind, t in zk_seal.sealed_targets(conn.sealed) if kind == "device"]
    if not jkts:
        return []
    return list(RunnerDeviceKey.objects.filter(ecdh_jkt__in=jkts).select_related("runner").order_by("runner__name"))


def waiting(conn: Connection) -> list[dict]:
    """Machines the statement allows that do not hold it yet: `{agent, runner,
    target}` (`agent` is "" for one of the owner's own computers). Empty while
    `unshared` or `revoking`: nothing is waiting for a connection nobody may use."""
    if conn.state != Connection.ACTIVE:
        return []
    held = {t for kind, t in zk_seal.sealed_targets(conn.sealed) if kind == "device"}
    out, seen = [], set()
    for k, agent in device_reasons(conn):
        row = (agent.slug if agent else "", k.runner.name, k.ecdh_jkt)
        if k.ecdh_jkt not in held and row not in seen:
            seen.add(row)
            out.append({"agent": row[0], "runner": row[1], "target": row[2]})
    return out


def _revokers(jkts) -> list[str]:
    """Those of `jkts` that could revoke at the provider: an ACTIVE, ECDH-ready key on
    a cloud runner (only cloud runners keep or revoke). Sorted."""
    return sorted(RunnerDeviceKey.objects.filter(
        ecdh_jkt__in=list(jkts), status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False,
        runner__kind=Runner.CLOUD).exclude(ecdh_jkt="").exclude(runner__status=Runner.RETIRED)
        .values_list("ecdh_jkt", flat=True))


def _last_holder(kept: set[tuple[str, str]], dropped: set[tuple[str, str]]) -> tuple[str, str] | None:
    """The one dropped machine to keep so a revoke still has a holder: only when no
    kept recipient could revoke, and only a machine that could (`_revokers`). A
    revoked key, a laptop or a computer is never kept: with nothing able to revoke,
    the delete-at-once path applies instead."""
    if _revokers(t for kind, t in kept if kind == "device"):
        return None
    able = _revokers(t for kind, t in dropped if kind == "device")
    return ("device", able[0]) if able else None


def prune(conn: Connection) -> list[str]:
    """Drop from both envelopes (and their writer sigs) every recipient that may no
    longer open it: each recipient carries its own ciphertext, so this decrypts
    nothing. When no kept machine could revoke it, one dropped machine that could (an
    active cloud box) stays, so a revoke still has a holder. Only an `active` row:
    nothing while `unshared` (sealed to the owner alone) or `revoking` (its holders
    are revoking it). Returns what was dropped (runner names,
    `person <id>`); empty when nothing changed."""
    with transaction.atomic():
        fresh = Connection.objects.select_for_update().filter(pk=conn.pk).first()
        if fresh is None or fresh.state != Connection.ACTIVE or not fresh.sealed:
            return []
        allowed = targets(recipients(fresh))
        _, dropped = zk_seal.keep_only(fresh.sealed, allowed)
        if dropped:
            last = _last_holder(zk_seal.sealed_targets(fresh.sealed) - dropped, dropped)
            if last is not None:
                allowed.add(last)
        sealed, dropped = zk_seal.keep_only(fresh.sealed, allowed)
        access, access_dropped = zk_seal.keep_only(fresh.access_sealed, allowed)
        if (not dropped and not access_dropped) or not zk_seal.sealed_targets(sealed):
            return []
        fresh.sealed, fresh.sealed_digest = sealed, vault_engine.value_digest(sealed)
        fresh.sealed_sig = wire.keep_only_sig(fresh.sealed_sig, allowed)
        if access_dropped:
            if zk_seal.sealed_targets(access):
                fresh.access_sealed, fresh.access_sig = access, wire.keep_only_sig(fresh.access_sig, allowed)
            else:
                fresh.access_sealed, fresh.access_sig, fresh.access_expires_at = "", None, None
        fresh.save(update_fields=["sealed", "sealed_digest", "sealed_sig", "access_sealed", "access_sig",
                                  "access_expires_at", "updated_at"])
    conn.refresh_from_db()
    gone = dropped | access_dropped
    jkts = [t for kind, t in gone if kind == "device"]
    names = dict(RunnerDeviceKey.objects.filter(ecdh_jkt__in=jkts).values_list("ecdh_jkt", "runner__name"))
    return sorted({names.get(t, t) for t in jkts} | {f"person {t}" for kind, t in gone if kind == "member"})
