"""A connection's wire format, as canopy-web checks it: grant statements and
writer-authenticated envelopes.

The normative definition is `docs/vault/zk-v1/connections_v1_reference.py` (with
vectors beside it); the canopy CLI and the owner's browser make these bytes and
are what the design relies on. canopy-web never opens an envelope and never
signs: it refuses junk early (a statement of the wrong shape, a signature that
does not verify, a writer MAC list that does not cover the envelope), so a
machine is not the first to find it.
"""
from __future__ import annotations

import json
import re
import uuid

from apps.agents.credentials import zk, zk_endorse, zk_seal
from apps.agents.credentials.zk import ZkError
from apps.harness import device_keys

REFRESH_NAME = "refresh"
ACCESS_NAME = "access"
STATEMENT_PREFIX = "canopy-connections/v1/grant|"

_STATEMENT_KEYS = {"account_id", "agents", "connection", "endorsers", "my_computers", "v", "version"}
#: Printable ASCII without '"' or '\', so the signed message has one spelling everywhere.
_ACCOUNT_ID = re.compile(r'^[\x21\x23-\x5b\x5d-\x7e]{1,255}$')
_AGENT_PK = re.compile(r"^[1-9][0-9]*$")
#: An RFC 7638 thumbprint: b64u of a SHA-256 digest.
_JKT = re.compile(r"^[A-Za-z0-9_-]{43}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAC_LEN = 32
#: The statement's lists are bounded like the recipients of one sealed value.
_MAX_LISTED = zk_seal.MAX_RECIPIENTS
_MAX_ENDORSERS = 64


class WireError(Exception):
    """A refused statement, writer sig or machine request; `code` is the problem's
    `error` member and `members` add to its body (`retry_after`, `workspaces`)."""

    def __init__(self, status: int, code: str, detail: str, **members):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.members = members


def binding_id(connection_id: uuid.UUID) -> str:
    """`connection:<uuid>`: the agent-id slot of every zk-v1 derivation for this connection."""
    return f"connection:{uuid.UUID(str(connection_id))}"


def statement_msg(st: dict) -> bytes:
    """The bytes the owner signs (contract §0.3)."""
    return (STATEMENT_PREFIX + json.dumps(st, sort_keys=True, separators=(",", ":"), ensure_ascii=True)).encode()


def _bad_statement(detail: str) -> WireError:
    return WireError(422, "bad_statement", f"grant statement: {detail}")


def _sorted_unique(items, pattern: re.Pattern, what: str, limit: int = _MAX_LISTED) -> None:
    if not isinstance(items, list) or len(items) > limit:
        raise _bad_statement(f"{what} must be a list of at most {limit}")
    if not all(isinstance(i, str) and pattern.fullmatch(i) for i in items):
        raise _bad_statement(f"{what} has an entry of the wrong form")
    if items != sorted(set(items)):
        raise _bad_statement(f"{what} must be sorted with no repeats")


def check_statement(st: dict, *, connection_id, account_id: str) -> dict:
    """`st` if it is a well-formed statement about THIS connection and account; 422
    `bad_statement` otherwise. Its signature, version and agents are the caller's
    to judge."""
    if not isinstance(st, dict) or set(st) != _STATEMENT_KEYS:
        raise _bad_statement(f"must be an object with exactly {sorted(_STATEMENT_KEYS)}")
    if type(st["v"]) is not int or st["v"] != 1:
        raise _bad_statement("v must be 1")
    if type(st["version"]) is not int or st["version"] < 1:
        raise _bad_statement("version must be a whole number of at least 1")
    if not isinstance(st["account_id"], str) or not _ACCOUNT_ID.fullmatch(st["account_id"]):
        raise _bad_statement("account_id must be 1-255 printable ASCII characters without quotes or backslashes")
    if st["account_id"] != account_id:
        raise _bad_statement("it names another account")
    if st["connection"] != str(uuid.UUID(str(connection_id))):
        raise _bad_statement("it names another connection")
    _sorted_unique(st["agents"], _AGENT_PK, "agents")
    _sorted_unique(st["my_computers"], _JKT, "my_computers")
    _sorted_unique(st["endorsers"], _JKT, "endorsers", limit=_MAX_ENDORSERS)
    return st


def verify_statement(st: dict, sig: str, signer_jwk: dict) -> bool:
    """Is `sig` the signer's ECDSA P-256 signature (P1363, b64u) over `st`? Never raises."""
    try:
        msg = statement_msg(st)
    except (TypeError, ValueError):
        return False
    return zk_endorse.verify_p1363(msg, sig, signer_jwk)


def _bad_sig(detail: str) -> WireError:
    return WireError(422, "bad_writer_sig", f"writer sig: {detail}")


def _check_public_jwk(jwk) -> None:
    if not isinstance(jwk, dict) or set(jwk) != {"kty", "crv", "x", "y"} or (jwk["kty"], jwk["crv"]) != ("EC", "P-256"):
        raise _bad_sig("by.public_jwk must be exactly a P-256 public JWK {kty, crv, x, y}")
    try:
        zk.public_key(jwk)
    except (ValueError, ZkError) as exc:
        raise _bad_sig("by.public_jwk is not a point on P-256") from exc


def check_writer_sig(value: str, sig: dict) -> dict:
    """`sig` if its structure fits the envelope `value` (contract §0.4): one MAC per
    sealed recipient, no more, and a writer named by its key. canopy-web cannot
    check a MAC (it holds none of the keys); the recipient does. 422 `bad_writer_sig`."""
    if not isinstance(sig, dict) or set(sig) != {"by", "expect", "macs"}:
        raise _bad_sig("must be an object with exactly ['by', 'expect', 'macs']")
    by = sig["by"]
    if not isinstance(by, dict) or set(by) != {"kind", "target", "public_jwk"}:
        raise _bad_sig("by must be an object with exactly ['kind', 'public_jwk', 'target']")
    _check_public_jwk(by["public_jwk"])
    if by["kind"] == "device":
        if by["target"] != device_keys.thumbprint(by["public_jwk"]):
            raise _bad_sig("a device writer's target must be the thumbprint of its key")
    elif by["kind"] == "member":
        if not isinstance(by["target"], str) or not _AGENT_PK.fullmatch(by["target"]):
            raise _bad_sig("a member writer's target must be a user id")
    else:
        raise _bad_sig("by.kind must be device or member")
    if not isinstance(sig["expect"], str) or not (sig["expect"] == "" or _DIGEST.fullmatch(sig["expect"])):
        raise _bad_sig("expect must be a sealed digest (64 lower-case hex) or empty")
    macs = sig["macs"]
    if not isinstance(macs, list):
        raise _bad_sig("macs must be a list")
    covered = []
    for i, m in enumerate(macs):
        if not isinstance(m, dict) or set(m) != {"kind", "target", "mac"}:
            raise _bad_sig(f"macs[{i}] must be an object with exactly ['kind', 'mac', 'target']")
        if not isinstance(m["kind"], str) or not isinstance(m["target"], str):
            raise _bad_sig(f"macs[{i}] must name its recipient by strings")
        try:
            if len(zk.b64u_bytes(m["mac"], f"macs[{i}].mac")) != _MAC_LEN:
                raise _bad_sig(f"macs[{i}].mac must decode to {_MAC_LEN} bytes")
        except ZkError as exc:
            raise _bad_sig(f"macs[{i}].mac must be base64url without padding") from exc
        covered.append((m["kind"], m["target"]))
    targets = zk_seal.sealed_targets(value)
    if not targets:
        raise _bad_sig("the envelope is not a sealed value")
    if len(covered) != len(set(covered)) or set(covered) != targets:
        raise _bad_sig("macs must cover exactly the envelope's recipients, once each")
    return sig


def keep_only_sig(sig: dict | None, allowed: set[tuple[str, str]]) -> dict | None:
    """`sig` with only the MACs of recipients in `allowed`: the twin of
    `zk_seal.keep_only`, since each MAC covers only its own entry. None stays None
    (canopy-web's first seal carries no sig)."""
    if sig is None:
        return None
    return {**sig, "macs": [m for m in sig["macs"] if (m["kind"], m["target"]) in allowed]}
