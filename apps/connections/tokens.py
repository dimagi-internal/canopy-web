"""What a machine needs to use a connection, the one refresh lease, and the
compare-and-swap write-back (spec §4.2; contract §0.6 routes 7-12).

Here canopy-web sees no token. A machine the owner's statement allows reads the
MATERIAL: both envelopes, the signed statement, the owner's key, the
endorsements, the recipients, and the app's client id and secret (the one
place the secret leaves canopy-web). It opens the envelopes itself; when the
access token is too close to expiry it takes the LEASE (one holder, 60 s,
renewable — answered with `ttl_seconds` so a box with a skewed clock renews on
its own monotonic clock), refreshes at the provider and WRITES BACK a new access
envelope (and refresh envelope, when the refresh token rotated) against the
`sealed_digest` it read. Another machine that wants a token waits for that
write instead of refreshing in parallel. canopy-web, when a connection is sealed to its own key,
takes the same lease and writes back the same way (`server_tokens`), as a `Writer`.

Admission is decided here, from `recipients` (THE rule), never from who the
envelope happens to be sealed to: `prune` keeps one dropped cloud box only so
it can revoke, and a lender who stopped administering an agent leaves its boxes
holding a copy they must no longer be handed (`admits_*`).

Every refusal is a `wire.WireError` (`problems.problem_for_wire`).
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.agents.credentials import delegations, vault_engine, zk_endorse, zk_seal
from apps.agents.credentials.zk import ZkError
from apps.harness.models import RunnerDeviceKey

from . import providers, server_key, services, wire
from . import recipients as rcpt
from .models import Connection
from .wire import WireError

LEASE_SECONDS = 60
#: The one provider error that means the stored refresh token is dead.
INVALID_GRANT = "invalid_grant"


def _title(conn: Connection) -> str:
    return getattr(providers.CLASSES.get(conn.provider), "title", conn.provider)


def _not_connected(conn: Connection) -> WireError:
    return WireError(409, "not_connected", f"This {_title(conn)} connection is not shared with this machine.")


def _not_revoking(conn: Connection) -> None:
    if conn.state == Connection.REVOKING:
        raise WireError(409, "revoking", "This connection is being disconnected.")


# ---- admission ----------------------------------------------------------------------

def _reasons(conn: Connection, key: RunnerDeviceKey, pairs=None) -> set[int | None]:
    """Why the statement in force lets `key` open `conn` now: the pk of each in-force
    agent that admits it, and None when it is one of the owner's own computers."""
    pairs = rcpt.device_reasons(conn) if pairs is None else pairs
    return {a.pk if a is not None else None for k, a in pairs if k.ecdh_jkt == key.ecdh_jkt}


def admits_runner(conn: Connection, key: RunnerDeviceKey) -> None:
    """A box may take the lease and write back only while it serves an agent the
    connection is in force for (`recipients`: a machine that runs it and whose owner
    may hold it, `runner_may_hold_agent`); 403 `not_a_recipient` otherwise."""
    if not any(r is not None for r in _reasons(conn, key)):
        raise WireError(403, "not_a_recipient",
                        "This machine does not serve an agent this connection is shared with.")


def admits_my_computer(conn: Connection, key: RunnerDeviceKey) -> None:
    """A person's CLI may use their connection only on a computer the statement
    lists (`Use on my computers`); 403 `not_on_my_computers` otherwise."""
    if None not in _reasons(conn, key):
        label = conn.account_label or conn.account_id
        raise WireError(403, "not_on_my_computers",
                        f"Turn on Use on my computers for {label} in Settings → Connections.")


# ---- material -----------------------------------------------------------------------

def _lease_view(conn: Connection, key: RunnerDeviceKey, now: datetime) -> dict | None:
    if conn.lease_id is None or conn.lease_expires_at is None or conn.lease_expires_at <= now:
        return None
    return {"mine": conn.lease_holder == key.ecdh_jkt,
            "ttl_seconds": max(0, math.ceil((conn.lease_expires_at - now).total_seconds()))}


def _owner(conn: Connection) -> dict:
    # The key as it was at connect, so a signer who replaced theirs or was deleted leaves a
    # connection machines can still read and revoke. A machine trusts it only because its
    # thumbprint is the `owner_key` sealed in the envelope.
    return {"user_id": conn.owner_user_id, "public_jwk": conn.owner_public_jwk}


