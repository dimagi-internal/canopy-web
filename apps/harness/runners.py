"""Runner administration — everything about a box that is not routing or work.

Its declared flags (`RunnerFlag`, e.g. ZDR), its Claude login
(`RunnerCredential`: set, read, swap primary and secondary), refresh requests,
its admins (`RunnerAdmin`), and the browser-driven re-authentication
(`RunnerMint`). Split out of `services.py`, which still re-exports every name
here.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import (
    Runner,
    RunnerFlag,
)


def set_runner_flags(runner: Runner, flags: set[str], *, by) -> tuple[set, set]:
    """Make the runner's declared flags exactly `flags`. Stamps only ADDED ones,
    so re-saving does not rewrite who first vouched. Returns (added, removed)."""
    current = set(runner.flags)
    added, removed = flags - current, current - flags
    with transaction.atomic():
        RunnerFlag.objects.filter(runner=runner, flag__in=removed).delete()
        for f in sorted(added):
            RunnerFlag.objects.create(runner=runner, flag=f, declared_by=by)
    return added, removed


def set_runner_credential(runner, *, claude_token=None, claude_token_secondary=None,
                          claude_api_key=None, claude_token_label=None,
                          claude_token_secondary_label=None, updated_by=None):
    """Upsert a runner's credential bundle. None fields are left unchanged."""
    from apps.common.encryption import encrypt_secret

    from .models import RunnerCredential

    # An all-None call is a READ dressed as a write (the shape the CLI used before
    # there was a status endpoint). Don't create a row and don't touch
    # updated_at/updated_by for it — that timestamp is the audit trail for when a
    # credential last actually changed.
    if all(v is None for v in (claude_token, claude_token_secondary, claude_api_key,
                               claude_token_label, claude_token_secondary_label)):
        return getattr(runner, "credential", None)
    cred, _ = RunnerCredential.objects.get_or_create(runner=runner)
    if claude_token is not None:
        cred.claude_token_enc = encrypt_secret(claude_token)
    if claude_token_secondary is not None:
        cred.claude_token_secondary_enc = encrypt_secret(claude_token_secondary)
    if claude_api_key is not None:
        cred.claude_api_key_enc = encrypt_secret(claude_api_key)
    if claude_token_label is not None:
        cred.claude_token_label = claude_token_label.strip()[:200]
    if claude_token_secondary_label is not None:
        cred.claude_token_secondary_label = claude_token_secondary_label.strip()[:200]
    if updated_by is not None:
        cred.updated_by = updated_by
    cred.save()
    return cred


def get_runner_credential(runner) -> dict:
    """Decrypt a runner's bundle for the runner to consume. Empty when unset."""
    from apps.common.encryption import decrypt_secret

    cred = getattr(runner, "credential", None)
    if cred is None:
        return {"claude_token": "", "claude_token_secondary": "", "claude_api_key": "",
                "updated_at": None}
    return {
        "claude_token": decrypt_secret(cred.claude_token_enc),
        "claude_token_secondary": decrypt_secret(cred.claude_token_secondary_enc),
        "claude_api_key": decrypt_secret(cred.claude_api_key_enc),
        "updated_at": cred.updated_at,
    }


def runner_credential_status(runner) -> dict:
    """Masked view — which tokens are set, never their values. Labels are names,
    not secrets, so they ride along."""
    cred = getattr(runner, "credential", None)
    if cred is None:
        return {"has_claude_token": False, "has_claude_token_secondary": False,
                "has_claude_api_key": False, "claude_token_label": "",
                "claude_token_secondary_label": "", "updated_at": None}
    return {
        "has_claude_token": bool(cred.claude_token_enc),
        "has_claude_token_secondary": bool(cred.claude_token_secondary_enc),
        "has_claude_api_key": bool(cred.claude_api_key_enc),
        "claude_token_label": cred.claude_token_label,
        "claude_token_secondary_label": cred.claude_token_secondary_label,
        "updated_at": cred.updated_at,
    }


