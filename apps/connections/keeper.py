"""Keepers re-seal, holders revoke, nothing waits forever (spec §4.1, §4.5; contract
§0.6 routes 13-15; rulings R2, R7, R12).

A cloud box's keeper (`canopy vault zk keep`, every 120 s) asks for connection
work (`work`): each connection one of its device keys holds that is either
`active` with machines the statement allows still waiting for a copy, or
`revoking`. For the first it re-seals the SAME plaintext to the waiting machines
(`reseal`, compare-and-swap on `sealed_digest`, checked as a write-back is); for
the second it revokes at the provider (only when `revoke_at_provider`) and
confirms (`confirm_revoked`), which deletes the row. Only cloud runners keep or
revoke (R3): a laptop holding a copy gets no work and may not confirm. canopy-web's
own key, where a row is sealed to it, revokes too (`server_tokens.revoke_if_able`), but keeps
nothing and is no machine.

`sweep` (the off-request clock, `scheduler.py`, and `manage.py connections_sweep`)
retries canopy-web's own revoke, then deletes what no holder will ever finish: a `revoking`
row unconfirmed after `REVOKE_GRACE`, or one neither canopy-web nor any cloud box holds any more,
each recorded `revoke_unconfirmed`; and a removed app once its connections are gone (R12).

This is its own route in `apps/connections` (R2), not a field on the agents
keeper's answer: agents and harness never import connections.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.agents.credentials import vault_engine, zk_seal
from apps.harness.models import Runner, RunnerDeviceKey

from . import recipients as rcpt
from . import server_key, server_tokens, services, tokens, wire
from .models import Connection, ConnectionApp
from .wire import WireError

#: How long a `revoking` row waits for a holder to confirm before the sweep deletes it.
REVOKE_GRACE = timedelta(days=7)
#: canopy-web's own revokes in one sweep, and how long a row waits before it is tried again: each
#: can cost KMS and provider timeouts, and the tick is shared.
SERVER_REVOKES_PER_TICK = 25
SERVER_REVOKE_EVERY_S = 60 * 60


def _keeps(runner: Runner) -> bool:
    """Only an active cloud box keeps or revokes (R3)."""
    return runner.kind == Runner.CLOUD and runner.status != Runner.RETIRED


def _not_a_holder(why: str) -> WireError:
    return WireError(403, "zk_not_a_holder", why)


def work(runner: Runner) -> list[dict]:
    """`ConnectionKeeperWorkOut` for every connection a device key of `runner` holds
    that needs it: `revoking` (revoke and confirm), or `active` with machines waiting
    (`recipients.waiting`) while this box is itself still one the statement allows
    (a box `prune` kept only so it can revoke re-seals for nobody). The material is
    `tokens.material_fields`: the endorsements of every agent in force and of the
    device that last wrote each envelope (even one since pruned, so this box can still
    validate what it re-seals), each tagged `agent_id`. Nothing for a box that does
    not keep. Each connection's `device_reasons` is read once, and one read of the
    fleet serves them all."""
    if not _keeps(runner):
        return []
    keys = {k.ecdh_jkt: k for k in RunnerDeviceKey.objects.filter(
        runner=runner, status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False)
        .exclude(ecdh_jkt="").select_related("runner")}
    if not keys:
        return []
    out = []
    rows = (Connection.objects.filter(state__in=[Connection.ACTIVE, Connection.REVOKING]).exclude(sealed="")
            .select_related("app__workspace", "agent", "user").order_by("pk"))
    held_rows = []
    for conn in rows:
        held = sorted(t for kind, t in zk_seal.sealed_targets(conn.sealed) if kind == "device" and t in keys)
        if held:
            held_rows.append((conn, keys[held[0]]))
    facts = rcpt.facts_for([conn for conn, _ in held_rows])
    for conn, key in held_rows:
        revoking = conn.state == Connection.REVOKING
        pairs = rcpt.device_reasons(conn, facts=facts)
        waiting: list[str] = []
        if not revoking:
            if key.ecdh_jkt not in {k.ecdh_jkt for k, _ in pairs}:
                continue
            waiting = sorted({w["target"] for w in rcpt.waiting(conn, pairs=pairs)})
            if not waiting:
                continue
        out.append({
            **tokens.material_fields(conn, key, pairs, facts),
            "waiting": waiting,
            "revoking": revoking,
            # Decided now, not at the disconnect (spec §4.5): revoke at the provider
            # exactly when no other live connection of the account through the app
            # remains — one that appeared since would die with it, and one that was
            # deleted since without a revoke must not leave this token live.
            "revoke_at_provider": revoking and not services.other_live_connections(conn).exists(),
        })
    return out


def _names(jkts) -> list[str]:
    names = dict(RunnerDeviceKey.objects.filter(ecdh_jkt__in=list(jkts)).values_list("ecdh_jkt", "runner__name"))
    return sorted({names.get(t, t) for t in jkts})


def reseal(conn: Connection, runner: Runner, device_key: RunnerDeviceKey, *, sealed: str, sealed_sig,
           access_sealed: str = "", access_sig=None, expect: str) -> list[str]:
    """Replace `conn`'s envelopes with the same plaintext re-sealed by a box that holds
    it, for the machines that were waiting. Returns the machines it is now sealed to.

    `device_key`'s machine must be a cloud box holding it before AND after (403
    `zk_not_a_holder`); 409 `revoking` while it is being disconnected; `expect` must
    be the stored `sealed_digest` (409 `zk_value_changed`); and each envelope is
    checked as a write-back's is (`tokens.check_write`: 422 `foreign_reader`,
    `zk_rewrap_required`, `bad_writer_sig`). A stored access envelope must be
    re-sealed with it (422 `zk_rewrap_required`). Records `resealed`."""
    mine = ("device", device_key.ecdh_jkt)
    rcpt.warm_server_key(conn)
    with transaction.atomic():
        row = (Connection.objects.select_for_update(of=("self",)).select_related("app", "agent", "user")
               .get(pk=conn.pk))
        if row.state == Connection.REVOKING:
            raise WireError(409, "revoking", "This connection is being disconnected.")
        old = zk_seal.sealed_targets(row.sealed)
        if (not _keeps(runner) or row.state != Connection.ACTIVE or mine not in old
                or mine not in zk_seal.sealed_targets(sealed)):
            raise _not_a_holder("This machine holds no copy of the connection to re-seal.")
        if (expect or "").strip().lower() != row.sealed_digest:
            raise WireError(409, "zk_value_changed", "The connection changed since you read it; read it again.")
        allowed = rcpt.targets(rcpt.recipients(row, cached_only=True))
        keep = old & allowed
        allowed |= rcpt.unconfirmed_server_target(row)  # an unreadable key refuses nobody's write
        tokens.check_write(sealed, sealed_sig, name=wire.REFRESH_NAME, allowed=allowed, keep=keep,
                           writer=device_key, expect=row.sealed_digest)
        if row.access_sealed:
            if not access_sealed:
                raise WireError(422, "zk_rewrap_required",
                                "access must be re-sealed with refresh, to the same machines.")
            tokens.check_write(access_sealed, access_sig, name=wire.ACCESS_NAME, allowed=allowed, keep=keep,
                               writer=device_key, expect=vault_engine.value_digest(row.access_sealed))
            row.access_sealed, row.access_sig = access_sealed, access_sig
        elif access_sealed:
            raise WireError(409, "zk_value_changed", "The connection changed since you read it; read it again.")
        row.sealed, row.sealed_sig, row.sealed_digest = sealed, sealed_sig, vault_engine.value_digest(sealed)
        row.owner_endorsements = rcpt.snapshot_owner_endorsements(row)
        row.save(update_fields=["sealed", "sealed_sig", "sealed_digest", "access_sealed", "access_sig",
                                "owner_endorsements", "updated_at"])
    now = {t for kind, t in zk_seal.sealed_targets(row.sealed) if kind == "device"}
    added = _names(now - {t for kind, t in old if kind == "device"})
    services.record(services.Subject.of(row), "resealed", machine=device_key.runner.name, added=",".join(added),
                    summary=f"{row.owner_label}'s {tokens._title(row)} re-sealed by {device_key.runner.name} "
                            f"for {', '.join(added) or 'nobody new'}")
    return _names(now)


def confirm_revoked(conn: Connection, runner: Runner, device_key: RunnerDeviceKey, *, revoked: bool,
                    error: str = "") -> None:
    """A holder revoked `conn` at the provider (or was told not to, `revoke_at_provider`
    false) — `revoked` — or tried and failed (`error`). Either way the row is deleted:
    the holder did what it could. 409 `not_revoking` unless it is being disconnected;
    403 `zk_not_a_holder` unless this cloud box holds it. Records `revoked`, or the
    operational `revoke_failed`."""
    def holder_only(row: Connection) -> None:
        if not _keeps(runner) or ("device", device_key.ecdh_jkt) not in zk_seal.sealed_targets(row.sealed):
            raise _not_a_holder("Only a cloud box that holds the connection revokes it.")

    services.finish_revoking(conn, machine=device_key.runner.name, revoked=revoked, error=error, check=holder_only)


def sweep(now: datetime | None = None) -> dict:
    """Try canopy-web's own revoke of the `revoking` rows sealed to its key (`_server_revokes`), then
    delete what no holder will ever finish: each `revoking` row unconfirmed for `REVOKE_GRACE`, or that neither
    canopy-web nor a cloud box still heard from holds (`recipients.revokers`), recorded
    `revoke_unconfirmed`; then each removed app with no connection left — after
    disconnecting any still live there (one that slipped past `remove_app`), so a
    removal always finishes. First, every lend whose time has run out is disconnected, so a
    holder revokes it at the provider, or canopy-web does: many at once, so with no revoke in that
    call, and `_server_revokes` below takes it this same tick and retries it on later ones. Returns the counts `{lapsed, revoke_unconfirmed,
    apps_deleted}`."""
    now = now or timezone.now()
    lapsed = 0
    for conn in (Connection.objects.filter(expires_at__lte=now).exclude(state=Connection.REVOKING)
                 .select_related("app__workspace", "agent__workspace").order_by("pk")):
        services.disconnect(conn, by=None, revoke_now=False, lapsed=True)
        lapsed += 1
    gone = 0
    _server_revokes()
    for conn in (Connection.objects.filter(state=Connection.REVOKING)
                 .select_related("app", "agent__workspace", "user").order_by("pk")):
        ours = server_tokens.may_revoke(conn)
        stale = conn.revoking_since is None or conn.revoking_since <= now - REVOKE_GRACE
        if not stale and (ours or rcpt.revokers(conn)):
            continue
        subject = services.Subject.of(conn)
        deleted, _ = Connection.objects.filter(pk=conn.pk, state=Connection.REVOKING).delete()
        if not deleted:
            continue
        gone += 1
        reason = "unconfirmed" if stale else "no_holder"
        services.record(subject, "revoke_unconfirmed", warn=True, reason=reason,
                        summary=f"{subject.owner}'s {subject.provider} ({subject.account_label}) deleted: "
                                + ("no machine confirmed revoking it" if stale else "no machine holds it any more"))
    apps = sum(_finish_removal(app)
               for app in list(ConnectionApp.objects.filter(removed_at__isnull=False).order_by("pk")))
    return {"lapsed": lapsed, "revoke_unconfirmed": gone, "apps_deleted": apps}


def _server_revokes() -> None:
    """At most `SERVER_REVOKES_PER_TICK` of canopy-web's own revokes, oldest first, each row at most
    once in `SERVER_REVOKE_EVERY_S` so failing rows can't hold up the rest. KMS down stops them all
    until the next tick, and costs that row no wait: it says nothing about the row."""
    tried = 0
    for conn in (Connection.objects.filter(state=Connection.REVOKING)
                 .select_related("app", "agent__workspace", "user")
                 .order_by(F("revoking_since").asc(nulls_first=True), "pk")):
        if tried >= SERVER_REVOKES_PER_TICK:
            return
        key = f"connections:revoke-attempt:{conn.pk}"
        if not server_tokens.may_revoke(conn) or not cache.add(key, 1, SERVER_REVOKE_EVERY_S):
            continue
        tried += 1
        try:
            server_tokens.revoke_if_able(conn, stop_on_kms=True)
        except server_key.Unavailable:
            cache.delete(key)
            return


def _finish_removal(app: ConnectionApp) -> bool:
    """Disconnect what is still live on removed `app`, then delete it if nothing is
    left. Under the app's lock, re-read: an admin who set the app up again since the
    sweep listed it (`configure_app` clears `removed_at` under the same lock) keeps
    it and every connection made through it. True when the app was deleted."""
    with transaction.atomic():
        locked = ConnectionApp.objects.select_for_update().filter(pk=app.pk, removed_at__isnull=False).first()
        if locked is None:
            return False
        for conn in (Connection.objects.filter(app=locked).exclude(state=Connection.REVOKING)
                     .select_related("app__workspace", "agent__workspace").order_by("pk")):
            # Holding the app's lock: canopy-web's own revoke waits for the next sweep.
            services.disconnect(conn, by=None, revoke_now=False)
        return services.delete_removed_app(locked)