def _record_issued(conn: Connection, key: RunnerDeviceKey, *, turn, runner) -> None:
    subject = services.Subject.of(conn)
    title = _title(conn)
    if turn is None:
        services.record(subject, "token_issued", by=conn.user, to="cli", machine=key.runner.name,
                        summary=f"{conn.owner_label}'s {title} opened on {key.runner.name}")
        return
    agent = delegations.turn_agent(turn)
    runner = runner or key.runner
    # One turn's repeated reads (its heartbeat re-stages) coalesce into one Event.
    services.record(subject, "token_issued", to="turn", agent=agent.slug, turn=str(turn.pk),
                    runner=str(runner.pk), machine=key.runner.name,
                    key=f"token_issued:{conn.provider}:{agent.slug}:{turn.pk}",
                    summary=f"{conn.owner_label}'s {title} for {agent.slug}'s turn on {runner.name}")


def material(conn: Connection, device_key: RunnerDeviceKey, *, turn=None, runner=None) -> dict:
    """Everything `device_key`'s machine needs to use `conn` (`ConnectionMaterialOut`),
    and the `token_issued` record of handing it out.

    With `turn`, for that turn's agent: the machine must be admitted by THAT agent's
    grant in force, else 409 `not_connected`. Without, a person's own CLI: the
    machine must be one of the owner's listed computers, else 403
    `not_on_my_computers`. Also 409 `not_connected` while `unshared` or `revoking`,
    409 `needs_reconnect` once a refresh was rejected, and 409 `waiting` for a
    machine the statement allows that holds no copy yet."""
    if conn.state != Connection.ACTIVE:
        raise _not_connected(conn)
    facts = rcpt.facts_for([conn])
    pairs = rcpt.device_reasons(conn, facts=facts)
    reasons = _reasons(conn, device_key, pairs)
    if turn is not None:
        agent = delegations.turn_agent(turn)
        if agent is None or agent.pk not in reasons:
            raise _not_connected(conn)
    else:
        admits_my_computer(conn, device_key)
    if conn.needs_reconnect:
        raise WireError(409, "needs_reconnect",
                        f"This {_title(conn)} connection no longer works; its owner must connect it again.")
    if ("device", device_key.ecdh_jkt) not in zk_seal.sealed_targets(conn.sealed):
        raise WireError(409, "waiting",
                        "This machine may use the connection but holds no copy yet: a machine that holds it "
                        "gives it one, or the owner does from Settings.")

    out = material_fields(conn, device_key, pairs, facts)
    if turn is not None:
        out.update(agent_id=str(agent.pk), agent_slug=agent.slug)
    now = timezone.now()
    Connection.objects.filter(pk=conn.pk).update(last_used_at=now)
    _record_issued(conn, device_key, turn=turn, runner=runner)
    return out


def material_fields(conn: Connection, device_key: RunnerDeviceKey, pairs=None, facts=None) -> dict:
    """`ConnectionMaterialOut` for `conn` as `device_key`'s machine reads it, with no
    admission check and no record: `material` (a machine about to use it) and the
    keeper (`keeper.work`: a box that holds it, re-sealing or revoking) both answer it.
    `recipients` and the endorsements are those of the statement in force
    (`pairs`, default `recipients.device_reasons`; `facts`, what they were read with),
    plus what vouches for the last writer (`with_writer_endorsements`)."""
    pairs = rcpt.device_reasons(conn) if pairs is None else pairs
    devices, seen = [], set()
    for k, _ in pairs:
        if k.ecdh_jkt not in seen:
            seen.add(k.ecdh_jkt)
            devices.append(k)
    owner = rcpt.owner_member(conn)
    recipients = ([{"kind": "member", "target": owner[1], "public_jwk": owner[2], "runner_name": ""}] if owner
                  else [])
    recipients += [{"kind": "device", "target": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk,
                    "runner_name": k.runner.name} for k in devices]
    # A machine seals only to who is listed here, and a write-back must keep whoever may still
    # open it (`check_write`): left out, canopy-web would make every refresh refused.
    server = rcpt.server_recipient(conn)
    if server is not None:
        recipients.append({"kind": server[0], "target": server[1], "public_jwk": server[2],
                           "runner_name": "canopy-web"})
    agents = {a.pk: a for _, a in pairs if a is not None}
    endorsements = with_writer_endorsements(conn, [
        {**e, "agent_id": str(a.pk)} for a in agents.values() for e in rcpt.endorsements_for(a, facts)
        if e["kind"] == "device" and e["target"] in seen])
    return {
        "id": conn.pk, "provider": conn.provider, "workspace": conn.app.workspace_id, "binding": conn.binding,
        "account_id": conn.account_id, "account_label": conn.account_label,
        "sealed": conn.sealed, "sealed_sig": conn.sealed_sig, "sealed_digest": conn.sealed_digest,
        "access_sealed": conn.access_sealed, "access_sig": conn.access_sig,
        "access_expires_at": conn.access_expires_at,
        "statement": conn.statement, "statement_sig": conn.statement_sig, "owner": _owner(conn),
        "endorsements": endorsements, "recipients": recipients,
        "app": {"client_id": conn.app.client_id, "client_secret": conn.app.client_secret,
                "options": dict(conn.app.options)},
        "lease": _lease_view(conn, device_key, timezone.now()),
        "rotates_refresh_token": bool(getattr(providers.CLASSES.get(conn.provider), "rotates_refresh_token",
                                              False)),
    }