def swap_runner_logins(runner, *, updated_by=None):
    """Make the fallback the primary and vice versa — token AND name together.

    The runner falls through the cascade in slot order, so which login is
    "primary" is a real choice (whose weekly cap gets spent first), and it used to
    be changeable only by pasting both tokens back in the other way round. Moving
    ciphertext is enough: both columns are encrypted with the same key, so no
    secret is decrypted to do this.
    """
    from .models import RunnerCredential

    with transaction.atomic():
        cred = RunnerCredential.objects.select_for_update().filter(runner=runner).first()
        if cred is None:
            return None
        cred.claude_token_enc, cred.claude_token_secondary_enc = (
            cred.claude_token_secondary_enc, cred.claude_token_enc)
        cred.claude_token_label, cred.claude_token_secondary_label = (
            cred.claude_token_secondary_label, cred.claude_token_label)
        if updated_by is not None:
            cred.updated_by = updated_by
        cred.save()
    return cred
# ---- Runner administrators (administer a box without speaking for it) -----
def request_refresh(runner: Runner) -> Runner:
    """Ask a box to refresh itself — re-run its bootstrap (plugins, the canopy
    CLI, Claude Code, each agent's provisioning) at its next idle moment.

    Only recorded here; the box acts on it. It reads `refresh_pending` off its
    heartbeat reply, so a dropped socket costs one beat rather than the request,
    and the request is discharged by the box reporting a newer bootstrap — never
    by this side guessing that it happened."""
    runner.refresh_requested_at = timezone.now()
    runner.save(update_fields=["refresh_requested_at"])
    return runner


def can_administer_runner(user, runner) -> bool:
    """May this person change what this box RUNS ON — its credentials, its
    sign-in — as opposed to speaking as it?

    The owner always can: they own the credential the box authenticates with,
    so withholding administration from them would be theatre. Anyone else needs
    an explicit grant, because the alternatives are a workspace with one owner
    (too narrow — that IS the single point of failure) or every auto-joined
    member (far too wide for credentials).
    """
    from .models import RunnerAdmin

    if not getattr(user, "is_authenticated", False):
        return False
    # A runner with NO owner is administered by nobody — it used to be
    # administered by everyone, a NULL-means-allow leg like the six this repo
    # has already removed elsewhere.
    if runner.owner_id is None:
        return False
    if runner.owner_id == user.id:
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    return RunnerAdmin.objects.filter(runner=runner, user=user).exists()


def grant_runner_admin(runner, user, *, granted_by=None):
    """Idempotent: re-granting returns the existing row rather than a second one
    that a later revoke would only half-remove."""
    from .models import RunnerAdmin

    admin, _ = RunnerAdmin.objects.get_or_create(
        runner=runner, user=user, defaults={"granted_by": granted_by})
    return admin


def revoke_runner_admin(runner, user) -> bool:
    """True when a grant was actually removed. The owner is not stored as a
    grant, so this can never revoke them — losing the last administrator of a box
    is not a state this should be able to produce."""
    from .models import RunnerAdmin

    deleted, _ = RunnerAdmin.objects.filter(runner=runner, user=user).delete()
    return bool(deleted)


def list_runner_admins(runner):
    from .models import RunnerAdmin

    return list(
        RunnerAdmin.objects.filter(runner=runner)
        .select_related("user", "granted_by")
        .order_by("created_at")
    )


# ---- Browser-driven re-authentication (RunnerMint) ------------------------
#: How long a sign-in may sit unfinished before it is treated as dead.
#:
#: An authorize URL is not durable. Its PKCE `state` and challenge expire, and
#: the `claude setup-token` process waiting on the box expires with them — so a
#: link that LOOKS fine can be unusable. Measured 2026-09-08: a mint started at
#: 17:56 and completed at 19:04 failed with "setup-token never printed a token",
#: and the only clue that anything was wrong was the clock. Ten minutes is
#: comfortably longer than the flow takes and comfortably shorter than the point
#: at which it silently stops working.
MINT_TTL_SECONDS = 600


