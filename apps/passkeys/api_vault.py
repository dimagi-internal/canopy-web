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

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.passkeys import stepup
from apps.passkeys.models import Passkey, UserVaultKeyset
from apps.passkeys.schemas import VaultKeysetIn, VaultKeysetOut, VaultKeysetPasskeyOut, VaultSetupOut

router = Router(auth=session_auth, tags=["passkeys"])


def prf_input(user_id) -> str:
    """b64u(SHA-256("canopy-vault/v1/prf|{user_id}")) — the reference's prf_input."""
    digest = hashlib.sha256(f"canopy-vault/v1/prf|{user_id}".encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _out(user) -> VaultKeysetOut:
    ks = UserVaultKeyset.objects.filter(user=user).first()
    sealed = {s.get("cred") for s in (ks.seals if ks else [])}
    return VaultKeysetOut(
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
    has_key = UserVaultKeyset.objects.filter(user=request.user).exists()
    return VaultSetupOut(
        required=stepup.is_browser(request) and not has_key,
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
    drops every agent's member wrap to the old key."""
    from apps.agents.credentials import zk

    stepup.require_recent_step_up(request)
    try:
        zk.put_keyset(request.user, public_jwk=payload.public_jwk, seals=payload.seals,
                      replace=payload.replace)
    except zk.ZkError as exc:
        raise ProblemError(exc.status, "Vault key", type_=(
            TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION), detail=str(exc),
            members={"error": exc.code}) from exc
    return _out(request.user)