def with_writer_endorsements(conn: Connection, endorsements: list[dict]) -> list[dict]:
    """`endorsements` (each with its `agent_id`) plus what vouches for the device that last
    wrote each envelope, for every agent the statement lists, each once. A reader must
    validate the writer to open it, or nobody could re-seal it after that device was
    pruned; its key travels in the sig's `by`. Machines and the owner's browser both read this."""
    writers = {sig["by"]["target"] for sig in (conn.sealed_sig, conn.access_sig)
               if sig and (sig.get("by") or {}).get("kind") == "device"}
    agent_pks = rcpt.statement_agent_pks(conn.statement)
    have = {(e["agent_id"], e["by"], e["target"]) for e in endorsements}
    # None left (say the key was deleted): this still answers; the reader refuses.
    return rcpt.as_signed(conn, endorsements + [
        e for jkt in sorted(writers) for e in zk_endorse.endorsements_of_device(agent_pks, jkt)
        if (e["agent_id"], e["by"], e["target"]) not in have])


# ---- the lease ----------------------------------------------------------------------

@dataclass(frozen=True)
class Writer:
    """Who holds the lease and writes an envelope when it is not a `RunnerDeviceKey`: canopy-web's
    own key, which no runner carries. `jkt` is what `lease_holder` and a writer sig's `by.target`
    name, `name` what a record shows, `refreshed` the kind its write-back records."""

    jkt: str
    name: str
    refreshed: str = "refreshed"


def _jkt(who: RunnerDeviceKey | Writer) -> str:
    return who.jkt if isinstance(who, Writer) else who.ecdh_jkt


def _name(who: RunnerDeviceKey | Writer) -> str:
    return who.name if isinstance(who, Writer) else who.runner.name


def _locked(conn: Connection) -> Connection:
    return Connection.objects.select_for_update(of=("self",)).select_related("app", "agent").get(pk=conn.pk)


def _same_lease(row: Connection, who: RunnerDeviceKey | Writer, lease_id) -> bool:
    return (lease_id is not None and row.lease_id is not None and str(row.lease_id) == str(lease_id)
            and row.lease_holder == _jkt(who))


def _is_canopy_web(jkt: str) -> bool:
    try:
        return server_key.available() and server_key.thumbprint() == jkt
    except server_key.Unavailable:
        return False


def _machine_name(jkt: str) -> str:
    """What a record calls a lease holder. Asks KMS at most for a holder no runner carries, so
    never call it holding a row lock."""
    name = RunnerDeviceKey.objects.filter(ecdh_jkt=jkt).values_list("runner__name", flat=True).first()
    return name or ("canopy-web" if _is_canopy_web(jkt) else jkt)


