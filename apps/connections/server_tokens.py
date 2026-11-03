"""canopy-web opens, refreshes and revokes a sealed connection itself, through its KMS key.

The spec's "canopy-web using a token" and "Disconnecting". While the owner's switch is on
(`Connection.server_use`), a connection's envelopes name canopy-web as one more `device` recipient
(`recipients.server_recipient`). `access_token` then answers from, in order:

1. this process's cache, kept 5 minutes at most, never past the token's life, keyed by the
   access envelope's digest, so anything written since is a miss;
2. the access envelope, opened through `server_key.derive`, when it has a minute left;
3. a refresh, under the one lease machines take (`tokens`), written back under the same
   compare-and-swap. A lease someone else holds is waited for, up to 30 s, without a lock.

ORDER, for every refresh: take the lease (it commits); open, and check KMS can still seal to today's
recipients, since a rotation can't be taken back; refresh, holding nothing; seal and compute the MACs,
holding nothing; write under the row lock with compare-and-swap. A KMS or provider call never runs
in a transaction or under a row lock. A conflict re-reads the row and seals again with the token already
in hand. A rotated refresh token exists only here until it is stored, so its write keeps trying, as
the CLI's does, for `ROTATED_WRITE_SECONDS` across KMS, database and conflict failures, and the
provider is never asked twice.

WHAT IS OPENED IS CHECKED, as a machine checks it (`orchestrator/connections/trust.py`): the writer's
MAC verifies and the writer is the owner, canopy-web, or a device the statement vouches for; the
plaintext is exactly the contract's fields, for this row's owner key, account and app's client; and
a host it names passes the provider's rules, so no other host is ever called. The one envelope with
no writer is canopy-web's own first seal: `sealed_sig` None on a row never shared (`unshared`, or
`revoking` once it is disconnected). A plaintext is rewritten with only `refresh_token` changed, so
`owner_key` and `statement_floor` stay what was opened.

Exceptions are `services.ConnectionError`s with a fixed sentence, or `server_key.Unavailable`. None
carries a token, and none is chained from an exception that may (`from None`), since a decode error
keeps its input. Nothing here logs a token or a derived secret.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import quote, quote_plus

from django.db import Error as DatabaseFailure
from django.db import connection
from django.utils import timezone

from apps.agents.credentials import vault_engine, zk_seal
from apps.agents.credentials.zk import ZkError
from apps.harness import device_keys

from . import providers, server_key, services, tokens, wire
from . import recipients as rcpt
from .models import Connection
from .wire import WireError

NAME = "canopy-web"
#: A token is cached this long at most, and handed out with this much life left at least.
CACHE_SECONDS, MIN_LIFE_SECONDS = 300, 60
CACHE_SIZE = 512
#: How long a caller waits for another holder's write, and how often it looks.
WAIT_SECONDS, POLL_SECONDS = 30, 1
#: How long a write keeps trying when it holds the only copy of a rotated token, and its pauses:
#: the CLI's budget (`orchestrator/connections/access.py`).
ROTATED_WRITE_SECONDS, RETRY_SECONDS, RETRY_MAX_SECONDS = 90, 2, 15
_KV = 0
_REFRESH_KEYS = frozenset({"v", "provider", "refresh_token", "client_id", "login_host", "instance_url", "account_id",
                           "owner_key", "issued_at", "scopes", "statement_floor"})
_ACCESS_KEYS = frozenset({"v", "access_token", "expires_at", "instance_url", "account_id", "scope", "owner_key",
                          "statement_floor"})
#: What a write-back says when the row moved under it: read it again and try once more.
_STALE = frozenset({"zk_value_changed", "foreign_reader", "zk_rewrap_required", "bad_writer_sig"})
#: A revoke answered with one of these found nothing left to revoke, which is what was wanted.
_ALREADY_DEAD = frozenset({"invalid_token", "invalid_grant"})
_DETAIL_MAX = 300

_sleep = time.sleep
_now = time.time
_monotonic = time.monotonic

logger = logging.getLogger(__name__)


class ServerUseOff(services.ConnectionError):  # noqa: N818 — the name is the interface
    """canopy-web may not use this connection: the switch is off, its key isn't set up, or what is
    stored isn't sealed to it."""

    code = "server_use_off"

    def __init__(self, message: str = "") -> None:
        super().__init__("", message or "canopy-web isn't allowed to use this connection; its owner can turn that "
                                        "on in Settings → Connections.")


