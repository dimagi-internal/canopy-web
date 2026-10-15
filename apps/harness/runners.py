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

    logins = [{"key": login.key, "account": login.account}
              for login in runner.logins.all()]
    cred = getattr(runner, "credential", None)
    if cred is None:
        return {"claude_token": "", "claude_token_secondary": "", "claude_api_key": "",
                "logins": logins, "updated_at": None}
    return {
        "claude_token": decrypt_secret(cred.claude_token_enc),
        "claude_token_secondary": decrypt_secret(cred.claude_token_secondary_enc),
        "claude_api_key": decrypt_secret(cred.claude_api_key_enc),
        "logins": logins,
        "updated_at": cred.updated_at,
    }


def runner_credential_status(runner) -> dict:
    """Masked view — which tokens are set, never their values. Labels are names,
    not secrets, so they ride along."""
    logins = [{"key": login.key, "account": login.account, "signed_in": login.signed_in,
               "reported_at": login.reported_at} for login in runner.logins.all()]
    cred = getattr(runner, "credential", None)
    if cred is None:
        return {"has_claude_token": False, "has_claude_token_secondary": False,
                "has_claude_api_key": False, "claude_token_label": "",
                "claude_token_secondary_label": "", "logins": logins, "updated_at": None}
    return {
        "logins": logins,
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
# ---- Full Claude logins on the box (RunnerLogin) ---------------------------
class LoginOrderRefused(ValueError):
    """An order that is not exactly the runner's current logins, rearranged."""


def _renumber(runner) -> None:
    for i, login in enumerate(runner.logins.all()):
        if login.position != i:
            login.position = i
            login.save(update_fields=["position"])


def add_runner_login(runner, *, by=None):
    """Add a login at the END of the cascade and start signing it in.

    The row exists from the start rather than on success: a sign-in that is
    abandoned leaves a row saying "not signed in", with Sign in and Remove next
    to it, instead of a login that silently never appeared.
    """
    import secrets

    from .models import RunnerLogin

    with transaction.atomic():
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        last = runner.logins.order_by("-position").first()
        login = RunnerLogin.objects.create(
            runner=runner, key=secrets.token_hex(4), created_by=by,
            position=(last.position + 1) if last else 0)
    start_runner_mint(runner, requested_by=by, slot="box", login_key=login.key)
    return login


def remove_runner_login(runner, key: str) -> bool:
    """Drop a login from the list. The box signs it out (deletes its directory)
    the next time it re-reads its bundle — within a few minutes."""
    with transaction.atomic():
        deleted, _ = runner.logins.filter(key=key).delete()
        _renumber(runner)
    return bool(deleted)


def order_runner_logins(runner, keys: list[str]) -> None:
    """Make the cascade order exactly `keys` — every current login, once each.

    Refused rather than partially applied: an order that names a login twice or
    leaves one out is a stale page, and guessing where the rest go would put a
    login somewhere nobody chose.
    """
    with transaction.atomic():
        logins = {login.key: login for login in runner.logins.select_for_update()}
        if sorted(keys) != sorted(logins):
            raise LoginOrderRefused(
                "the order must name every login on this runner exactly once — "
                "reload the page and try again")
        for i, key in enumerate(keys):
            if logins[key].position != i:
                logins[key].position = i
                logins[key].save(update_fields=["position"])


def report_runner_logins(runner, reports: list[dict], *, adopt_default: bool = False) -> None:
    """The box says which of its logins are signed in, and as whom.

    `adopt_default`: the box found a login in the CLI's own directory that this
    list has never held — one signed in before there was a list, or by hand on
    the box. It goes FIRST, where that login always was.
    """
    from .models import RunnerLogin

    now = timezone.now()
    with transaction.atomic():
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        if adopt_default and not runner.logins.filter(key=RunnerLogin.DEFAULT).exists():
            for login in runner.logins.all():
                login.position += 1
                login.save(update_fields=["position"])
            RunnerLogin.objects.create(runner=runner, key=RunnerLogin.DEFAULT, position=0)
        for r in reports:
            runner.logins.filter(key=r.get("key", "")).update(
                signed_in=bool(r.get("signed_in")),
                account=(r.get("account") or "")[:254], reported_at=now)


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


# ---- Workspace enrollment (which tenants a box serves) ---------------------
class EnrollmentRefused(ValueError):
    """The owner may not run work in that workspace, so the box may not serve it."""


def enroll_runner(runner, workspace_slug: str, *, by=None):
    """Opt `runner` in to serving `workspace_slug`. Idempotent.

    Refused unless the box's OWNER holds `agent.work` (editor and above) there:
    a box runs with its owner's memberships, so enrollment can narrow what the
    owner's box serves but never widen it past what the owner may do. Checked
    here as well as at claim time so a useless row is a clear refusal now, not
    a silent no-op later. Who may ASK is the route's business (owner or a
    runner admin); this is the property of the box itself.
    """
    from apps.workspaces import permissions as perms

    from .models import RunnerEnrollment

    if not runner.owner_id or not perms.can(runner.owner, workspace_slug, perms.AGENT_WORK):
        raise EnrollmentRefused(
            f"this runner's owner is not an editor (or above) in '{workspace_slug}', "
            "so the runner cannot serve it")
    row, _ = RunnerEnrollment.objects.get_or_create(
        runner=runner, workspace_id=workspace_slug, defaults={"enrolled_by": by})
    return row


def unenroll_runner(runner, workspace_slug: str) -> bool:
    """True when an enrollment was actually removed. The box stops serving that
    workspace at its next claim; a turn it already holds runs to completion."""
    from .models import RunnerEnrollment

    deleted, _ = RunnerEnrollment.objects.filter(
        runner=runner, workspace_id=workspace_slug).delete()
    return bool(deleted)


def list_runner_enrollments(runner):
    from .models import RunnerEnrollment

    return list(
        RunnerEnrollment.objects.filter(runner=runner)
        .select_related("workspace", "enrolled_by")
        .order_by("workspace_id")
    )


def backfill_runner_enrollments(runner_model, enrollment_model, membership_model,
                                workspace_model, *, exclude=("marshellis",)) -> int:
    """Enroll every existing runner in every workspace its owner may run work in
    (editor and above, counting ownership inherited down the tree — the same
    answer `permissions.slugs_with(owner, AGENT_WORK)` gave before enrollment
    existed), except the `exclude` workspaces and everything below them.

    So introducing enrollment changes nothing for the fleet that existed, with
    one deliberate exception: `marshellis`, a separate root created on
    2026-10-11 that no existing box should serve. A missing excluded slug is
    simply nothing to exclude. Idempotent: existing rows are kept.

    Takes the model classes so the migration passes its HISTORICAL models and a
    test passes the live ones (the `seed_assignments_from_capabilities` pattern).
    """
    from apps.workspaces.models import MAX_DEPTH

    owner_role, editor_roles = "owner", {"owner", "admin", "editor"}
    parents = dict(workspace_model.objects.values_list("slug", "parent_id"))
    children: dict[str, set[str]] = {}
    for slug, parent in parents.items():
        if parent:
            children.setdefault(parent, set()).add(slug)

    def below(roots: set[str]) -> set[str]:
        out: set[str] = set()
        frontier = set(roots)
        for _ in range(MAX_DEPTH):
            frontier = {c for f in frontier for c in children.get(f, ())} - out - set(roots)
            if not frontier:
                break
            out |= frontier
        return out

    excluded = {s for s in exclude if s in parents}
    excluded |= below(excluded)

    roles: dict[int, dict[str, str]] = {}
    for user_id, slug, role in membership_model.objects.values_list(
            "user_id", "workspace_id", "role"):
        roles.setdefault(user_id, {})[slug] = role
    existing = set(enrollment_model.objects.values_list("runner_id", "workspace_id"))
    rows = []
    for runner_id, owner_id in runner_model.objects.filter(
            owner__isnull=False).values_list("pk", "owner_id"):
        mine = dict(roles.get(owner_id, {}))
        for slug in below({s for s, r in mine.items() if r == owner_role}):
            mine[slug] = owner_role
        for slug, role in sorted(mine.items()):
            if (role in editor_roles and slug in parents and slug not in excluded
                    and (runner_id, slug) not in existing):
                rows.append(enrollment_model(runner_id=runner_id, workspace_id=slug))
    enrollment_model.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


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


def start_runner_mint(runner, *, requested_by=None, slot="box", login_key=""):
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
    return RunnerMint.objects.create(runner=runner, requested_by=requested_by, slot=slot,
                                     login_key=login_key)


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


def finish_runner_mint(mint, *, token: str = "", detail: str = "", logged_in: bool = False,
                       account: str = ""):
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

    if mint.slot == RunnerMint.BOX:
        # A full login the runner keeps on the box: nothing to store here, and a
        # token arriving anyway is ignored rather than written into a bundle
        # slot nobody asked to replace.
        mint.status = RunnerMint.DONE if logged_in else RunnerMint.FAILED
        if logged_in:
            _record_signed_in(mint.runner, mint.login_key, account)
        mint.detail = detail or ("signed in on the box" if logged_in
                                 else "the runner did not complete the sign-in")
        mint.code = ""
        mint.save(update_fields=["status", "detail", "code", "updated_at"])
        return mint
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


def _record_signed_in(runner, login_key: str, account: str) -> None:
    """A box sign-in finished: mark its login signed in, as the account the box
    read back. A mint with no login (an older runner, or one started before
    there was a list) signed in the box's default login — which joins the list
    first, where it always sat in the cascade."""
    from .models import RunnerLogin

    key = login_key or RunnerLogin.DEFAULT
    if not runner.logins.filter(key=key).exists():
        if key != RunnerLogin.DEFAULT:
            return  # removed while the sign-in was in flight; the box drops it
        report_runner_logins(runner, [], adopt_default=True)
    runner.logins.filter(key=key).update(
        signed_in=True, account=account[:254], reported_at=timezone.now())
