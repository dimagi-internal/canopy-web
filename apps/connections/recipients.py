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
- a machine admitted through an agent counts only while the owner's key or a
  person the statement lists in `endorsers` vouches for its exact key for that
  agent (`_vouched`, contract §0.5): what is listed here is never something a box
  would refuse;
- plus the owner's member key, the one the envelope names (`owner_member`), and
  canopy-web's own key while the switch is on (`server_recipient`). That one is a `device`
  target with no `RunnerDeviceKey` and no runner: it is never a machine (`device_reasons`,
  `holders`, `waiting`) and, here, never a revoker (`_revokers`).

A recipient that stops qualifying is pruned from both envelopes without
decrypting anything (`prune`), except one active cloud box when no remaining
recipient could revoke it: a revoke still needs one.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.agents import access
from apps.agents.credentials import accounts, vault_engine, zk_endorse, zk_seal
from apps.agents.models import Agent, AgentVaultEndorsement
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerDeviceKey
from apps.harness.routing import ServingFacts
from apps.passkeys.models import UserVaultKeyset

from . import server_key, wire
from .models import Connection

Recipient = tuple[str, str, dict]

#: How long a holder may go unheard before it counts as gone, so nothing will revoke through
#: it. Far longer than a runner's own online window, which answers "can it take work now", not
#: "will it ever come back": a reboot must not lose a revoke, and a canopy-web outage leaves
#: every box unheard at once, when a sweep must not delete every `revoking` row unrevoked.
REVOKER_GONE_AFTER = timedelta(minutes=15)


def statement_agent_pks(statement: dict | None) -> list[int]:
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
    return _in_force(conn, [pk for pk in statement_agent_pks(conn.statement) if pk in granted])


def lent_in_force(agent: Agent, conns) -> set:
    """The pks of `conns`, each with a grant row for `agent`, whose grant is in force now
    (`grants_in_force`, for one agent across many connections, reading who administers
    it once)."""
    conns = [c for c in conns if str(agent.pk) in (c.statement or {}).get("agents", [])
             and owner_agent_usable(c) and agent.workspace_id == c.app.workspace_id]
    people = {c.user_id: c.user for c in conns if c.user_id is not None}
    standings = access.Standings(list(people.values()), [agent]) if people else None
    return {c.pk for c in conns if c.user_id is None or standings.is_admin(c.user, agent)}


def _ready(keys):
    return (keys.filter(status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False)
            .exclude(ecdh_jkt="").select_related("runner__owner"))


def facts_for(conns) -> ServingFacts | None:
    """One read of the fleet and each agent's facts for everything `conns`' stored
    statements name, to pass to the calls below when asking about the same connections
    several times (each of them otherwise reads its own). Read per request and dropped
    with it, so never across a write. None when no statement names an agent."""
    pks = {pk for c in conns for pk in statement_agent_pks(c.statement)}
    agents = list(Agent.objects.filter(pk__in=pks)) if pks else []
    return device_keys.serving_facts(agents) if agents else None


def endorsements_for(agent: Agent, facts: ServingFacts | None = None) -> list[dict]:
    """`zk_endorse.endorsements(agent)`, read once per `facts`."""
    if facts is None:
        return zk_endorse.endorsements(agent)
    return facts.once(("endorsements", agent.pk), lambda: zk_endorse.endorsements(agent, facts))


def _verifies_as_owners(conn: Connection, agent_id, e: dict) -> bool:
    return zk_endorse.verify(agent_id, kind="device", target=e["target"], key_jkt=e["key_jkt"], sig=e["sig"],
                             endorser_jwk=conn.owner_public_jwk)