class Waiting(services.ConnectionError):  # noqa: N818 — the name is the interface
    """Another holder's lease outlasted the wait."""

    code = "waiting"

    def __init__(self, message: str = "") -> None:
        super().__init__("", message or "Another machine is refreshing this connection; try again in a moment.")


class Refused(services.ConnectionError):  # noqa: N818 — a kind of ConnectionError
    """What is stored failed a check a machine would refuse it for, or could not be written. The
    sentence is fixed; the reason is recorded as `server_refused`, never a value from the envelope."""

    code = "server_refused"

    def __init__(self, message: str = "") -> None:
        super().__init__("", message or "canopy-web could not trust what it holds for this connection; its owner "
                                        "should connect it again.")


# ---- the cache ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
#: {connection id: (access digest, token, instance url, expires at, cached at)}, oldest use first.
_cache: OrderedDict[object, tuple[str, str, str, float, float]] = OrderedDict()


def _forget_all() -> None:
    with _cache_lock:
        _cache.clear()


def _forget(connection_id) -> None:
    with _cache_lock:
        _cache.pop(connection_id, None)


def _remember(connection_id, digest: str, token: str, instance_url: str, expires_at: float) -> None:
    now = _now()
    with _cache_lock:
        for key in [k for k, hit in _cache.items() if hit[3] <= now or now - hit[4] >= CACHE_SECONDS]:
            del _cache[key]
        _cache[connection_id] = (digest, token, instance_url, expires_at, now)
        _cache.move_to_end(connection_id)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def _cached(conn: Connection) -> tuple[str, str] | None:
    digest = vault_engine.value_digest(conn.access_sealed)
    now = _now()
    with _cache_lock:
        hit = _cache.get(conn.pk)
        if hit is None or not digest or hit[0] != digest:
            return None
        if hit[3] - now <= MIN_LIFE_SECONDS or now - hit[4] >= CACHE_SECONDS:
            del _cache[conn.pk]
            return None
        _cache.move_to_end(conn.pk)
        return hit[1], hit[2]


# ---- what is opened is checked -----------------------------------------------------------

def _row(pk) -> Connection:
    """The row as stored now, with no lock. NotConnected when it is gone."""
    row = (Connection.objects.select_related("app__workspace", "agent__workspace", "user").filter(pk=pk).first())
    if row is None:
        raise services.NotConnected("This connection is no longer connected.")
    return row


def _refused(row: Connection, reason: str) -> Refused:
    """`reason` is a fixed phrase of this module's, never a value out of an envelope."""
    services.record(services.Subject.of(row), "server_refused", warn=True, reason=reason)
    return Refused()


def _writer_accepted(row: Connection, by: dict, me: str) -> bool:
    if by["kind"] == "member":
        return by["target"] == row.owner_user_id and device_keys.thumbprint(by["public_jwk"]) == row.owner_key
    # By the statement, never today's recipients: a box that wrote this and has since stopped running
    # the agent is still provable, as it is to a machine, or nobody could open what it wrote.
    return by["target"] == me or rcpt._writer_provable(row, {"by": by})


def _check_writer(row: Connection, value: str, sig, name: str, me: str) -> None:
    if sig is None:
        if name == wire.REFRESH_NAME and row.state in (Connection.UNSHARED, Connection.REVOKING):
            return  # canopy-web's own first seal: a row never shared, or that one being disconnected
        raise _refused(row, f"{name} has no writer")
    try:
        wire.check_writer_sig(value, sig)
    except WireError:
        raise _refused(row, f"{name} has a malformed writer") from None
    if not _writer_accepted(row, sig["by"], me):
        raise _refused(row, f"{name} was written by someone the statement does not allow")
    z = server_key.derive(sig["by"]["public_jwk"])
    if not wire.mac_verifies(value, sig, binding=row.binding, name=name, kind="device", target=me, z=z):
        raise _refused(row, f"{name} has a writer MAC that does not verify")


