"""The owner shares a connection: a signed grant statement and an envelope their
browser re-sealed to the machines it allows.

canopy-web signs nothing, and opens the envelope only if the statement names it
(`canopy_web`), which only the owner can sign. The owner's browser opens
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
- the writer MAC list covers the envelope and names the owner as the writer;
- a `canopy_web` key the statement names is canopy-web's own current key, and the deployment
  accepts it (`CONNECTIONS_SERVER_STATEMENTS`): the statement then switches `server_use` on, and
  one that omits it switches it off. Naming only canopy-web is a valid first share.

Then the `ConnectionGrant` rows follow the statement, the row is `active`, and
recipients that no longer qualify are pruned.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction

from apps.agents.credentials import vault_engine, zk_endorse, zk_seal
from apps.agents.credentials.zk import ZkError
from apps.agents.models import Agent
from apps.harness import device_keys
from apps.passkeys.models import UserVaultKeyset

from . import providers, server_key, services, tokens, wire
from . import recipients as rcpt
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
                        "Only the vault key this connection was sealed to can share it, and yours is not that "
                        "key now. Disconnect it, then connect it again to share it yourself.")
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


def _agent_out(agent: Agent) -> dict:
    return {"id": str(agent.pk), "slug": agent.slug, "name": agent.name}


def server_state(conn: Connection) -> dict:
    """What the browser needs to offer canopy-web as a reader: whether its key can be read (a KMS
    failure is `available` false, never an error), the key, the row's switch, whether the deployment
    accepts it in a statement, whether the provider is one canopy-web calls, and `reachable`:
    whether the stored envelope is sealed to it now."""
    jwk = None
    if server_key.available():
        try:
            jwk = server_key.public_jwk()
        except server_key.Unavailable:
            jwk = None
    cls = providers.CLASSES.get(conn.provider)
    return {"available": jwk is not None, "thumbprint": device_keys.thumbprint(jwk) if jwk else "",
            "public_jwk": jwk, "on": conn.server_use,
            "statements_enabled": bool(getattr(settings, "CONNECTIONS_SERVER_STATEMENTS", False)),
            "provider_calls": bool(cls and cls.server_calls), "reachable": rcpt.server_reachable(conn)}


def share_state(conn: Connection, user) -> dict:
    """What the owner's browser needs to share `conn`: the envelopes, the stored
    statement, the owner's key, every recipient the owner could choose (each
    machine with the agent that admits it, or `mine` for their own computer) and
    the endorsements to check those machines and the envelopes' last writer by. Owner only."""
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
    endorsements = tokens.with_writer_endorsements(conn, [
        {**e, "agent_id": str(a.pk)} for a in agents for e in zk_endorse.endorsements(a)
        if e["kind"] == "device" and e["target"] in devices])
    return {
        "id": conn.pk, "binding": conn.binding, "provider": conn.provider, "account_id": conn.account_id,
        "account_label": conn.account_label, "state": conn.state,
        "sealed": conn.sealed, "sealed_sig": conn.sealed_sig, "sealed_digest": conn.sealed_digest,
        "access_sealed": conn.access_sealed, "access_sig": conn.access_sig,
        "statement": conn.statement, "statement_sig": conn.statement_sig,
        "endorsers": (conn.statement or {}).get("endorsers", []),
        "owner_agent": _agent_out(conn.agent) if conn.agent_id is not None else None,
        "owner": tokens._owner(conn),
        "candidates": candidates, "endorsements": endorsements,
        "agents": [_agent_out(a) for a in agents],
        "server": server_state(conn),
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
    # Under the lock below only the cache is read, so a statement that names canopy-web warms it first,
    # whatever the row's switch says now.
    rcpt.warm_server_key(conn, wanted=isinstance(statement, dict) and "canopy_web" in statement)
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
        named = _check_canopy_web(statement)
        agent_pks = [int(a) for a in statement["agents"]]
        _check_agents(conn, user, agent_pks)
        _check_machines(conn, user, statement["my_computers"])
        if conn.agent_id is not None and conn.agent_id not in agent_pks:
            raise WireError(422, "statement_missing_owner_agent",
                            f"The statement must list {conn.agent.slug}, whose connection this is.")
        if (expect or "").strip().lower() != conn.sealed_digest:
            raise WireError(409, "zk_value_changed",
                            "The connection changed since you read it; reload and share again.")
        # The statement decides the switch, and decides it before the recipients are worked out: a row
        # that was off and now names canopy-web may be sealed to it.
        switched = conn.server_use != named
        conn.server_use = named
        allowed = rcpt.targets(rcpt.recipients(conn, statement, cached_only=True))
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
        conn.owner_endorsements = rcpt.snapshot_owner_endorsements(conn)
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
    if switched:
        _record_switch(subject, conn, user)
    rcpt.prune(conn)
    return conn


def _check_canopy_web(statement: dict) -> bool:
    """Whether `statement` names canopy-web, after checking it may: the deployment accepts it and it
    is exactly canopy-web's current key. 422 `bad_canopy_web` otherwise; 503 `try_again` while the key
    is not cached (this runs under a row lock, so KMS is not asked): a key we cannot read yet is not
    a forged one."""
    if "canopy_web" not in statement:
        return False
    if not getattr(settings, "CONNECTIONS_SERVER_STATEMENTS", False) or not server_key.available():
        raise WireError(422, "bad_canopy_web",
                        "canopy-web can't be given this connection here yet, so leave \"Project screens and "
                        "apps\" off and share again.")
    jwk = server_key.cached_public_jwk()
    if jwk is None:
        raise WireError(503, "try_again", "canopy-web could not check its own key just now; try sharing again "
                                          "in a moment.")
    if statement["canopy_web"] != device_keys.thumbprint(jwk):
        raise WireError(422, "bad_canopy_web",
                        "That is not canopy-web's current key; reload the page and share again.")
    return True


def _record_switch(subject: services.Subject, conn: Connection, user) -> None:
    on = conn.server_use
    services.record(subject, "server_use_on" if on else "server_use_off", by=user,
                    summary=f"{conn.owner_label} turned {'on' if on else 'off'} canopy-web's use of "
                            f"{conn.provider} ({conn.account_label})")


def turn_off_server_use(conn: Connection, *, user) -> Connection:
    """Give up canopy-web's access: it leaves both envelopes and the switch goes off. The owner's key
    and the stored statement are untouched, so no passkey: giving up access is always easy. Only a
    row with no statement in force (`unshared`, even one that kept an old statement from before a
    reconnect, which decides nothing): on an `active` row canopy-web is a recipient of the signed
    statement, so removing it needs a new one (409 `needs_share`: go through Share). Already off
    changes nothing. 409 `revoking` while it is being disconnected."""
    with transaction.atomic():
        row = Connection.objects.select_for_update(of=("self",)).select_related("app", "agent").get(pk=conn.pk)
        _not_revoking(row)
        if not row.server_use:
            return row
        if row.state == Connection.ACTIVE and row.statement:
            raise WireError(409, "needs_share",
                            "This connection is shared, so turning canopy-web off means sharing it again "
                            "without \"Project screens and apps\". That needs your passkey.")
        row.server_use = False
        allowed = rcpt.targets(rcpt.recipients(row))  # the switch is off: canopy-web is not among them
        row.sealed, _ = zk_seal.keep_only(row.sealed, allowed)
        row.sealed_digest = vault_engine.value_digest(row.sealed)
        row.sealed_sig = wire.keep_only_sig(row.sealed_sig, allowed)
        if row.access_sealed:
            row.access_sealed, _ = zk_seal.keep_only(row.access_sealed, allowed)
            if zk_seal.sealed_targets(row.access_sealed):
                row.access_sig = wire.keep_only_sig(row.access_sig, allowed)
            else:
                row.access_sealed, row.access_sig, row.access_expires_at = "", None, None
        row.save(update_fields=["server_use", "sealed", "sealed_digest", "sealed_sig", "access_sealed",
                                "access_sig", "access_expires_at", "updated_at"])
    _record_switch(services.Subject.of(row), row, user)
    return row


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