def as_signed(conn: Connection, endorsements: list[dict], agent_id=None) -> list[dict]:
    """`endorsements` as the owner signed them. An endorsement names its endorser's CURRENT
    key, so one signed before they replaced theirs would verify against nothing, and one
    re-signed since is under a key the envelope does not name: either way every machine they
    vouched for would drop out. So the owner's is served under the key stored at connect, or,
    when that no longer verifies, as the row last snapshotted it (`owner_endorsements`).
    Nothing is believed because of this: a reader still verifies the signature under the key
    served and takes the signer's thumbprint from it. `agent_id`: the agent these are for,
    when they are not tagged."""
    if not conn.owner_key or not conn.owner_public_jwk:
        return endorsements
    kept = {(e["agent_id"], e["target"]): e for e in conn.owner_endorsements or []}
    out = []
    for e in endorsements:
        if e.get("by") != conn.owner_user_id and e.get("by_jkt") != conn.owner_key:
            out.append(e)
            continue
        agent = str(e.get("agent_id") or agent_id)
        if e.get("by_jkt") == conn.owner_key and _verifies_as_owners(conn, agent, e):
            out.append({**e, "by_public_jwk": conn.owner_public_jwk})
        else:
            out.append({**kept.get((agent, e["target"]), e)})
    return out


def snapshot_owner_endorsements(conn: Connection) -> list[dict]:
    """`conn.owner_endorsements` as it should be now that a share or a re-seal landed: what the
    owner signed for the statement's agents and still verifies, on top of what was kept before,
    which is what survives a re-endorsement under another key."""
    agents = statement_agent_pks(conn.statement)
    wanted = {str(a) for a in agents}
    kept = {(e["agent_id"], e["target"]): e for e in conn.owner_endorsements or [] if e["agent_id"] in wanted}
    rows = (AgentVaultEndorsement.objects.filter(agent_id__in=agents, kind="device", endorser_jkt=conn.owner_key)
            .select_related("endorser"))
    for r in rows:
        e = {"kind": r.kind, "target": r.target, "key_jkt": r.key_jkt, "by": str(r.endorser_id),
             "by_email": r.endorser.email, "by_jkt": r.endorser_jkt, "by_public_jwk": conn.owner_public_jwk,
             "sig": r.sig, "agent_id": str(r.agent_id)}
        if r.target == r.key_jkt and _verifies_as_owners(conn, r.agent_id, e):
            kept[(e["agent_id"], e["target"])] = e
    return [kept[k] for k in sorted(kept)]


def _agent_devices(conn: Connection, agent: Agent, facts: ServingFacts) -> list[RunnerDeviceKey]:
    if conn.agent_id is not None:
        # An agent's own: every machine that runs it and whose owner may hold it. A
        # computer with no runner app runs no turn, so it is none of them.
        keys = _ready(device_keys.keys_for_agent(agent, facts)).exclude(runner__kind=Runner.COMPUTER)
        return [k for k in keys if facts.may_hold(k.runner, agent)]
    return accounts.delegated_devices(agent, conn.user, facts)


def my_machines(user, jkts=None) -> list[RunnerDeviceKey]:
    """`user`'s own enrolled machines (ACTIVE, ECDH-approved keys on runners they own,
    any kind), limited to the thumbprints in `jkts` when given."""
    keys = _ready(RunnerDeviceKey.objects.filter(runner__owner=user)).exclude(runner__status=Runner.RETIRED)
    if jkts is not None:
        keys = keys.filter(ecdh_jkt__in=list(jkts))
    return [k for k in keys.order_by("runner__name", "ecdh_jkt")
            if accounts.may_hold_for(k.runner, user, own_use=True)]


def _vouched(conn: Connection, agent: Agent, trusted: set[str], facts: ServingFacts) -> set[str]:
    """The ECDH thumbprints of the machines a person key in `trusted` vouches for, for
    `agent`, by the check a box makes (contract §0.5) on the endorsements it is handed:
    the endorser is who the key that verifies the signature says, not what a row claims."""
    out = set()
    for e in as_signed(conn, endorsements_for(agent, facts), agent.pk):
        jwk = e["by_public_jwk"]
        if (e["kind"] == "device" and e["target"] == e["key_jkt"] and jwk
                and device_keys.thumbprint(jwk) in trusted
                and zk_endorse.verify(agent.pk, kind="device", target=e["target"], key_jkt=e["key_jkt"],
                                      sig=e["sig"], endorser_jwk=jwk)):
            out.add(e["target"])
    return out


