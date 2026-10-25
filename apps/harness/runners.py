"""Runner administration — everything about a box that is not routing or work.

Its declared flags (`RunnerFlag`, e.g. `sensitive_data`), its Claude credentials (one
ordered list of `RunnerLogin` rows: logins, tokens and API keys), refresh requests,
its admins (`RunnerAdmin`), and the browser-driven re-authentication
(`RunnerMint`). Split out of `services.py`; import these names from here.
"""
from __future__ import annotations

import re
import secrets

from django.db import transaction
from django.utils import timezone

from apps.workspaces.models import MAX_DEPTH

from .models import (
    Runner,
    RunnerAdmin,
    RunnerCredential,
    RunnerCredentialSet,
    RunnerEnrollment,
    RunnerFlag,
    RunnerLogin,
    RunnerMint,
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


# ---- The runner's Claude credentials: one ordered list (RunnerLogin) --------
class LoginOrderRefused(ValueError):
    """An order that is not exactly the runner's current credentials, rearranged."""


class CredentialRefused(ValueError):
    """A credential change the list may not take (see the message)."""


LAST_FALLBACK_REFUSAL = (
    "That is this runner's last token or API key. Keep at least one: a full login "
    "renews itself, and when its renewal fails a runner with nothing else stops "
    "(cloud-ec2-2, 2026-10-15). Add another first, then remove this one.")

#: The kinds that hold a value here (a login's lives on the box).
VALUE_KINDS = (RunnerLogin.TOKEN, RunnerLogin.API_KEY)


def list_owner(runner) -> dict:
    """Whose credential list `runner` reads, as filter/create kwargs: its credential
    set's (every member of a fleet reads its fleet's, `RunnerCredentialSet`), else
    its own."""
    if runner.credential_set_id:
        return {"credential_set_id": runner.credential_set_id}
    return {"runner": runner}


def list_readers(runner) -> list:
    """Every runner reading `runner`'s list: the live runners on its set, or itself."""
    if not runner.credential_set_id:
        return [runner]
    return list(Runner.objects.filter(credential_set_id=runner.credential_set_id)
                .exclude(status=Runner.RETIRED))


def _entries(runner, kind: str | None = None):
    qs = RunnerLogin.objects.filter(**list_owner(runner)).order_by("position", "created_at")
    return qs.filter(kind=kind) if kind else qs


def _lock_list(runner) -> None:
    """Serialize changes to `runner`'s list: its set's row, or its own."""
    if runner.credential_set_id:
        RunnerCredentialSet.objects.select_for_update().filter(pk=runner.credential_set_id).first()
    else:
        Runner.objects.select_for_update().filter(pk=runner.pk).first()


def _touch(runner, by=None) -> None:
    """Move the bundle's stamp: a box that sees it move proves its credentials again.
    Every runner reading the list sees it move: a set's change reaches each member."""
    for r in list_readers(runner):
        cred, _ = RunnerCredential.objects.get_or_create(runner=r)
        if by is not None:
            cred.updated_by = by
        cred.save()


#: For a change that moves a runner onto or off a list (`credential_sets`).
touch_list = _touch


def _renumber(runner) -> None:
    for i, entry in enumerate(_entries(runner)):
        if entry.position != i:
            entry.position = i
            entry.save(update_fields=["position"])


def _would_leave_no_fallback(runner, removing: set[str]) -> bool:
    """True when removing these entries takes away the runner's last token or API key
    (one whose value a vault serving the runner holds)."""
    in_vault = _vault_secret_names(runner)
    held = {e.key for e in _entries(runner).filter(kind__in=VALUE_KINDS) if e.vault_secret_name in in_vault}
    return bool(held) and not (held - removing)


def add_runner_credential(runner, *, kind: str, name: str = "", by=None, key: str = ""):
    """A token or API key, last in the list, with NO value here: its value goes into a
    vault serving the runner (`vault_secret_name`), sealed by whoever holds that vault's
    key — a person's browser, or the box after a sign-in. canopy-web never holds one.
    `key` lets the box name the entry it is about to fill."""

    if kind not in VALUE_KINDS:
        raise CredentialRefused(f"a {kind} is not pasted in: sign it in instead")
    if not open_runner_vault(runner):
        raise CredentialRefused(f"{runner.name} has no vault with a key yet: make its key first")
    key = key or secrets.token_hex(4)
    if not _ENTRY_KEY.fullmatch(key) or _entries(runner).filter(key=key).exists():
        raise CredentialRefused(f"{key!r} is not a free entry key")
    with transaction.atomic():
        _lock_list(runner)
        last = _entries(runner).order_by("-position").first()
        entry = RunnerLogin.objects.create(
            **list_owner(runner), key=key, kind=kind, created_by=by, name=(name or "").strip()[:200],
            position=(last.position + 1) if last else 0)
        _touch(runner, by)
    return entry


SET_LOGIN_REFUSAL = ("this runner reads a credential set, which holds tokens and API keys only: "
                     "a full login's refresh token rotates, so it cannot be shared")

#: An entry key: what `add_runner_credential` makes, and all a box may name.
_ENTRY_KEY = re.compile(r"[0-9a-f]{8}")


def open_runner_vault(runner):
    """The first vault serving `runner` that has a key (values can go in), or None."""
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — cycle: it imports this module

    return next((v for v in workspace_vaults.runner_vaults(runner) if v.kv), None)


class VaultWriteRefused(ValueError):
    """A box's vault write that does not fit its own list (see the message)."""


def store_vault_values(runner, *, vault_id: str, kv: int, values: dict[str, str]) -> list[str]:
    """The box's own write: `zk1:` ciphertext for names its list uses, into a vault
    serving it, at that vault's current key version. The bundle's stamp moves, so the
    box (and any other box the vault serves) reads it again."""
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — cycle: it imports this module

    vault = next((v for v in workspace_vaults.runner_vaults(runner) if str(v.pk) == str(vault_id)), None)
    if vault is None:
        raise VaultWriteRefused(f"vault {vault_id} does not serve {runner.name}")
    known = {e.vault_secret_name for e in _entries(runner).filter(kind__in=VALUE_KINDS)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise VaultWriteRefused(f"{', '.join(unknown)} is not on {runner.name}'s credential list")
    names = workspace_vaults.set_runner_values(vault, kv=kv, values=values, user=runner.owner)
    vault_values_written(vault, names)
    return names


def update_runner_credential(runner, key: str, *, name: str | None = None, by=None) -> bool:
    """A new name for one token or API key (a new value is sealed into the vault, never
    sent here). False when there is no such entry."""

    entry = _entries(runner).filter(key=key, kind__in=VALUE_KINDS).first()
    if entry is None:
        return False
    if name is not None:
        entry.name = name.strip()[:200]
        entry.save(update_fields=["name"])
    return True


def remove_runner_credential(runner, key: str, *, by=None) -> bool:
    """Drop one entry, of any kind. A login is signed out on the box, a token or
    key forgotten, the next time the box re-reads its credentials — within a few
    minutes. Refused when it is the last token or API key."""
    with transaction.atomic():
        _lock_list(runner)
        if not _entries(runner).filter(key=key).exists():
            return False
        if _would_leave_no_fallback(runner, {key}):
            raise CredentialRefused(LAST_FALLBACK_REFUSAL)
        gone = [e.vault_secret_name for e in _entries(runner).filter(key=key) if e.vault_secret_name]
        _entries(runner).filter(key=key).delete()
        _forget_vault_secrets(runner, gone)
        _renumber(runner)
        _touch(runner, by)
    return True


def order_runner_credentials(runner, keys: list[str], *, by=None) -> None:
    """Make the order exactly `keys` — every entry on the runner, once each.

    Refused rather than partially applied: an order that names an entry twice or
    leaves one out is a stale page, and guessing where the rest go would put a
    credential somewhere nobody chose.
    """
    with transaction.atomic():
        entries = {e.key: e for e in _entries(runner).select_for_update()}
        if sorted(keys) != sorted(entries):
            raise LoginOrderRefused(
                "the order must name every credential on this runner exactly once — "
                "reload the page and try again")
        for i, key in enumerate(keys):
            if entries[key].position != i:
                entries[key].position = i
                entries[key].save(update_fields=["position"])
        _touch(runner, by)


def _first_values(runner) -> dict:
    """The old fixed slots, read off the list: the first two tokens and the first
    API key (the entries, never a value)."""
    tokens = list(_entries(runner, RunnerLogin.TOKEN))
    api_key = _entries(runner, RunnerLogin.API_KEY).first()
    return {
        "claude_token": tokens[0] if tokens else None,
        "claude_token_secondary": tokens[1] if len(tokens) > 1 else None,
        "claude_api_key": api_key,
    }


def get_runner_credential(runner) -> dict:
    """The runner's credential list, for the box: every entry in order, a token's or API
    key's `vault_secret` when a vault serving it holds the value (the box opens it with
    its own device key, `…/credential/resolve`), and the logins it should keep. No
    value: canopy-web holds none."""

    entries = list(_entries(runner))
    cred = getattr(runner, "credential", None)
    held = _vault_secret_names(runner)
    return {
        "logins": [{"key": e.key, "account": e.account}
                   for e in entries if e.kind == RunnerLogin.LOGIN],
        "cascade": [{"key": e.key, "kind": e.kind,
                     "vault_secret": e.vault_secret_name if e.vault_secret_name in held else ""}
                    for e in entries],
        "vault_open": open_runner_vault(runner) is not None,
        "updated_at": cred.updated_at if cred is not None else None,
    }


def vault_values_written(vault, names) -> None:
    """Someone holding a vault's key sealed values into it: each runner the vault serves
    whose list names one has its bundle's stamp moved, so the box reads the vault again."""
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — cycle: it imports this module

    names = set(names)
    for runner in workspace_vaults.serving_runners(vault):
        if any(e.vault_secret_name in names for e in _entries(runner).filter(kind__in=VALUE_KINDS)):
            _touch(runner)


def _forget_vault_secrets(runner, names: list[str]) -> None:
    """Delete these secrets from every vault serving `runner` (every vault of its set),
    unless another runner that vault serves still lists one (a shared vault keeps what
    anyone uses)."""
    if not names:
        return
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — cycle: it imports this module
    from apps.agents.models import VaultSecret  # noqa: PLC0415 — with the module above

    mine = list_owner(runner)
    # A set's entry has a value in each tree's vault, not only this runner's.
    vaults = (list(runner.credential_set.vaults.all()) if runner.credential_set_id
              else workspace_vaults.runner_vaults(runner))
    for v in vaults:
        # Any OTHER list a runner the vault serves reads (the runners on one set
        # read one list, so they are not "others" to each other).
        others: set[str] = set()
        for r in workspace_vaults.serving_runners(v):
            if list_owner(r) != mine:
                others |= {e.vault_secret_name for e in _entries(r)}
        VaultSecret.objects.filter(vault=v, name__in=[n for n in names if n not in others]).delete()


def _vault_secret_names(runner) -> set[str]:
    """Every secret name the vaults serving `runner` hold (spec 2026-10-23-runner-claude-
    credentials-in-the-vault-design.md): a list entry whose value is among them is read
    from there by the box, with its own device key."""
    # cycle: agents.credentials.workspace_vaults imports this module (enrolled_roots)
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — import cycle, above

    return workspace_vaults.runner_secret_names(runner)


def _held(entry, held: set[str]) -> bool:
    return entry is not None and entry.vault_secret_name in held


def runner_credential_status(runner) -> dict:
    """Masked view — which credentials are held, never their values. Names are
    not secrets, so they ride along."""
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — cycle: it imports this module

    from . import credential_sets  # noqa: PLC0415 — cycle: it imports this module

    entries = list(_entries(runner))
    slots = _first_values(runner)
    cred = getattr(runner, "credential", None)
    vaults = workspace_vaults.runner_vaults(runner)
    held = _vault_secret_names(runner) if vaults else set()
    return {
        "credentials": [{"key": e.key, "kind": e.kind, "name": e.name, "account": e.account,
                         "signed_in": e.signed_in,
                         "has_value": e.vault_secret_name in held,
                         "in_vault": bool(e.vault_secret_name) and e.vault_secret_name in held,
                         "vault_secret": e.vault_secret_name,
                         "reported_at": e.reported_at} for e in entries],
        "vaults": [{"vault_id": str(v.pk), "name": v.name, "kv": v.kv} for v in vaults],
        "credential_set": credential_sets.summary(runner.credential_set) if runner.credential_set_id else None,
        "logins": [{"key": e.key, "account": e.account, "signed_in": e.signed_in,
                    "reported_at": e.reported_at} for e in entries if e.kind == RunnerLogin.LOGIN],
        "has_claude_token": _held(slots["claude_token"], held),
        "has_claude_token_secondary": _held(slots["claude_token_secondary"], held),
        "has_claude_api_key": _held(slots["claude_api_key"], held),
        "claude_token_label": slots["claude_token"].name if slots["claude_token"] else "",
        "claude_token_secondary_label": (slots["claude_token_secondary"].name
                                         if slots["claude_token_secondary"] else ""),
        "updated_at": cred.updated_at if cred is not None else None,
    }


def _nth_token(runner, n: int):
    tokens = list(_entries(runner, RunnerLogin.TOKEN))
    return tokens[n] if n < len(tokens) else None


def swap_runner_logins(runner, *, updated_by=None):
    """Swap the first two tokens' places in the list.

    What a box from before the list asks for when its second token ran a turn
    its first could not (`_promote_working_subscription`)."""
    with transaction.atomic():
        _lock_list(runner)
        first, second = _nth_token(runner, 0), _nth_token(runner, 1)
        if first is None or second is None:
            return getattr(runner, "credential", None)
        first.position, second.position = second.position, first.position
        first.save(update_fields=["position"])
        second.save(update_fields=["position"])
        _touch(runner, updated_by)
    return runner.credential


def add_runner_login(runner, *, by=None):
    """Add a login at the END of the list and start signing it in.

    The row exists from the start rather than on success: a sign-in that is
    abandoned leaves a row saying "not signed in", with Sign in and Remove next
    to it, instead of a login that silently never appeared.

    Refused for a runner on a credential set (a fleet member): a login's refresh
    token rotates, so it cannot be shared, and it would be state on one box.
    """
    if runner.credential_set_id:
        raise CredentialRefused(SET_LOGIN_REFUSAL)

    with transaction.atomic():
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        last = _entries(runner).order_by("-position").first()
        # No stamp: the box has nothing new to prove until the login is signed in.
        login = RunnerLogin.objects.create(
            runner=runner, key=secrets.token_hex(4), created_by=by, kind=RunnerLogin.LOGIN,
            position=(last.position + 1) if last else 0)
    start_runner_mint(runner, requested_by=by, slot="box", login_key=login.key)
    return login


def remove_runner_login(runner, key: str) -> bool:
    """The old name for removing an entry."""
    return remove_runner_credential(runner, key)


def order_runner_logins(runner, keys: list[str]) -> None:
    """The old route: every credential, or just the logins — then the logins are
    reordered among the places they already hold, and the rest stay put."""
    logins = list(_entries(runner, RunnerLogin.LOGIN).values_list("key", flat=True))
    if sorted(keys) != sorted(logins):
        return order_runner_credentials(runner, keys)
    wanted = iter(keys)
    order_runner_credentials(runner, [next(wanted) if kind == RunnerLogin.LOGIN else key
                                      for key, kind in _entries(runner).values_list("key", "kind")])


def report_runner_logins(runner, reports: list[dict], *, adopt_default: bool = False) -> None:
    """The box says which of its logins are signed in, and as whom.

    `adopt_default`: the box found a login in the CLI's own directory that this
    list has never held — one signed in before there was a list, or by hand on
    the box. It goes FIRST, where that login always was.
    """
    if runner.credential_set_id:
        # A member reads its set's tokens and keys; a login found on its disk is
        # not one of them, and is never adopted onto the shared list.
        return
    now = timezone.now()
    with transaction.atomic():
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        if adopt_default and not runner.logins.filter(key=RunnerLogin.DEFAULT).exists():
            for login in runner.logins.all():
                login.position += 1
                login.save(update_fields=["position"])
            RunnerLogin.objects.create(runner=runner, key=RunnerLogin.DEFAULT, position=0,
                                       kind=RunnerLogin.LOGIN)
        for r in reports:
            runner.logins.filter(kind=RunnerLogin.LOGIN, key=r.get("key", "")).update(
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
    return runner.pk in administered_runner_ids(user, [runner])


def administered_runner_ids(user, runners) -> set:
    """`can_administer_runner` for many runners at once: the pks of those `user`
    administers, in at most one query (none when they own every one)."""
    if not getattr(user, "is_authenticated", False):
        return set()
    # A runner with NO owner is administered by nobody — it used to be
    # administered by everyone, a NULL-means-allow leg like the six this repo
    # has already removed elsewhere.
    runners = [r for r in runners if r.owner_id is not None]
    out = {r.pk for r in runners if r.owner_id == user.id}
    rest = [r.pk for r in runners if r.pk not in out]
    if rest:
        out |= set(RunnerAdmin.objects.filter(runner_id__in=rest, user=user).values_list("runner_id", flat=True))
    return out


def may_trust_device_keys(user, runner) -> bool:
    """May this person put a device key on this box, or approve one?

    On a shared box, whoever administers it. On a PRIVATE runner, its owner alone:
    that box's token signs its owner's own sessions in AS the owner, opens their
    local turns and reads secrets under their license, so a runner admin who could
    trust a key there could act as the owner. Admins still see the keys, and may
    revoke one — taking trust away is never the risk."""
    if runner.private:
        return (getattr(user, "is_authenticated", False) and runner.owner_id is not None
                and runner.owner_id == user.id)
    return can_administer_runner(user, runner)


def grant_runner_admin(runner, user, *, granted_by=None):
    """Idempotent: re-granting returns the existing row rather than a second one
    that a later revoke would only half-remove."""
    admin, _ = RunnerAdmin.objects.get_or_create(
        runner=runner, user=user, defaults={"granted_by": granted_by})
    return admin


def revoke_runner_admin(runner, user) -> bool:
    """True when a grant was actually removed. The owner is not stored as a
    grant, so this can never revoke them — losing the last administrator of a box
    is not a state this should be able to produce."""
    deleted, _ = RunnerAdmin.objects.filter(runner=runner, user=user).delete()
    return bool(deleted)


def list_runner_admins(runner):
    return list(
        RunnerAdmin.objects.filter(runner=runner)
        .select_related("user", "granted_by")
        .order_by("created_at")
    )


# ---- The machine a runner is on --------------------------------------------
def set_runner_machine(runner, role_arn: str) -> bool:
    """Record that `runner` is on the machine whose instance role is `role_arn`
    (`Runner.machine_id`). Call it only with a role the box PROVED — an approved
    aws-iam device key, or pairing by role — never with one it merely named.
    Idempotent; True when the stored value changed. A role that is not one
    raises `aws_iam_proof.AwsProofError`."""
    from .aws_iam_proof import canonical_role_arn

    machine = canonical_role_arn(role_arn)
    if runner.machine_id == machine:
        return False
    type(runner).objects.filter(pk=runner.pk).update(machine_id=machine)
    runner.machine_id = machine
    return True


def backfill_machine_ids(runner_model, device_key_model) -> int:
    """Set `machine_id` on every runner with an ACTIVE aws-iam device key that
    has none yet, from that key's (already canonical) role ARN — the newest key
    when a runner has several. Takes the model classes so the migration passes its
    historical ones. Returns how many runners it set."""
    keys = (device_key_model.objects.filter(kind="aws-iam", status="active")
            .exclude(role_arn="").filter(runner__machine_id="")
            .order_by("runner_id", "-approved_at", "-created_at")
            .values_list("runner_id", "role_arn"))
    first: dict = {}
    for runner_id, role_arn in keys:
        first.setdefault(runner_id, role_arn)
    for runner_id, role_arn in first.items():
        runner_model.objects.filter(pk=runner_id).update(machine_id=role_arn)
    return len(first)


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

    if runner.kind == Runner.COMPUTER:
        # A computer in plain Claude Code (no runner app) serves no workspace's work.
        raise EnrollmentRefused(f"{runner.name} is a computer without the runner app; it serves no workspace")
    if not runner.owner_id or not perms.can(runner.owner, workspace_slug, perms.AGENT_WORK):
        raise EnrollmentRefused(
            f"this runner's owner is not an editor (or above) in '{workspace_slug}', "
            "so the runner cannot serve it")
    existing = RunnerEnrollment.objects.filter(runner=runner, workspace_id=workspace_slug).first()
    if existing is not None:
        # Idempotent, before the one-tree rule: a cloud box enrolled across two
        # trees before the rule may still re-send an enrollment it already has.
        return existing
    other_tree = _other_tree(runner, workspace_slug)
    if other_tree:
        raise EnrollmentRefused(
            f"a cloud runner serves one workspace tree: {runner.name} serves the "
            f"'{other_tree}' tree, and '{workspace_slug}' is not in it — pair another "
            "runner unit on the box for that tree")
    row, _ = RunnerEnrollment.objects.get_or_create(
        runner=runner, workspace_id=workspace_slug, defaults={"enrolled_by": by})
    return row


def _other_tree(runner, workspace_slug: str) -> str:
    """The root of a tree OTHER than `workspace_slug`'s that a CLOUD runner is
    already enrolled in — "" when there is none, or the runner is not a cloud box.

    A cloud runner is one isolation unit on its box: its own Unix user, keys and
    logins. Two trees on one unit would share all of that, so a cloud runner names
    one tree, and a box that serves two runs two units. A laptop has one desktop
    user however many runners it pairs, so it may span trees; its runner page says
    so (`RunnerOut.roots`)."""
    from apps.workspaces import services as wsvc

    if runner.kind != Runner.CLOUD:
        return ""
    existing = set(RunnerEnrollment.objects.filter(runner=runner)
                   .values_list("workspace_id", flat=True)) - {workspace_slug}
    if not existing:
        return ""
    roots = wsvc.tree_roots(existing | {workspace_slug})
    others = sorted({roots[s] for s in existing} - {roots[workspace_slug]})
    return others[0] if others else ""


def enrolled_roots(runner) -> list[str]:
    """The roots of the trees `runner` is enrolled in, sorted. More than one only
    for a laptop (`_other_tree`), or a cloud box enrolled before the rule."""
    from apps.workspaces import services as wsvc

    from .routing import enrolled_slugs

    slugs = enrolled_slugs(runner)
    return sorted(set(wsvc.tree_roots(slugs).values())) if slugs else []


def unenroll_runner(runner, workspace_slug: str) -> bool:
    """True when an enrollment was actually removed. The box stops serving that
    workspace at its next claim; a turn it already holds runs to completion."""
    deleted, _ = RunnerEnrollment.objects.filter(
        runner=runner, workspace_id=workspace_slug).delete()
    return bool(deleted)


def list_runner_enrollments(runner):
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
    return _expire_if_stale(
        RunnerMint.objects.filter(runner=runner).order_by("-created_at").first())


def claim_runner_mint(runner):
    """The mint this runner should act on right now, or None.

    Returns a row only in the two states where the RUNNER owes work — it has been
    asked to start, or a human has handed back a code. In `awaiting_code` the ball
    is with the human, and handing that back on every poll tick would have the
    runner restart the CLI under a URL somebody is already signing in to.
    """
    mint = current_runner_mint(runner)
    if mint is None or mint.status not in (RunnerMint.REQUESTED, RunnerMint.COMPLETING):
        return None
    return mint


def record_mint_url(mint, url: str):
    """The runner has the CLI running and a URL for the human."""
    mint.authorize_url = url
    mint.status = RunnerMint.AWAITING_CODE
    mint.save(update_fields=["authorize_url", "status", "updated_at"])
    return mint


def submit_mint_code(mint, code: str):
    """A human pasted the authorization code back."""
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
                       account: str = "", vault_key: str = ""):
    """The runner reports how a sign-in ended. A token sign-in is done only when the
    runner's vault holds the token under `vault_key`'s entry — sealed there by the box,
    never sent here — so a sign-in can no longer read "signed in" for a token nobody
    can use. The only secret a human handles is the single-use authorization code.

    The entry keeps its name. The token cannot say whose it is (a setup-token is
    scoped to inference only; the profile endpoint answers 403), and re-signing
    an expired login is the common case, which is the same account.
    """
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
    entry = _entries(mint.runner, RunnerLogin.TOKEN).filter(key=vault_key).first() if vault_key else None
    if (mint.slot == RunnerMint.TOKEN and entry is not None and not detail
            and entry.vault_secret_name in _vault_secret_names(mint.runner)):
        # Done only once the vault actually holds it: a sign-in that reports success
        # for a token nobody can use is the failure this whole flow exists to end.
        mint.status = RunnerMint.DONE
        mint.detail = "signed in; the token is sealed in the runner's vault"
    else:
        # A token is only ever sealed into the runner's vault on the box; one sent here
        # is refused and never stored (spec 2026-10-23-runner-claude-credentials-in-the-
        # vault-design.md, step 3). An entry named for this sign-in that never got its
        # value goes again, unless the sign-in was for it.
        if entry is not None and vault_key != mint.login_key \
                and entry.vault_secret_name not in _vault_secret_names(mint.runner):
            entry.delete()
            _renumber(mint.runner)
        mint.status = RunnerMint.FAILED
        mint.detail = ((NO_VAULT_COPY if token else "") or detail
                       or ("the token did not reach the runner's vault" if vault_key else "")
                       or "the runner did not complete the sign-in")
    mint.code = ""
    mint.save(update_fields=["status", "detail", "code", "updated_at"])
    return mint


#: What a sign-in says when the box could not seal its token: it holds no copy of the
#: runner's vault key yet (or the runner has no vault), and a token never comes here.
NO_VAULT_COPY = ("the sign-in completed, but this runner holds no copy of its vault's key, so it "
                 "could not keep the token — make the vault's key (or give this box its copy) on "
                 "the runner's Credentials tab, then sign in again")


def name_minted_entry(mint, vault_key: str) -> None:
    """The entry a token the box just signed in will be sealed under — the one the
    sign-in was for, else a new one at the end, named `vault_key` by the box so it can
    seal under that name before it reports the sign-in (`finish_runner_mint`). The
    value arrives by `store_vault_values`; canopy-web never sees it."""
    if mint.slot != RunnerMint.TOKEN or mint.status != RunnerMint.COMPLETING:
        raise CredentialRefused("no token sign-in is completing on this runner")
    runner, by = mint.runner, mint.requested_by
    if mint.login_key and _entries(runner, RunnerLogin.TOKEN).filter(key=mint.login_key).exists():
        if vault_key != mint.login_key:
            raise CredentialRefused(f"the sign-in was for {mint.login_key}, not {vault_key}")
        return
    add_runner_credential(runner, kind=RunnerLogin.TOKEN, by=by, key=vault_key)


def _record_signed_in(runner, login_key: str, account: str) -> None:
    """A box sign-in finished: mark its login signed in, as the account the box
    read back. A mint with no login (an older runner, or one started before
    there was a list) signed in the box's default login — which joins the list
    first, where it always sat in the cascade."""
    key = login_key or RunnerLogin.DEFAULT
    if not runner.logins.filter(kind=RunnerLogin.LOGIN, key=key).exists():
        if key != RunnerLogin.DEFAULT:
            return  # removed while the sign-in was in flight; the box drops it
        report_runner_logins(runner, [], adopt_default=True)
    runner.logins.filter(key=key).update(
        signed_in=True, account=account[:254], reported_at=timezone.now())