def _open(row: Connection, value: str, sig, name: str, me: str, keys: frozenset) -> dict:
    _check_writer(row, value, sig, name, me)
    try:
        text = zk_seal.open_for(value, agent_id=row.binding, name=name, kv=_KV, kind="device", target=me,
                                derive=server_key.derive)
        plain = json.loads(text)
    except (ZkError, ValueError):
        raise _refused(row, f"{name} does not open") from None
    if not isinstance(plain, dict) or set(plain) != keys or plain["v"] != 1:
        raise _refused(row, f"{name} does not have the contract's fields")
    if plain["owner_key"] != row.owner_key or not row.owner_key:
        raise _refused(row, f"{name} names another owner key")
    if plain["account_id"] != row.account_id:
        raise _refused(row, f"{name} is for another account")
    floor = plain["statement_floor"]
    if type(floor) is not int or floor < 0 or (row.statement or {}).get("version", 0) < floor:
        raise _refused(row, f"{name} requires a newer statement than the one stored")
    return plain


def _checked_hosts(row: Connection, name: str, **hosts) -> None:
    try:
        providers.for_app(row.app).check_sealed_hosts(**hosts)
    except (providers.ProviderError, providers.UnknownProvider):
        raise _refused(row, f"{name} names a host the provider's rules do not allow") from None


def _open_access(row: Connection, me: str) -> dict:
    plain = _open(row, row.access_sealed, row.access_sig, wire.ACCESS_NAME, me, _ACCESS_KEYS)
    token, expires = plain["access_token"], plain["expires_at"]
    if (not isinstance(token, str) or not token or type(expires) is not int
            or not isinstance(plain["instance_url"], str)):
        raise _refused(row, "access has no usable token, expiry or instance")
    _checked_hosts(row, "access", instance_url=plain["instance_url"])
    return plain


def _open_refresh(row: Connection, me: str) -> dict:
    plain = _open(row, row.sealed, row.sealed_sig, wire.REFRESH_NAME, me, _REFRESH_KEYS)
    if plain["provider"] != row.provider:
        raise _refused(row, "refresh is for another provider")
    if plain["client_id"] != row.app.client_id:
        raise _refused(row, "refresh was issued to another client")
    if not isinstance(plain["refresh_token"], str) or not plain["refresh_token"]:
        raise _refused(row, "refresh has no refresh token")
    if not isinstance(plain["login_host"], str) or not isinstance(plain["instance_url"], str):
        raise _refused(row, "refresh names no host")
    if not isinstance(plain["scopes"], str):
        raise _refused(row, "refresh has no scopes")
    _checked_hosts(row, "refresh", login_host=plain["login_host"], instance_url=plain["instance_url"])
    return plain


# ---- using a token -------------------------------------------------------------------------

def _usable(row: Connection) -> None:
    """Anything that stops canopy-web using `row`, before a key is read."""
    if row.state == Connection.REVOKING:
        raise services.NotConnected("This connection is being disconnected.")
    if row.needs_reconnect:
        title = tokens._title(row)
        raise services.NotConnected(f"This {title} connection no longer works; its owner must connect it again.")
    if not row.server_use or not server_key.available():
        raise ServerUseOff()


def _sealed_to_me(row: Connection, me: str) -> None:
    if ("device", me) not in zk_seal.sealed_targets(row.sealed) or rcpt.server_recipient(row) is None:
        raise ServerUseOff("What is stored for this connection isn't sealed so that canopy-web can open it; its owner "
                           "can connect it again to fix that.")