def device_reasons(conn: Connection, statement: dict | None = None,
                   facts: ServingFacts | None = None) -> list[tuple[RunnerDeviceKey, Agent | None]]:
    """Each device `statement` (default: the stored one, through `grants_in_force`)
    lets `conn` reach, with the agent that admits it (None: one of the owner's own
    computers). A device admitted twice is listed twice. `facts` (`facts_for`) must
    cover the statement's agents."""
    agents = _in_force(conn, statement_agent_pks(statement)) if statement is not None else grants_in_force(conn)
    st = statement if statement is not None else (conn.statement or {})
    trusted = {conn.owner_key, *st.get("endorsers", [])} - {""}
    out: list[tuple[RunnerDeviceKey, Agent | None]] = []
    facts = facts or (device_keys.serving_facts(agents) if agents else None)
    for a in agents:
        vouched = _vouched(conn, a, trusted, facts)
        out += [(k, a) for k in _agent_devices(conn, a, facts) if k.ecdh_jkt in vouched]
    if conn.user_id is not None and st.get("my_computers"):
        out += [(k, None) for k in my_machines(conn.user, st["my_computers"])]
    return out


def candidate_reasons(conn: Connection, agents, user) -> list[tuple[RunnerDeviceKey, Agent | None]]:
    """What the owner `user` could choose among: every machine of each of `agents`, and
    for a person's connection their own computers, with the agent that admits it (None:
    their computer). Not narrowed by who vouches: that is the owner's browser's to
    decide (it names them in `endorsers`), so it is shown what it has not yet trusted."""
    agents = list(agents)
    facts = device_keys.serving_facts(agents) if agents else None
    out: list[tuple[RunnerDeviceKey, Agent | None]] = [(k, a) for a in agents
                                                       for k in _agent_devices(conn, a, facts)]
    if conn.user_id is not None:
        out += [(k, None) for k in my_machines(user)]
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
    """`("member", user id, public JWK)` of the key the envelope names (`owner_key`), as
    stored at connect. It stays while the row does, even once the signer replaced that key
    or was deleted: a machine seals to it because its thumbprint is `owner_key`, and the
    way off it is to disconnect and connect again (R10). None for a row with no such key."""
    if not conn.owner_user_id or not conn.owner_public_jwk or not conn.owner_key:
        return None
    return ("member", conn.owner_user_id, conn.owner_public_jwk)


def server_recipient(conn: Connection, statement: dict | None = None, *,
                     cached_only: bool = False) -> Recipient | None:
    """`("device", thumbprint, public JWK)` of canopy-web's key while `conn` may be sealed to it:
    the switch is on, the key is configured, and a statement in force names exactly this key. A
    row `unshared` with no statement passed needs none: the one it kept from before a reconnect
    decides nothing. A `statement` passed is the one about to be in force, so it decides even
    there. None while the key can't be read (a machine can't seal to a key it wasn't shown);
    `unconfirmed_server_target` keeps that from costing canopy-web its copy. `cached_only`: a
    caller holding a row lock, where a key not yet cached counts as unreadable (no KMS call)."""
    if not conn.server_use or not server_key.available():
        return None
    any_statement = statement is None and conn.state == Connection.UNSHARED
    named = (conn.statement or {} if statement is None else statement).get("canopy_web")
    if not any_statement and not named:  # asks KMS nothing
        return None
    if cached_only:
        jwk = server_key.cached_public_jwk()
    else:
        try:
            jwk = server_key.public_jwk()
        except server_key.Unavailable:
            jwk = None
    if jwk is None:
        return None
    jkt = device_keys.thumbprint(jwk)
    return ("device", jkt, jwk) if any_statement or named == jkt else None


def unconfirmed_server_target(conn: Connection) -> set[tuple[str, str]]:
    """canopy-web's target as the stored statement names it, when nothing can say it is the wrong
    key: the key is unset, or not cached (KMS failing, or not read yet). Only the owner's statement,
    or a key readable and different, drops canopy-web, so a `prune` keeps it and a write may still
    reach it, though no write must. Reads the cache only: callers hold a row lock."""
    named = (conn.statement or {}).get("canopy_web")
    if not (named and conn.server_use and conn.state == Connection.ACTIVE):
        return set()
    if not server_key.available() or server_key.cached_public_jwk() is None:
        return {("device", named)}
    return set()