def take_lease(conn: Connection, device_key: RunnerDeviceKey | Writer, lease_id=None) -> dict:
    """Take (or, with the `lease_id` this machine holds, renew) the one refresh lease
    for `LEASE_SECONDS`. Another live holder — another machine, or another process on
    this one without the id — is 409 `lease_held` with `retry_after` seconds: wait
    for its write-back instead of refreshing in parallel. Taking over a lease that
    expired unwritten records `lease_expired_unwritten` (its holder died between
    refreshing and writing back). 409 `revoking` while being disconnected."""
    with transaction.atomic():
        row = _locked(conn)
        _not_revoking(row)
        now = timezone.now()
        live = row.lease_id is not None and row.lease_expires_at is not None and row.lease_expires_at > now
        same = _same_lease(row, device_key, lease_id)
        if live and not same:
            raise WireError(409, "lease_held", "Another machine is refreshing this connection; wait for its "
                                               "new access token.",
                            retry_after=max(1, math.ceil((row.lease_expires_at - now).total_seconds())))
        crashed = row.lease_holder if (not same and row.lease_id is not None and not row.lease_written) else ""
        if not same:
            row.lease_id, row.lease_holder, row.lease_written = uuid.uuid4(), _jkt(device_key), False
        row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        row.save(update_fields=["lease_id", "lease_holder", "lease_written", "lease_expires_at", "updated_at"])
    if crashed:
        services.record(services.Subject.of(row), "lease_expired_unwritten", warn=True,
                        machine=_machine_name(crashed), taken_by=_name(device_key),
                        summary=f"{row.owner_label}'s {_title(row)} lease expired before "
                                f"{_machine_name(crashed)} wrote back")
    return {"lease_id": row.lease_id, "ttl_seconds": LEASE_SECONDS, "expires_at": row.lease_expires_at}


def release_lease(conn: Connection, device_key: RunnerDeviceKey | Writer, lease_id) -> None:
    """Give the lease up; a no-op unless this machine holds `lease_id`."""
    with transaction.atomic():
        row = _locked(conn)
        if _same_lease(row, device_key, lease_id):
            row.lease_id, row.lease_holder, row.lease_expires_at = None, "", None
            row.save(update_fields=["lease_id", "lease_holder", "lease_expires_at", "updated_at"])


def _check_lease(row: Connection, key: RunnerDeviceKey | Writer, lease_id) -> None:
    # R13: an expired lease still counts while nobody took it since (taking it
    # replaces the id).
    if not _same_lease(row, key, lease_id):
        raise WireError(409, "lease_lost", "Another machine took the refresh lease; read the material again.")


# ---- write-back ---------------------------------------------------------------------

def check_write(value, sig, *, name: str, allowed: set, keep: set, writer: RunnerDeviceKey | Writer,
                expect: str) -> None:
    """One envelope a machine writes (a write-back, or a keeper's re-seal): well
    formed, sealed only to `allowed` (422 `foreign_reader`), still to everyone in
    `keep` (422 `zk_rewrap_required`), with writer MACs by `writer`'s ECDH key over
    exactly the value `expect` names (422 `bad_writer_sig`)."""
    try:
        zk_seal.check_sealed_value(value, name)
    except ZkError as exc:
        raise WireError(422, exc.code, str(exc)) from None
    given = zk_seal.sealed_targets(value)
    stray = sorted(t for _, t in given - allowed)
    if stray:
        raise WireError(422, "foreign_reader",
                        f"{name} may be sealed only to the owner and the machines the statement allows: "
                        f"{', '.join(stray)}")
    missing = sorted(t for _, t in keep - given)
    if missing:
        raise WireError(422, "zk_rewrap_required",
                        f"{name} must keep everyone who may still open it: {', '.join(missing)}")
    if sig is None:
        raise WireError(422, "bad_writer_sig", f"{name}: every envelope a machine writes carries its writer MACs.")
    wire.check_writer_sig(value, sig)
    by = sig["by"]
    if by["kind"] != "device" or by["target"] != _jkt(writer):
        raise WireError(422, "bad_writer_sig", f"{name}: the writer must be this machine, by its ECDH key.")
    if sig["expect"] != expect:
        raise WireError(422, "bad_writer_sig", f"{name}: the writer MACs name another value to replace.")


