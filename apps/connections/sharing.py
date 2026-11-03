"""The owner shares a connection: a signed grant statement and an envelope their
browser re-sealed to the machines it allows.

canopy-web never opens the envelope and never signs. The owner's browser opens
the refresh token with their vault key, signs the statement (contract §0.3) and
seals the token again to its owner and the devices the statement allows, with
a writer MAC per recipient (§0.4). Here canopy-web checks what it can:

- the signer is the owner — the holder of `owner_key`, which is sealed inside
  the envelope at connect, so a machine checks the same key (R10);
- the signature verifies, and the version only moves forward;
- every agent the statement ADDS is one the signer administers, in the app's
  workspace (one already lent may stay listed after its lender stopped
  administering it: `recipients.grants_in_force` drops it);
- `my_computers` are the signer's own enrolled machines (none for an agent's);
- `endorsers` is well formed; whom to trust is the owner's to say, and `recipients`
  holds the envelope to it;
- the write replaces what the browser read (`expect`), and its recipients are
  the owner and machines the statement allows (`recipients`), nobody else;
- the writer MAC list covers the envelope and names the owner as the writer.

Then the `ConnectionGrant` rows follow the statement, the row is `active`, and
recipients that no longer qualify are pruned.
"""
from __future__ import annotations

from django.db import transaction

from apps.agents.credentials import vault_engine, zk_endorse, zk_seal
from apps.agents.credentials.zk import ZkError
from apps.agents.models import Agent
from apps.harness import device_keys
from apps.passkeys.models import UserVaultKeyset

from . import recipients as rcpt
from . import services, wire
from .models import Connection, ConnectionGrant
from .wire import WireError


def visible(conn: Connection, user) -> bool:
    """May `user` see that `conn` exists: their own, or an agent's they administer.
    Anyone else gets a 404, so ids cannot be probed."""
    if conn.user_id is not None:
        return conn.user_id == user.pk
    return conn.agent.is_admin(user)


def _owner_keyset(conn: Connection, user, by_jkt: str | None = None) -> UserVaultKeyset:
    """`user`'s keyset when they hold `owner_key` (and `by_jkt` names it); 403
    `not_the_owner_key` otherwise. Only that key re-signs (R10)."""
    ks = UserVaultKeyset.objects.filter(user=user).first()
    jkt = device_keys.thumbprint(ks.public_jwk) if ks is not None else ""
    if (ks is None or conn.signer_id != user.pk or not conn.owner_key or jkt != conn.owner_key
            or (by_jkt is not None and by_jkt != jkt)):
        raise WireError(403, "not_the_owner_key",
                        "Only the person whose vault key this connection was sealed to can share it. "
                        "Disconnect it, then connect it again to share it yourself.")
    return ks


def _not_revoking(conn: Connection) -> None:
    if conn.state == Connection.REVOKING:
        raise WireError(409, "revoking", "This connection is being disconnected.")


def _choosable(conn: Connection, user) -> list[Agent]:
    """The agents `user` may lend `conn` to: those they administer in the app's
    workspace (for an agent's connection, its own agent among them)."""
    if conn.agent_id is not None and not rcpt.owner_agent_usable(conn):
        return []
    return rcpt.administered(user, Agent.objects.filter(workspace_id=conn.app.workspace_id).order_by("slug"))


def share_state(conn: Connection, user) -> dict:
    """What the owner's browser needs to share `conn`: the envelopes, the stored
    statement, the owner's key, every recipient the owner could choose (each
    machine with the agent that admits it, or `mine` for their own computer) and
    the endorsements to check those machines by. Owner only."""
    _not_revoking(conn)
    ks = _owner_keyset(conn, user)
    agents = _choosable(conn, user)
    candidates = [{"kind": "member", "target": str(user.pk), "public_jwk": ks.public_jwk, "agent_id": "",
                   "runner_name": "", "mine": True}]
    candidates += [{"kind": "device", "target": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk,
                    "agent_id": str(a.pk) if a else "", "runner_name": k.runner.name,
                    "mine": k.runner.owner_id == user.pk}
                   for k, a in rcpt.candidate_reasons(conn, agents, user)]
    devices = {c["target"] for c in candidates if c["kind"] == "device"}
    endorsements = [{**e, "agent_id": str(a.pk)} for a in agents for e in zk_endorse.endorsements(a)
                    if e["kind"] == "device" and e["target"] in devices]
    return {
        "id": conn.pk, "binding": conn.binding, "provider": conn.provider, "account_id": conn.account_id,
        "account_label": conn.account_label, "state": conn.state,
        "sealed": conn.sealed, "sealed_sig": conn.sealed_sig, "sealed_digest": conn.sealed_digest,
        "access_sealed": conn.access_sealed, "access_sig": conn.access_sig,
        "statement": conn.statement, "endorsers": (conn.statement or {}).get("endorsers", []),
        "owner": {"user_id": str(user.pk), "public_jwk": ks.public_jwk},
        "candidates": candidates, "endorsements": endorsements,
        "agents": [{"id": str(a.pk), "slug": a.slug, "name": a.name} for a in agents],
    }


def _check_agents(conn: Connection, user, agent_pks: list[int]) -> None:
    kept = set(conn.grants.values_list("agent_id", flat=True))
    added = [pk for pk in agent_pks if pk not in kept]
    if not added:
        return
    found = {a.pk: a for a in Agent.objects.filter(pk__in=added)}
    mine = {a.pk for a in rcpt.administered(user, found.values())}
    for pk in added:
        if pk not in mine:
            raise WireError(403, "not_an_admin", "You can lend a connection only to agents you administer.")
        if found[pk].workspace_id != conn.app.workspace_id:
            raise WireError(422, "agent_outside_workspace",
                            f"{found[pk].slug} is not in the workspace whose app made this connection.")