def server_reachable(conn: Connection) -> bool:
    """Whether the stored refresh envelope is sealed to canopy-web's key now, so that it can use the
    connection. False when the key can't be read: the answer is then unknown, and that is not a
    reason to say it can."""
    if not server_key.available():
        return False
    jwk = server_key.cached_public_jwk()
    if jwk is None:
        try:
            jwk = server_key.public_jwk()
        except server_key.Unavailable:
            return False
    return ("device", device_keys.thumbprint(jwk)) in zk_seal.sealed_targets(conn.sealed)


def warm_server_key(conn: Connection, *, wanted: bool = False) -> None:
    """Read canopy-web's public key into the process's cache before a caller takes `conn`'s row
    lock, since under the lock only the cache is read (`cached_only`). A best effort: `conn` may
    be stale and KMS may be down, and a miss then only counts as unavailable for that write.
    `wanted`: the caller means to switch it on, whatever the row says now."""
    if (conn.server_use or wanted) and server_key.available():
        try:
            server_key.public_jwk()
        except server_key.Unavailable:
            pass


def recipients(conn: Connection, statement: dict | None = None, *, cached_only: bool = False) -> list[Recipient]:
    """THE rule: everyone `conn` may be sealed to — the owner's member key, the machines
    `statement` (default: the stored one) still allows, and canopy-web (`server_recipient`).
    `cached_only`: the caller holds a row lock, so no KMS call."""
    out = [("device", k.ecdh_jkt, k.ecdh_public_jwk) for k in devices(conn, statement)]
    owner = owner_member(conn)
    server = server_recipient(conn, statement, cached_only=cached_only)
    return ([owner] if owner else []) + out + ([server] if server else [])


def owner_keys_replaced(conns) -> set:
    """The pks of `conns` sealed to a key their signer no longer holds: new tokens stay readable
    by the old key until the row is disconnected. Not a signer who was deleted, who holds none."""
    conns = [c for c in conns if c.signer_id is not None and c.owner_key]
    keys = dict(UserVaultKeyset.objects.filter(user_id__in={c.signer_id for c in conns})
                .values_list("user_id", "public_jwk")) if conns else {}
    return {c.pk for c in conns if c.signer_id in keys and device_keys.thumbprint(keys[c.signer_id]) != c.owner_key}


def targets(rs: list[Recipient]) -> set[tuple[str, str]]:
    return {(kind, target) for kind, target, _ in rs}


def holders(conn: Connection) -> list[RunnerDeviceKey]:
    """The device keys the refresh envelope is sealed to now."""
    jkts = [t for kind, t in zk_seal.sealed_targets(conn.sealed) if kind == "device"]
    if not jkts:
        return []
    return list(RunnerDeviceKey.objects.filter(ecdh_jkt__in=jkts).select_related("runner").order_by("runner__name"))


def waiting(conn: Connection, *, pairs=None, facts: ServingFacts | None = None) -> list[dict]:
    """Machines the statement allows that do not hold it yet: `{agent, runner,
    target}` (`agent` is "" for one of the owner's own computers). Empty while
    `unshared` or `revoking`: nothing is waiting for a connection nobody may use.
    `pairs`: this connection's `device_reasons`, when the caller already read them."""
    if conn.state != Connection.ACTIVE:
        return []
    held = {t for kind, t in zk_seal.sealed_targets(conn.sealed) if kind == "device"}
    out, seen = [], set()
    for k, agent in (device_reasons(conn, facts=facts) if pairs is None else pairs):
        row = (agent.slug if agent else "", k.runner.name, k.ecdh_jkt)
        if k.ecdh_jkt not in held and row not in seen:
            seen.add(row)
            out.append({"agent": row[0], "runner": row[1], "target": row[2]})
    return out