def _live_access(row: Connection, me: str) -> tuple[str, str] | None:
    """The stored access token when it is worth opening: sealed to canopy-web, a minute left."""
    if (not row.access_sealed or row.access_expires_at is None
            or ("device", me) not in zk_seal.sealed_targets(row.access_sealed)
            or row.access_expires_at.timestamp() - _now() <= MIN_LIFE_SECONDS):
        return None
    plain = _open_access(row, me)
    if plain["expires_at"] - _now() <= MIN_LIFE_SECONDS:
        return None
    answer = plain["access_token"], plain["instance_url"]
    _remember(row.pk, vault_engine.value_digest(row.access_sealed), *answer, plain["expires_at"])
    return answer


def _lease_is_live(row: Connection) -> bool:
    return (row.lease_id is not None and row.lease_expires_at is not None
            and row.lease_expires_at > timezone.now())


def access_token(conn: Connection, *, sleep=None) -> tuple[str, str]:
    """`(access token, instance_url)` for `conn`, with at least a minute left.

    Raises `services.NotConnected` (gone, being disconnected, or a refresh was refused for good),
    `ServerUseOff`, `Waiting` (another holder's lease outlasted 30 s), `server_key.Unavailable`, or
    another `services.ConnectionError`: `Refused` for what fails a machine's checks, `NotConfigured`,
    `provider_error`, `provider_unavailable`. `sleep`: how to wait a second, for a test. Never hold
    a transaction around this: it calls KMS and the provider."""
    sleep = sleep or _sleep
    row = _row(conn.pk)
    _usable(row)
    hit = _cached(row)
    if hit is not None:
        return hit
    me = server_key.thumbprint()
    writer = tokens.Writer(me, NAME, refreshed="server_refreshed")
    for polled in range(WAIT_SECONDS + 1):
        if polled:
            sleep(POLL_SECONDS)
            row = _row(conn.pk)
            _usable(row)
        _sealed_to_me(row, me)
        answer = _live_access(row, me)
        if answer is None and not _lease_is_live(row):
            answer = _refreshed(row, writer, sleep)
        if answer is not None:
            return answer
    raise Waiting()


def _refreshed(row: Connection, writer: tokens.Writer, sleep) -> tuple[str, str] | None:
    """Take the lease and refresh. None when another holder got there first, or took over since:
    wait for its write. A rotated refresh token exists only here until it is stored, so if that fails
    the lease stays held, and its expiry says what may be lost (`lease_expired_unwritten`)."""
    pk = row.pk
    try:
        lease_id = tokens.take_lease(row, writer)["lease_id"]
    except WireError as e:
        if e.code == "lease_held":
            return None
        raise _write_refused(row, e) from None
    except Connection.DoesNotExist:
        raise services.NotConnected("This connection is no longer connected.") from None
    keep_lease = False
    try:
        row = _row(pk)  # only now can no earlier holder land a rotation
        _usable(row)
        answer = _live_access(row, writer.jkt)
        if answer is not None:
            return answer
        plain = _open_refresh(row, writer.jkt)
        p = services._configured(row.app)
        _check_can_seal(row, writer.jkt)
        try:
            token = p.refresh(plain["refresh_token"])
        except providers.ProviderError as e:
            raise _refresh_refused(row, writer, lease_id, plain["refresh_token"], e) from None
        rotated = bool(token.refresh_token) and token.refresh_token != plain["refresh_token"]
        keep_lease = rotated
        try:
            answer = _written_back(row, plain, token, rotated, writer, lease_id, sleep)
        except WireError as e:
            raise _write_refused(row, e) from None
        except ZkError:
            raise _refused(row, "the envelope could not be sealed") from None
        keep_lease = False
        return answer
    finally:
        if not keep_lease:
            _release(row, writer, lease_id)


def _check_can_seal(row: Connection, me: str) -> None:
    """Before the refresh: a rotating provider's refresh can't be taken back, so what is about to be
    needed to store its answer is checked first. An outage then fails here, with nothing lost."""
    rs = _write_recipients(row)
    if not 1 <= len(rs) <= zk_seal.MAX_RECIPIENTS or ("device", me) not in rcpt.targets(rs):
        raise _refused(row, "there is nobody the new token could be sealed to")
    server_key.derive(server_key.public_jwk())