def _expire_if_stale(mint):
    """Fail a mint that has aged out, so a dead link stops being offered as live.

    Returns the mint either way — callers want the row, not a decision about
    whether to look at it.
    """
    from django.utils import timezone

    from .models import RunnerMint

    if mint is None or mint.status in RunnerMint.FINISHED:
        return mint
    if (timezone.now() - mint.updated_at).total_seconds() <= MINT_TTL_SECONDS:
        return mint
    mint.status = RunnerMint.FAILED
    mint.detail = ("This sign-in expired before it was finished — the link is only "
                   "good for a few minutes. Start another one.")
    mint.code = ""
    mint.save(update_fields=["status", "detail", "code", "updated_at"])
    return mint


def start_runner_mint(runner, *, requested_by=None, slot="primary"):
    """Ask a runner to begin a browser sign-in, superseding any unfinished one.

    Superseding rather than refusing: a mint that stalled (the operator closed
    the tab, the runner restarted mid-flow) would otherwise block every later
    attempt, and "try again" is the only sensible response to a stuck sign-in.
    The old row is marked failed rather than deleted so the history stays honest.
    """
    from .models import RunnerMint

    RunnerMint.objects.filter(runner=runner).exclude(
        status__in=RunnerMint.FINISHED
    ).update(status=RunnerMint.FAILED, detail="superseded by a newer request")
    return RunnerMint.objects.create(runner=runner, requested_by=requested_by, slot=slot)


def current_runner_mint(runner):
    """The mint worth showing or acting on, or None.

    Only ever the newest: an older unfinished row cannot exist (start supersedes
    them), and a finished one is history.
    """
    from .models import RunnerMint

    return _expire_if_stale(
        RunnerMint.objects.filter(runner=runner).order_by("-created_at").first())


def claim_runner_mint(runner):
    """The mint this runner should act on right now, or None.

    Returns a row only in the two states where the RUNNER owes work — it has been
    asked to start, or a human has handed back a code. In `awaiting_code` the ball
    is with the human, and handing that back on every poll tick would have the
    runner restart the CLI under a URL somebody is already signing in to.
    """
    from .models import RunnerMint

    mint = current_runner_mint(runner)
    if mint is None or mint.status not in (RunnerMint.REQUESTED, RunnerMint.COMPLETING):
        return None
    return mint


def record_mint_url(mint, url: str):
    """The runner has the CLI running and a URL for the human."""
    from .models import RunnerMint

    mint.authorize_url = url
    mint.status = RunnerMint.AWAITING_CODE
    mint.save(update_fields=["authorize_url", "status", "updated_at"])
    return mint


def submit_mint_code(mint, code: str):
    """A human pasted the authorization code back."""
    from .models import RunnerMint

    mint.code = code.strip()
    mint.status = RunnerMint.COMPLETING
    mint.save(update_fields=["code", "status", "updated_at"])
    return mint


def take_mint_code(mint) -> str:
    """Hand the code to the runner exactly once, then forget it.

    An authorization code is spent on first use, so a second delivery can only
    fail — and a used code sitting in a row is a credential nobody is accounting
    for. Read-and-clear is the whole point of this being a function.
    """
    code = mint.code
    if code:
        mint.code = ""
        mint.save(update_fields=["code", "updated_at"])
    return code


def finish_runner_mint(mint, *, token: str = "", detail: str = ""):
    """The runner reports the outcome; on success the token lands in the bundle,
    in the slot the human chose.

    The token arrives HERE rather than in the browser on purpose: the only secret
    a human ever handles in this flow is the single-use authorization code, and
    the long-lived credential goes straight from the box into encrypted storage.

    The slot keeps its name. The token cannot say whose it is (a setup-token is
    scoped to inference only; the profile endpoint answers 403), and re-signing
    an expired login is the common case, which is the same account.
    """
    from .models import RunnerMint

    if token:
        key = ("claude_token_secondary" if mint.slot == RunnerMint.SECONDARY
               else "claude_token")
        set_runner_credential(mint.runner, **{key: token}, updated_by=mint.requested_by)
        mint.status = RunnerMint.DONE
        mint.detail = detail or "signed in"
    else:
        mint.status = RunnerMint.FAILED
        mint.detail = detail or "the runner did not complete the sign-in"
    mint.code = ""
    mint.save(update_fields=["status", "detail", "code", "updated_at"])
    return mint