def _check_machines(conn: Connection, user, jkts: list[str]) -> None:
    if not jkts:
        return
    if conn.agent_id is not None:
        raise WireError(422, "not_your_machine", "An agent's connection is used only by the machines that run it.")
    mine = {k.ecdh_jkt for k in rcpt.my_machines(user, jkts)}
    if set(jkts) - mine:
        raise WireError(422, "not_your_machine",
                        "Use on my computers lists only your own enrolled computers "
                        "(canopy vault device --enroll).")


def _check_envelope(value, sig, *, name: str, allowed: set, user, ks, expect: str, must_include=()) -> None:
    try:
        zk_seal.check_sealed_value(value, name)
    except ZkError as exc:
        raise WireError(422, exc.code, str(exc)) from None
    given = zk_seal.sealed_targets(value)
    stray = sorted(t for _, t in given - allowed)
    if stray or not set(must_include) <= given:
        raise WireError(422, "foreign_reader",
                        f"{name} may be sealed only to you and the machines the statement allows"
                        + (f": {', '.join(stray)}" if stray else "; it must include you"))
    if sig is None:
        raise WireError(422, "bad_writer_sig", f"{name}: every envelope you write carries its writer MACs.")
    wire.check_writer_sig(value, sig)
    by = sig["by"]
    if by["kind"] != "member" or by["target"] != str(user.pk) or device_keys.thumbprint(by["public_jwk"]) != \
            device_keys.thumbprint(ks.public_jwk):
        raise WireError(422, "bad_writer_sig", f"{name}: the writer must be you, by your vault key.")
    if sig["expect"] != expect:
        raise WireError(422, "bad_writer_sig", f"{name}: the writer MACs name another value to replace.")


def share(conn: Connection, *, user, statement: dict, sig: str, by_jkt: str, sealed: str, sealed_sig,
          access_sealed: str | None = None, access_sig=None, expect: str) -> Connection:
    """Store the owner's signed statement and the envelope their browser sealed to
    it; the row becomes `active`. Every refusal is a `WireError` (contract §0.6
    route 4)."""
    with transaction.atomic():
        conn = Connection.objects.select_for_update(of=("self",)).select_related("app", "agent").get(pk=conn.pk)
        _not_revoking(conn)
        ks = _owner_keyset(conn, user, by_jkt)
        wire.check_statement(statement, connection_id=conn.pk, account_id=conn.account_id)
        if not wire.verify_statement(statement, sig, ks.public_jwk):
            raise WireError(422, "bad_signature", "The statement's signature does not verify against your key.")
        if statement["version"] <= conn.statement_version:
            raise WireError(409, "statement_stale",
                            f"This connection is already shared at version {conn.statement_version}; "
                            "reload and share again.")
        agent_pks = [int(a) for a in statement["agents"]]
        _check_agents(conn, user, agent_pks)
        _check_machines(conn, user, statement["my_computers"])
        if conn.agent_id is not None and conn.agent_id not in agent_pks:
            raise WireError(422, "statement_missing_owner_agent",
                            f"The statement must list {conn.agent.slug}, whose connection this is.")
        if (expect or "").strip().lower() != conn.sealed_digest:
            raise WireError(409, "zk_value_changed",
                            "The connection changed since you read it; reload and share again.")
        allowed = rcpt.targets(rcpt.recipients(conn, statement))
        owner = ("member", str(user.pk))
        _check_envelope(sealed, sealed_sig, name=wire.REFRESH_NAME, allowed=allowed, user=user, ks=ks,
                        expect=conn.sealed_digest, must_include=[owner])
        if access_sealed:
            _check_envelope(access_sealed, access_sig, name=wire.ACCESS_NAME, allowed=allowed, user=user, ks=ks,
                            expect=vault_engine.value_digest(conn.access_sealed))

        conn.sealed, conn.sealed_sig, conn.sealed_digest = sealed, sealed_sig, vault_engine.value_digest(sealed)
        if access_sealed:
            conn.access_sealed, conn.access_sig = access_sealed, access_sig
        conn.statement, conn.statement_sig, conn.statement_version = statement, sig, statement["version"]
        conn.state = Connection.ACTIVE
        conn.save()
        added, removed = _follow_statement(conn, agent_pks, by=user)

    subject = services.Subject.of(conn)
    for agent in added:
        services.record_granted(subject, agent, by=user)
    for agent in removed:
        services.record_grant_revoked(subject, agent, by=user)
    services.record(subject, "shared", by=user, version=statement["version"],
                    summary=f"{conn.owner_label} shared {conn.provider} ({conn.account_label}), "
                            f"version {statement['version']}")
    rcpt.prune(conn)
    return conn


def _follow_statement(conn: Connection, agent_pks: list[int], *, by) -> tuple[list[Agent], list[Agent]]:
    """Make the `ConnectionGrant` rows the statement's agents (the owner agent is
    its own, never a grant). Returns `(added, removed)` agents."""
    want = set(agent_pks) - {conn.agent_id}
    have = {g.agent_id: g for g in conn.grants.select_related("agent__workspace")}
    removed = [have[pk].agent for pk in sorted(set(have) - want)]
    ConnectionGrant.objects.filter(connection=conn, agent_id__in=[a.pk for a in removed]).delete()
    added = []
    for agent in Agent.objects.filter(pk__in=want - set(have)).order_by("slug"):
        ConnectionGrant.objects.create(connection=conn, agent=agent, granted_by=by)
        added.append(agent)
    return added, removed