def _release(row: Connection, writer: tokens.Writer, lease_id) -> None:
    try:
        tokens.release_lease(row, writer, lease_id)
    except Connection.DoesNotExist:
        pass


def _write_refused(row: Connection, e: WireError) -> services.ConnectionError:
    if e.code == "revoking":
        return services.NotConnected("This connection is being disconnected.")
    return _refused(row, f"the write-back was refused: {e.code}")


def _scrubbed(text: str, secrets: tuple[str, ...]) -> str:
    """A provider's own words, without what was sent to it, one printable line."""
    for s in secrets:
        if s:
            for spelled in (s, quote(s, safe=""), quote_plus(s)):
                text = text.replace(spelled, "[redacted]")
    return re.sub(r"[^\x20-\x7e]", " ", text)[:_DETAIL_MAX]


def _refresh_refused(row: Connection, writer: tokens.Writer, lease_id, refresh_token: str,
                     e: providers.ProviderError) -> services.ConnectionError:
    """What a refused refresh becomes, recorded as a machine's report of it is
    (`tokens.mark_reconnect_required`). Only `invalid_grant` marks the connection."""
    secrets = (refresh_token, row.app.client_secret)
    code = _scrubbed(services._error_code(e), secrets)
    try:
        tokens.mark_reconnect_required(row, writer, lease_id, code, _scrubbed(str(e), secrets))
    except (WireError, Connection.DoesNotExist):  # the lease went elsewhere meanwhile, or the row did
        pass
    _forget(row.pk)
    title = tokens._title(row)
    if isinstance(e, providers.ProviderUnavailable):
        return services.ConnectionError("provider_unavailable", f"{title} could not be reached; try again.")
    if code == tokens.INVALID_GRANT:
        return services.NotConnected(f"{title} refused this connection's refresh token; its owner must connect it "
                                     "again.")
    return services.ConnectionError("provider_error", f"{title} refused the refresh.")


def _write_recipients(row: Connection) -> list[tuple[str, str, dict]]:
    """Who a write reaches: everyone `recipients` names. While `unshared` that is only who already
    holds it, since the statement a reconnect kept decides nothing and its machines must not
    get the new token until the owner shares again."""
    everyone = rcpt.recipients(row)
    if row.state != Connection.UNSHARED:
        return everyone
    held = zk_seal.sealed_targets(row.sealed)
    return [r for r in everyone if (r[0], r[1]) in held]


def _once_each(derive):
    """`derive`, asked once per key across the envelopes of one write: one KMS call per recipient.
    What it holds goes with the write."""
    seen: dict[tuple[str, str], bytes] = {}

    def once(jwk: dict) -> bytes:
        k = (jwk["x"], jwk["y"])
        if k not in seen:
            seen[k] = derive(jwk)
        return seen[k]

    return once


def _seal(row: Connection, name: str, plain: dict, rs: list, expect: str, me: str, derive) -> tuple[str, dict]:
    value = zk_seal.seal_to(json.dumps(plain, separators=(",", ":")), agent_id=row.binding, name=name, kv=_KV,
                            recipients=rs)
    sig = wire.writer_sig(value, binding=row.binding, name=name, expect=expect, by_kind="device", by_target=me,
                          public_jwk=server_key.public_jwk(), keys={(k, t): j for k, t, j in rs}, derive=derive)
    return value, sig


