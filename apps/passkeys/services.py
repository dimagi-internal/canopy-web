"""WebAuthn ceremonies over py_webauthn: register a passkey, assert one.

Every ceremony is two calls. `begin_*` issues a random challenge and keeps it in
the session; `finish_*` pops it (single use, and only for `CHALLENGE_TTL`) and
hands the browser's response to py_webauthn, which checks the signature, the
challenge, the origin and the relying-party id. Nothing here re-implements any
of that.

The relying party is the HOST the browser is on (`canopy.dimagi.com` in
production, `localhost` in dev) unless `PASSKEY_RP_ID` / `PASSKEY_ORIGINS`
pin it. A passkey is bound to its RP id by the authenticator itself, so one
registered on one host simply is not offered on another — the failure is a
"no passkey" prompt, never a bypass.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone

from apps.passkeys.models import Passkey

CHALLENGE_TTL = 5 * 60
_REG_KEY = "passkeys.reg_challenge"
_AUTH_KEY = "passkeys.auth_challenge"


class CeremonyError(ValueError):
    """The browser's response did not verify — a 400 the person can retry."""


# ---- the relying party ------------------------------------------------------

def rp_id(request: HttpRequest) -> str:
    pinned = (getattr(settings, "PASSKEY_RP_ID", "") or "").strip()
    return pinned or request.get_host().split(":")[0]


def expected_origins(request: HttpRequest) -> list[str]:
    pinned = [o.strip().rstrip("/") for o in getattr(settings, "PASSKEY_ORIGINS", []) or [] if o.strip()]
    return pinned or [f"{request.scheme}://{request.get_host()}"]


def rp_name() -> str:
    return getattr(settings, "PASSKEY_RP_NAME", "") or "Canopy"


def _user_handle(user) -> bytes:
    # WebAuthn's user.id: opaque, stable per person, and not their email. A hash
    # of the pk under a fixed label — stable, so a second registration on the
    # same authenticator replaces rather than duplicates the account entry.
    return hashlib.sha256(f"canopy-web passkey user {user.pk}".encode()).digest()


# ---- challenges -------------------------------------------------------------

def _stash(request: HttpRequest, key: str, challenge: bytes) -> None:
    from webauthn.helpers import bytes_to_base64url

    request.session[key] = {"c": bytes_to_base64url(challenge), "at": time.time()}


def _pop(request: HttpRequest, key: str) -> bytes:
    from webauthn.helpers import base64url_to_bytes

    raw = request.session.pop(key, None)
    if not isinstance(raw, dict) or not raw.get("c"):
        raise CeremonyError("no passkey ceremony in progress — start again")
    if time.time() - float(raw.get("at") or 0) > CHALLENGE_TTL:
        raise CeremonyError("the passkey prompt expired — start again")
    return base64url_to_bytes(raw["c"])


def _descriptors(passkeys) -> list:
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import AuthenticatorTransport, PublicKeyCredentialDescriptor

    known = {t.value for t in AuthenticatorTransport}
    return [
        PublicKeyCredentialDescriptor(
            id=base64url_to_bytes(p.credential_id),
            transports=[AuthenticatorTransport(t) for t in (p.transports or []) if t in known] or None,
        )
        for p in passkeys
    ]


# ---- registration -----------------------------------------------------------

def begin_registration(request: HttpRequest) -> dict[str, Any]:
    from webauthn import generate_registration_options
    from webauthn.helpers import options_to_json_dict
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    user = request.user
    options = generate_registration_options(
        rp_id=rp_id(request),
        rp_name=rp_name(),
        user_id=_user_handle(user),
        user_name=user.email or user.get_username(),
        user_display_name=(user.get_full_name() or user.email or user.get_username()),
        # Touch ID / Windows Hello / a phone: the person must VERIFY, not just be present.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_descriptors(Passkey.objects.filter(user=user)),
    )
    _stash(request, _REG_KEY, options.challenge)
    return options_to_json_dict(options)


def finish_registration(request: HttpRequest, credential: dict, label: str = "") -> Passkey:
    from webauthn import verify_registration_response
    from webauthn.helpers import bytes_to_base64url

    challenge = _pop(request, _REG_KEY)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(request),
            expected_origin=expected_origins(request),
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 - every parse/verify failure is the same 400
        raise CeremonyError(f"the passkey could not be verified: {exc}") from exc

    cred_id = bytes_to_base64url(verified.credential_id)
    if Passkey.objects.filter(credential_id=cred_id).exists():
        raise CeremonyError("that passkey is already registered")
    transports = credential.get("response", {}).get("transports") if isinstance(credential, dict) else None
    return Passkey.objects.create(
        user=request.user,
        credential_id=cred_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=[str(t) for t in (transports or [])][:8],
        label=(label or "").strip()[:120],
        aaguid=str(verified.aaguid or ""),
        backed_up=bool(verified.credential_backed_up),
    )


# ---- assertion (the step-up) ------------------------------------------------

def begin_assertion(request: HttpRequest) -> dict[str, Any] | None:
    """Options for navigator.credentials.get(), or None when the caller has no passkey."""
    from webauthn import generate_authentication_options
    from webauthn.helpers import options_to_json_dict
    from webauthn.helpers.structs import UserVerificationRequirement

    passkeys = list(Passkey.objects.filter(user=request.user))
    if not passkeys:
        return None
    options = generate_authentication_options(
        rp_id=rp_id(request),
        allow_credentials=_descriptors(passkeys),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _stash(request, _AUTH_KEY, options.challenge)
    return options_to_json_dict(options)


def finish_assertion(request: HttpRequest, credential: dict) -> Passkey:
    """Verify the assertion against one of the CALLER's own passkeys."""
    from webauthn import verify_authentication_response

    challenge = _pop(request, _AUTH_KEY)
    cred_id = credential.get("id") if isinstance(credential, dict) else None
    # Scoped to request.user: a valid assertion from SOMEONE ELSE's passkey is
    # not a step-up for this session, however good its signature.
    passkey = Passkey.objects.filter(user=request.user, credential_id=cred_id or "").first()
    if passkey is None:
        raise CeremonyError("that passkey is not registered to you")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(request),
            expected_origin=expected_origins(request),
            credential_public_key=bytes(passkey.public_key),
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 - every parse/verify failure is the same 400
        raise CeremonyError(f"the passkey could not be verified: {exc}") from exc

    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = timezone.now()
    passkey.save(update_fields=["sign_count", "last_used_at"])
    return passkey