def unreachable(conn: Connection, *, pairs=None, facts: ServingFacts | None = None) -> list[dict]:
    """The agents the stored statement lends `conn` to that no machine can receive it for
    (`{agent, reason}`, by slug), by the same rule as `device_reasons`. `reason`:
    `lapsed` (its grant is no longer in force), `no_machine` (nothing runs it that may
    hold it), `unvouched` (something does, and nobody the statement names vouches for it).
    Empty unless `active`. `pairs`: this connection's `device_reasons`, when already read."""
    if conn.state != Connection.ACTIVE:
        return []
    pairs = device_reasons(conn, facts=facts) if pairs is None else pairs
    missing = set(statement_agent_pks(conn.statement)) - {a.pk for _, a in pairs if a is not None}
    if not missing:
        return []
    in_force = {a.pk: a for a in grants_in_force(conn) if a.pk in missing}
    facts = facts or (device_keys.serving_facts(list(in_force.values())) if in_force else None)
    out = []
    for slug, pk in Agent.objects.filter(pk__in=missing).order_by("slug").values_list("slug", "pk"):
        if pk not in in_force:
            out.append({"agent": slug, "reason": "lapsed"})
        else:
            out.append({"agent": slug,
                        "reason": "unvouched" if _agent_devices(conn, in_force[pk], facts) else "no_machine"})
    return out


def _revokers(jkts, *, up: bool = False) -> list[str]:
    """Those of `jkts` that could revoke at the provider: an ACTIVE, ECDH-ready key on
    a cloud runner (only cloud runners keep or revoke). `up`: and that runner has been heard
    from within `REVOKER_GONE_AFTER`. Sorted."""
    heard = timezone.now() - REVOKER_GONE_AFTER
    keys = (RunnerDeviceKey.objects.filter(
        ecdh_jkt__in=list(jkts), status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False,
        runner__kind=Runner.CLOUD).exclude(ecdh_jkt="").exclude(runner__status=Runner.RETIRED)
        .select_related("runner"))
    return sorted(k.ecdh_jkt for k in keys if not up or (k.runner.last_heartbeat_at or heard) > heard)


def _writer_provable(conn: Connection, sig: dict | None) -> bool:
    """Could a machine accept the envelope `sig` belongs to? Its writer must be the owner's
    key, one of the statement's computers, canopy-web's key if the statement names it, or a
    device a key the statement names vouches for: the check a box makes before it uses, or
    revokes, anything (contract §0.4)."""
    by = (sig or {}).get("by") or {}
    if by.get("kind") == "member":
        return by.get("target") == conn.owner_user_id
    target = by.get("target")
    if by.get("kind") != "device" or not target:
        return False
    st = conn.statement or {}
    if target in st.get("my_computers", []) or target == st.get("canopy_web"):
        return True
    trusted = {conn.owner_key, *st.get("endorsers", [])} - {""}
    for e in as_signed(conn, zk_endorse.endorsements_of_device(statement_agent_pks(st), target)):
        jwk = e["by_public_jwk"]
        if (jwk and device_keys.thumbprint(jwk) in trusted
                and zk_endorse.verify(e["agent_id"], kind="device", target=target, key_jkt=target, sig=e["sig"],
                                      endorser_jwk=jwk)):
            return True
    return False


def revokers(conn: Connection) -> list[str]:
    """The machines holding `conn` that could revoke it now: an active cloud box it is
    sealed to and not long gone (`_revokers`), and an envelope that box can accept. A signer
    deleted with their endorsements can leave one no machine will read, and a stopped box
    revokes nothing: waiting on either would refuse a reconnect for days. None means
    nothing will: a disconnect deletes it at once, and the sweep any it was waiting on."""
    able = _revokers((t for kind, t in zk_seal.sealed_targets(conn.sealed) if kind == "device"), up=True)
    if not able or not _writer_provable(conn, conn.sealed_sig):
        return []
    if conn.access_sealed and not _writer_provable(conn, conn.access_sig):
        return []
    return able


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
    are revoking it). canopy-web's key goes only because the statement stopped naming it or a
    readable key differs, never because its key can't be read or is unset
    (`unconfirmed_server_target`). Returns what was dropped (runner names, `person <id>`);
    empty when nothing changed."""
    warm_server_key(conn)
    with transaction.atomic():
        fresh = Connection.objects.select_for_update().filter(pk=conn.pk).first()
        if fresh is None or fresh.state != Connection.ACTIVE or not fresh.sealed:
            return []
        allowed = targets(recipients(fresh, cached_only=True)) | unconfirmed_server_target(fresh)
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
