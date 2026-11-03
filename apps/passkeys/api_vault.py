"""/api/vault/keyset — the caller's own zero-knowledge vault key (canopy-web#1550).

A P-256 key pair generated in the browser; canopy-web keeps the PUBLIC half and
one seal of the private half per PRF-capable passkey (formats:
docs/vault/zk-v1/zk_v1_reference.py §4). Only the caller's own row, ever.
Writing it needs a person at a browser with a fresh passkey step-up: the seals
are made from that passkey's PRF output, which only a browser can produce.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.passkeys import stepup, vault_log
from apps.passkeys.models import Passkey, UserVaultKeyset
from apps.passkeys.schemas import (
    VaultClientReportIn,
    VaultKeysetIn,
    VaultKeysetOut,
    VaultKeysetPasskeyOut,
    VaultSetupOut,
)

router = Router(auth=session_auth, tags=["passkeys"])


def prf_input(user_id) -> str:
    """b64u(SHA-256("canopy-vault/v1/prf|{user_id}")) — the reference's prf_input."""
    digest = hashlib.sha256(f"canopy-vault/v1/prf|{user_id}".encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _out(user) -> VaultKeysetOut:
    from apps.agents.credentials import login_test_key  # noqa: PLC0415 — agents imports passkeys

    ks = UserVaultKeyset.objects.filter(user=user).first()
    sealed = {s.get("cred") for s in (ks.seals if ks else [])}
    return VaultKeysetOut(
        test_key=ks is not None and not login_test_key.is_person(user),
        user_id=str(user.pk), prf_input=prf_input(user.pk), exists=ks is not None,
        public_jwk=ks.public_jwk if ks else None, seals=list(ks.seals) if ks else [],
        updated_at=ks.updated_at if ks else None,
        passkeys=[VaultKeysetPasskeyOut(credential_id=p.credential_id, label=p.label,
                                        prf_enabled=p.prf_enabled, sealed=p.credential_id in sealed)
                  for p in Passkey.objects.filter(user=user)],
    )


@router.get("/setup", response=VaultSetupOut, summary="Must I set up my passkey and vault key?")
@gate("self")
def get_vault_setup(request: HttpRequest) -> VaultSetupOut:
    """`required` is true for a person at a browser who has no vault key yet:
    the SPA then asks for a passkey and a vault key before anything else, so
    being added to a vault later never waits on them."""
    # Everyone sets it up at sign-in, not when a vault first needs them: a
    # member with no vault key is one no keeper can pass a key to.
    from apps.agents.credentials import login_test_key  # noqa: PLC0415 — agents imports passkeys

    has_key = UserVaultKeyset.objects.filter(user=request.user).exists()
    return VaultSetupOut(
        # Never asked of an agent's login: it is not a person.
        required=stepup.is_browser(request) and not has_key and login_test_key.is_person(request.user),
        has_passkey=Passkey.objects.filter(user=request.user).exists(),
        has_vault_key=has_key,
    )


@router.get("/keyset", response=VaultKeysetOut, summary="My zero-knowledge vault key (public half + seals)")
@gate("self")
def get_vault_keyset(request: HttpRequest) -> VaultKeysetOut:
    """`{user_id, public_jwk, seals, prf_input, passkeys, …}`; 404 (problem+json,
    `error: "no_vault_keyset"`) until set up — the 404 body still carries
    `user_id`, `prf_input` and `passkeys` for the setup screen. `prf_input` is
    what to pass as `extensions.prf.eval.first` to open a passkey seal."""
    out = _out(request.user)
    if not out.exists:
        vault_log.log("keyset_missing", request)
        raise ProblemError(404, "No vault key", type_=TYPE_NOT_FOUND,
                           detail="You have not set up a vault key (Settings → Vault key).",
                           members={"error": "no_vault_keyset", **out.model_dump(mode="json")})
    return out


@router.put("/keyset", response=VaultKeysetOut, summary="Set up or extend my vault key (browser + passkey)")
# browser session + passkey step-up only
# the person's zero-knowledge vault key
@gate("self", setting=True, step_up=True)
def put_vault_keyset(request: HttpRequest, payload: VaultKeysetIn) -> VaultKeysetOut:
    """`{public_jwk, seals, replace}`. `seals` replaces the stored list; each
    must name one of the caller's passkeys. A different `public_jwk` from the
    stored one is 409 `zk_keyset_exists` unless `replace` is true, which also
    drops every agent's member wrap to the old key.

    An agent's own login, while an admin has turned its test vault key on, makes
    its FIRST key here with passphrase seals only, and with no passkey step-up (it
    has no passkey, and may hold none); anything else from such a login is refused
    (403 `not_a_person`, 409 `test_key_exists`, 422 `test_key_passphrase_only`)."""
    from apps.agents.credentials import login_test_key, zk  # noqa: PLC0415 — agents imports passkeys

    # The step-up is what makes a person's write theirs. A login that is not a person
    # never steps up: `zk.put_keyset` refuses it outright, unless it is an agent login
    # whose test key an admin turned on with their own step-up — and then holds it to
    # one first key, passphrase-sealed.
    if login_test_key.is_person(request.user):
        stepup.require_recent_step_up(request)
    try:
        zk.put_keyset(request.user, public_jwk=payload.public_jwk, seals=payload.seals,
                      replace=payload.replace)
    except zk.ZkError as exc:
        vault_log.log("keyset_write_refused", request, level=logging.WARNING, code=exc.code, status=exc.status,
                      replace=payload.replace,
                      new_seals=",".join(sorted(str(s.get("alg")) for s in payload.seals)) or "-")
        raise ProblemError(exc.status, "Vault key", type_=(
            TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION), detail=str(exc),
            members={"error": exc.code}) from exc
    vault_log.log("keyset_written", request, replace=payload.replace,
                  new_seals=",".join(sorted(str(s.get("alg")) for s in payload.seals)) or "-")
    return _out(request.user)


@router.post("/client-report", response={204: None}, summary="Report a vault unlock/setup outcome the browser saw")
@gate("self", kind="telemetry")  # a diagnostic line in the server log; changes nothing
def post_vault_client_report(request: HttpRequest, payload: VaultClientReportIn) -> Status:
    """The browser alone sees why an unlock failed (which method, a seal that
    did not open, a passkey that gave no PRF). It reports that here, with no
    secret in it, and the server logs it beside the client and keyset facts."""
    vault_log.log("client_report", request,
                  level=logging.WARNING if payload.outcome != "ok" else logging.INFO,
                  stage=payload.stage, method=payload.method, outcome=payload.outcome,
                  error=payload.error[:200], browser_prf=payload.browser_prf,
                  asked=",".join(payload.asked) or "-", secret_key_remembered=payload.secret_key_remembered,
                  page=payload.page[:100])
    return Status(204, None)