def write_back(conn: Connection, *, device_key: RunnerDeviceKey | Writer, lease_id, access_sealed: str, access_sig,
               access_expires_at: datetime, expect: str, rotated: bool, sealed: str | None = None,
               sealed_sig=None) -> str:
    """Store what the lease holder minted: the new access envelope and, when the
    refresh token `rotated`, the new refresh envelope. Returns the `sealed_digest`.

    Refused while `revoking` (409, nothing stored), unless this machine still holds
    `lease_id` (409 `lease_lost`), unless `expect` is the stored digest (409
    `zk_value_changed`), for a recipient the rule does not allow (422
    `foreign_reader`), for dropping one who may still open it (422
    `zk_rewrap_required`), and for writer MACs that are not this machine's over
    exactly the value replaced (422 `bad_writer_sig`)."""
    if rotated != (sealed is not None):
        raise WireError(422, "rotation_mismatch",
                        "Send `sealed` exactly when the refresh token rotated (`rotated`).")
    rcpt.warm_server_key(conn)
    with transaction.atomic():
        row = _locked(conn)
        _not_revoking(row)
        _check_lease(row, device_key, lease_id)
        if (expect or "").strip().lower() != row.sealed_digest:
            raise WireError(409, "zk_value_changed", "The connection changed since you read it; read it again.")
        allowed = rcpt.targets(rcpt.recipients(row, cached_only=True))
        keep = zk_seal.sealed_targets(row.sealed) & allowed
        # Never refused because canopy-web's key can't be read: this machine's new token may be
        # the only copy.
        allowed |= rcpt.unconfirmed_server_target(row)
        check_write(access_sealed, access_sig, name=wire.ACCESS_NAME, allowed=allowed, keep=keep,
                     writer=device_key, expect=vault_engine.value_digest(row.access_sealed))
        if sealed is not None:
            check_write(sealed, sealed_sig, name=wire.REFRESH_NAME, allowed=allowed, keep=keep,
                         writer=device_key, expect=row.sealed_digest)
            row.sealed, row.sealed_sig, row.sealed_digest = sealed, sealed_sig, vault_engine.value_digest(sealed)
        row.access_sealed, row.access_sig, row.access_expires_at = access_sealed, access_sig, access_expires_at
        row.lease_written = True
        row.save(update_fields=["sealed", "sealed_sig", "sealed_digest", "access_sealed", "access_sig",
                                "access_expires_at", "lease_written", "updated_at"])
    # A day's refreshes of one connection coalesce into one Event (its count says how many);
    # a rotation is its own Event so it is never overwritten by a plain refresh.
    kind = device_key.refreshed if isinstance(device_key, Writer) else "refreshed"
    services.record(services.Subject.of(row), kind, rotated=bool(rotated), machine=_name(device_key),
                    key=f"{kind}:{row.pk}:{'rotated' if rotated else 'plain'}:{timezone.now().date().isoformat()}",
                    summary=f"{row.owner_label}'s {_title(row)} refreshed on {_name(device_key)}"
                            + (" (refresh token rotated)" if rotated else ""))
    return row.sealed_digest


def mark_reconnect_required(conn: Connection, device_key: RunnerDeviceKey | Writer, lease_id, error: str,
                            detail: str = "") -> None:
    """The lease holder's refresh was refused at the provider. Only `invalid_grant`
    (the refresh token is dead) marks the connection `needs_reconnect`, ends the lease
    (nothing was refreshed, so the next machine to take it has no holder to suspect)
    and records `reconnect_required`; anything else is canopy's or the provider's
    trouble, a `provider_error` log line. `detail` is the provider's own words, scrubbed by
    the machine, because one code can mean several things (Salesforce's `ip restricted` is
    `invalid_grant` too). 409 `lease_lost` unless this machine holds `lease_id`."""
    dead = error == INVALID_GRANT
    with transaction.atomic():
        row = _locked(conn)
        _check_lease(row, device_key, lease_id)
        if dead:
            row.refresh_failed_at = timezone.now()
            row.lease_id, row.lease_holder, row.lease_expires_at = None, "", None
            row.save(update_fields=["refresh_failed_at", "lease_id", "lease_holder", "lease_expires_at",
                                    "updated_at"])
    subject = services.Subject.of(row)
    if dead:
        services.record(subject, "reconnect_required", warn=True, error=error, detail=detail,
                        machine=_name(device_key),
                        summary=f"{row.owner_label}'s {_title(row)} was refused on refresh; connect it again")
    else:
        services.record(subject, "provider_error", warn=True, during="refresh", error=error, detail=detail,
                        machine=_name(device_key))