def _write_once(row: Connection, plain: dict, token: providers.TokenResponse, rotated: bool, writer: tokens.Writer,
                lease_id) -> tuple[str, int]:
    """Seal and MAC holding nothing, then store under the row lock. Returns what was written."""
    scope = token.raw.get("scope")
    expires_at = int(token.expires_at.timestamp())
    access = {"v": 1, "access_token": token.access_token, "expires_at": expires_at,
              "instance_url": plain["instance_url"], "account_id": plain["account_id"],
              "scope": scope if isinstance(scope, str) and scope else plain["scopes"],
              "owner_key": plain["owner_key"], "statement_floor": plain["statement_floor"]}
    rs = _write_recipients(row)
    derive = _once_each(server_key.derive)
    access_sealed, access_sig = _seal(row, wire.ACCESS_NAME, access, rs, vault_engine.value_digest(row.access_sealed),
                                      writer.jkt, derive)
    sealed = sealed_sig = None
    if rotated:
        sealed, sealed_sig = _seal(row, wire.REFRESH_NAME, {**plain, "refresh_token": token.refresh_token}, rs,
                                   row.sealed_digest, writer.jkt, derive)
    tokens.write_back(row, device_key=writer, lease_id=lease_id, access_sealed=access_sealed, access_sig=access_sig,
                      access_expires_at=datetime.fromtimestamp(expires_at, UTC), expect=row.sealed_digest,
                      rotated=rotated, sealed=sealed, sealed_sig=sealed_sig)
    return access_sealed, expires_at


def _attempts(rotated: bool, sleep):
    """One more try each time round. With no rotation, one retry at once: the stored refresh token
    still works, so nothing is at stake. With one, the CLI's budget, backing off."""
    yield
    if not rotated:
        yield
        return
    deadline = _monotonic() + ROTATED_WRITE_SECONDS
    pause = RETRY_SECONDS
    while (left := deadline - _monotonic()) > 0:
        sleep(min(pause, left))
        pause = min(pause * 2, RETRY_MAX_SECONDS)
        yield


def _written_back(row: Connection, plain: dict, token: providers.TokenResponse, rotated: bool, writer: tokens.Writer,
                  lease_id, sleep) -> tuple[str, str] | None:
    """`(token, instance_url)` once stored, keeping the lease renewed. A row that moved is read again
    and sealed again: only the floor and recipients change. With a rotated token in hand, a KMS or
    database failure or a lease someone else holds is tried again too. None when this write no longer
    applies: without a rotation the lease went elsewhere; or a reconnect replaced the refresh token this
    one came from. Out of tries, `server_write_failed` and `Refused`; the caller keeps a rotated lease."""
    used, last = plain["refresh_token"], ""
    reread = False
    for _ in _attempts(rotated, sleep):
        try:
            if not connection.in_atomic_block:  # a connection the last failure killed stays dead otherwise
                connection.close_if_unusable_or_obsolete()
            if reread:
                row = _row(row.pk)
                plain = _open_refresh(row, writer.jkt)
                if rotated and plain["refresh_token"] == token.refresh_token:  # landed; its answer was lost
                    return _stored(row, token, plain, vault_engine.value_digest(row.access_sealed),
                                   int(token.expires_at.timestamp()))
                if plain["refresh_token"] != used:
                    return None
                reread = False
            lease_id = tokens.take_lease(row, writer, lease_id)["lease_id"]
            value, expires_at = _write_once(row, plain, token, rotated, writer, lease_id)
            return _stored(row, token, plain, vault_engine.value_digest(value), expires_at)
        except WireError as e:
            last = e.code
            if e.code in _STALE:
                reread = True
            elif e.code in ("lease_lost", "lease_held"):
                if not rotated:
                    return None
            else:
                raise
        except (server_key.Unavailable, DatabaseFailure, ZkError) as e:
            if not rotated:
                raise
            last = type(e).__name__.lower()
            # A database error may be a commit whose answer was lost: only the row can say.
            reread = reread or isinstance(e, DatabaseFailure)
        except Connection.DoesNotExist:
            raise services.NotConnected("This connection is no longer connected.") from None
    if rotated:
        services.record(services.Subject.of(row), "server_write_failed", warn=True, machine=NAME, rotated=True,
                        error=last,
                        summary=f"{row.owner_label}'s {tokens._title(row)} refresh token rotated on {NAME} and "
                                "could not be stored; it may have to be connected again")
        raise Refused("canopy-web could not store this connection's new token; if it does not work again soon, its "
                      "owner should connect it again.")
    raise _refused(row, f"the write-back was refused: {last}")


