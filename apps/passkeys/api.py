"""/api/passkeys/ — a person's passkeys, and the step-up they earn.

Every route here touches only the CALLER's own passkeys (gate `self`), and every
ceremony needs a person at a browser: a token cannot register a passkey (which
would let whoever holds it mint their own second factor) and cannot step up.

Bootstrapping: the FIRST passkey needs only a signed-in browser session — there
is nothing to confirm it with yet. Every later change to the set (adding
another, deleting one) needs a fresh step-up with a passkey already held, so a
stolen session cannot swap in its own authenticator once a person has one.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.passkeys import services, stepup
from apps.passkeys.models import Passkey
from apps.passkeys.schemas import (
    PasskeyAssertIn,
    PasskeyOptionsOut,
    PasskeyOut,
    PasskeyRegisterIn,
    StepUpStatusOut,
)

router = Router(auth=session_auth, tags=["passkeys"])


def _out(p: Passkey) -> PasskeyOut:
    return PasskeyOut(
        id=p.pk, credential_id=p.credential_id, label=p.label, created_at=p.created_at, last_used_at=p.last_used_at,
        backed_up=p.backed_up, prf_enabled=p.prf_enabled,
    )


def _bad(exc: Exception) -> ProblemError:
    return ProblemError(400, "Passkey verification failed", type_=TYPE_VALIDATION, detail=str(exc))


def _status(request: HttpRequest) -> StepUpStatusOut:
    until = stepup.step_up_until(request) if stepup.is_browser(request) else None
    return StepUpStatusOut(
        active=until is not None, until=until,
        has_passkey=Passkey.objects.filter(user=request.user).exists(),
        browser=stepup.is_browser(request), window_seconds=stepup.STEP_UP_SECONDS,
    )


@router.get("/", response=list[PasskeyOut], summary="List my passkeys")
def list_passkeys(request: HttpRequest) -> list[PasskeyOut]:
    return [_out(p) for p in Passkey.objects.filter(user=request.user)]


@router.post("/register/begin", response=PasskeyOptionsOut,
             summary="Start registering a passkey for this device")
def begin_passkey_registration(request: HttpRequest, upgrade: bool = False) -> PasskeyOptionsOut:
    """Returns the options for navigator.credentials.create(). Browser session
    only; adding a SECOND passkey needs a fresh step-up with the first.
    `?upgrade=true` replaces a passkey that cannot hold a vault key (no PRF):
    it is left out of `excludeCredentials` so the passkey app will make the new
    one for the same account."""
    stepup.require_browser(request, "registering a passkey")
    if Passkey.objects.filter(user=request.user).exists():
        stepup.require_recent_step_up(request)
    return PasskeyOptionsOut(options=services.begin_registration(request, upgrade=upgrade))


@router.post("/register/finish", response={201: PasskeyOut},
             summary="Finish registering a passkey")
def finish_passkey_registration(request: HttpRequest, payload: PasskeyRegisterIn) -> Status:
    """Verifies the authenticator's response and stores its public key. The
    registration required user verification, so it also opens a step-up window
    — the person just proved themselves with this very passkey."""
    stepup.require_browser(request, "registering a passkey")
    try:
        passkey = services.finish_registration(request, payload.credential, payload.label)
    except services.CeremonyError as exc:
        raise _bad(exc) from exc
    stepup.mark_stepped_up(request)
    return Status(201, _out(passkey))


# `int:` so the bare `/{id}` pattern cannot swallow `/step-up` (a 405 otherwise).
@router.delete("/{int:passkey_id}", response={204: None}, summary="Delete one of my passkeys")
def delete_passkey(request: HttpRequest, passkey_id: int) -> Status:
    stepup.require_recent_step_up(request)
    passkey = Passkey.objects.filter(pk=passkey_id, user=request.user).first()
    if passkey is None:
        raise ProblemError(404, "Passkey not found", type_=TYPE_NOT_FOUND)
    from apps.passkeys.models import UserVaultKeyset

    # Its vault-key seal (canopy-web#1550) opens with nothing once it is gone.
    ks = UserVaultKeyset.objects.filter(user=request.user).first()
    if ks is not None and any(s.get("cred") == passkey.credential_id for s in ks.seals):
        ks.seals = [s for s in ks.seals if s.get("cred") != passkey.credential_id]
        ks.save(update_fields=["seals", "updated_at"])
    passkey.delete()
    return Status(204, None)


@router.get("/step-up", response=StepUpStatusOut,
            summary="Is a passkey step-up in force on this browser session?")
def passkey_step_up_status(request: HttpRequest) -> StepUpStatusOut:
    return _status(request)


@router.post("/step-up/begin", response=PasskeyOptionsOut,
             summary="Start a passkey step-up (options for navigator.credentials.get)")
def begin_passkey_step_up(request: HttpRequest) -> PasskeyOptionsOut:
    stepup.require_browser(request, "a passkey step-up")
    options = services.begin_assertion(request)
    if options is None:
        raise ProblemError(
            409, "No passkey registered", type_=TYPE_CONFLICT,
            detail="Register a passkey for this device (Settings → Passkeys) first.",
            members={"error": "no_passkey", "register_url": stepup.REGISTER_PATH},
        )
    return PasskeyOptionsOut(options=options)


@router.post("/step-up/finish", response=StepUpStatusOut,
             summary="Finish a passkey step-up; opens a five-minute window")
def finish_passkey_step_up(request: HttpRequest, payload: PasskeyAssertIn) -> StepUpStatusOut:
    stepup.require_browser(request, "a passkey step-up")
    try:
        services.finish_assertion(request, payload.credential)
    except services.CeremonyError as exc:
        raise _bad(exc) from exc
    stepup.mark_stepped_up(request)
    return _status(request)