def _stored(row: Connection, token: providers.TokenResponse, plain: dict, digest: str,
            expires_at: int) -> tuple[str, str]:
    _remember(row.pk, digest, token.access_token, plain["instance_url"], expires_at)
    return token.access_token, plain["instance_url"]


# ---- revoking --------------------------------------------------------------------------------

def may_revoke(conn: Connection) -> bool:
    """Could canopy-web revoke `conn` itself: its key is configured and `conn` is sealed to it? While
    the key can't be read, a row with the switch on counts, since a KMS outage must not lose the
    revoke; the sweep tries again. Reads only the cached key: callers hold a row lock."""
    if not server_key.available():
        return False
    jwk = server_key.cached_public_jwk()
    if jwk is None:
        return conn.server_use
    return ("device", device_keys.thumbprint(jwk)) in zk_seal.sealed_targets(conn.sealed)


def revoke_if_able(conn: Connection, *, stop_on_kms: bool = False) -> bool:
    """Revoke `conn`, being disconnected, through canopy-web's own key, and delete it as a holder's
    confirmation does (`services.finish_revoking`). At the provider only when no other connection
    of that account through that app remains. True when the row is gone. False leaves it `revoking`
    for its holders and the next sweep. A failure that can never pass (what it holds fails a check, the
    app has no client, the provider refused the client) with no machine able to revoke deletes it now,
    recorded `revoke_unconfirmed`, or the account couldn't connect again for days. Never raises, since
    a person's disconnect has already happened; `stop_on_kms`: `server_key.Unavailable` does, for
    the sweep, which then spares the rest of its tick."""
    try:
        return _revoke(conn)
    except server_key.Unavailable:
        _revoke_failed(conn, "kms_unavailable")
        if stop_on_kms:
            raise
        return False
    except Exception as e:  # noqa: BLE001 — a failure here must never reach the person disconnecting
        _revoke_failed(conn, type(e).__name__.lower())
        return False


def _revoke_failed(conn: Connection, error: str) -> None:
    error = error if services._ERROR_CODE.fullmatch(error or "") else "unrecognised"
    try:
        services.record(services.Subject.of(conn), "revoke_failed", warn=True, during="server_revoke", machine=NAME,
                        error=error)
    except Exception as e:  # noqa: BLE001
        logger.warning("connections.revoke_failed connection=%s error=%s", conn.pk, type(e).__name__)


def _revoke(conn: Connection) -> bool:
    _forget(conn.pk)
    row = Connection.objects.select_related("app__workspace", "agent__workspace", "user").filter(pk=conn.pk).first()
    if row is None:
        return True
    if row.state != Connection.REVOKING or not server_key.available():
        return False
    me = server_key.thumbprint()
    if ("device", me) not in zk_seal.sealed_targets(row.sealed):
        return False
    if not services.other_live_connections(row).exists():
        try:
            plain = _open_refresh(row, me)
            p = services._configured(row.app)
        except (services.NotConfigured, Refused) as e:
            return _revoke_never_works(row, e.code)
        try:
            p.revoke(plain["refresh_token"])
        except providers.ProviderUnavailable as e:
            _revoke_failed(row, services._error_code(e))
            return False
        except providers.ProviderError as e:
            if e.code not in _ALREADY_DEAD:
                return _revoke_never_works(row, services._error_code(e))
    services.finish_revoking(row, machine=NAME, revoked=True)
    return True


def _revoke_never_works(row: Connection, error: str) -> bool:
    """Trying again won't help. A machine that can revoke still may, so it is left to one; with none,
    delete now, as a disconnect does when nothing holds it. True when the row is gone."""
    _revoke_failed(row, error)
    if rcpt.revokers(row):
        return False
    subject = services.Subject.of(row)
    deleted, _ = Connection.objects.filter(pk=row.pk, state=Connection.REVOKING).delete()
    if deleted:
        services.record(subject, "revoke_unconfirmed", warn=True, reason="refused",
                        summary=f"{subject.owner}'s {subject.provider} ({subject.account_label}) deleted: canopy-web "
                                "could not revoke it and no machine holds it")
    return True
