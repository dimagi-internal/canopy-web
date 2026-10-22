"""Alert source `runner_credential`: a cloud runner's Claude credential no longer works.

A box proves each credential it holds with one short prompt and reports the
answer as a health check, `claude.probe.<slot>` (`runner/ec2/cloud_runner.py`
`_record_probe`). A credential that needs a person to sign it in again is
reported `status="fail"` with the detail
`"not signed in — someone has to sign it in again · probed <ISO>"`. Until
someone does, every turn on that credential fails — so this is `blocking`.

The title names the credential the way a PERSON named it — the token's name on
the runner's credential list (`claude_token_label` in the status view), a
login's account — never the box's slot name. cloud-ec2-2, 2026-10-22: the token
labelled "hal" was signed out and every surface called it "subscription-1".

Two actions, the only two that fix it (Jonathan, 2026-10-22: "remove token or
replace token"): `replace` links to the runner's credentials, and the alert
closes when the box re-probes the new value; `remove` drops the entry here.
`remove` is offered only where removing is allowed — never the runner's last
token or API key (`runners.LAST_FALLBACK_REFUSAL`) — so the alert never shows a
button that would be refused. No dismiss: a broken credential is either fixed
or gone. Resolves when the probe stops failing; `remove` also drops the box's
stale check for that slot, so the alert can't reopen before the box re-probes.

This replaces the once-a-day `claude_auth_required` mail in `credential_notify`:
the alert's own blocking notice reaches the same people (the runner's owner and
admins, who hold `replace`).

Private runners (`workspace_id` NULL) are skipped in v1: an alert needs a
workspace to live in (`Alert.workspace` is NOT NULL) and a private box's
credentials are its owner's alone.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta

from apps.attention.alerts import Action, Raise

from .models import Runner, RunnerLogin
from .routing import agents_routed_to
from .runners import (
    CredentialRefused,
    _first_values,
    _would_leave_no_fallback,
    can_administer_runner,
    remove_runner_credential,
)

KIND = "runner_credential"
SEVERITY = "blocking"
CONFIRM_WITHIN = timedelta(hours=2)
CLOSING_ACTION_IDS = ("replace", "remove")

PROBE_PREFIX = "claude.probe."

#: Lower-case phrases that mean "a person has to sign this credential in again".
#: The first is the box's own health-check wording (`_record_probe`, status
#: `dead`); the others are the Claude CLI's sentences (`_AUTH_REQUIRED_MARKERS`
#: in cloud_runner.py), which is how the box's journal printed the outcome on
#: cloud-ec2-2: `subscription-1 dead (Not logged in · Please run /login)`.
SIGNED_OUT_MARKERS = ("not signed in", "not logged in", "please run /login")

#: The volatile tail the box appends to every probe detail: when it asked.
_PROBED_AT = re.compile(r"\s*·\s*probed\s+\S+\s*$", re.IGNORECASE)

_BOX_LOGIN = "box-login"


def runner_path(runner) -> str:
    """The runner's detail on /supervisor, app-relative (/supervisor is not a
    tenant page, so it has no `/w/<ws>/` prefix). `credential_notify` links
    its mail here too."""
    return f"/supervisor?tab=runners&runner={runner.pk}"


def watches(runner) -> bool:
    """Is `runner` one this source reconciles? The same filter as `_runners()`:
    not retired, and in a workspace (an alert needs one to live in).
    `credential_notify.alert_covers` asks this, so a box the source skips still
    gets its mail."""
    return runner.status != Runner.RETIRED and runner.workspace_id is not None


def _runners():
    return Runner.objects.exclude(status=Runner.RETIRED).filter(workspace_id__isnull=False)


def signed_out_checks(runner) -> list[dict]:
    """The runner's `claude.probe.*` health checks that say it is signed out."""
    return [c for c in ((runner.health or {}).get("checks") or [])
            if isinstance(c, dict) and str(c.get("name", "")).startswith(PROBE_PREFIX) and is_signed_out(c)]


def is_signed_out(check: dict) -> bool:
    detail = str(check.get("detail") or "").lower()
    return check.get("status") == "fail" and any(m in detail for m in SIGNED_OUT_MARKERS)


def _stable(detail: str) -> str:
    return _PROBED_AT.sub("", detail or "").strip()


def entry_for(runner, slot: str):
    """The credential-list entry a slot names, or None when it is no longer on
    the list.

    Slots are the box's cascade labels: `subscription-1`/`subscription-2` (the
    first two tokens on the list, from an older bundle), `token:<key>` /
    `api-key:<key>` (a list entry), `box-login[:<key>]` (a login held on the
    box), `api-key` (an older bundle's API key). The tokens are found the way
    the status view finds them (`runners._first_values`).
    """
    if slot in ("subscription-1", "subscription-2"):
        return _first_values(runner)["claude_token" if slot == "subscription-1" else "claude_token_secondary"]
    if is_login(slot):
        key = slot.partition(":")[2] or RunnerLogin.DEFAULT
        return RunnerLogin.objects.filter(runner=runner, kind=RunnerLogin.LOGIN, key=key).first()
    if slot == "api-key":
        return _first_values(runner)["claude_api_key"]
    for prefix, kind in (("token:", RunnerLogin.TOKEN), ("api-key:", RunnerLogin.API_KEY)):
        if slot.startswith(prefix):
            return RunnerLogin.objects.filter(runner=runner, kind=kind, key=slot[len(prefix):]).first()
    return None


def _noun(slot: str) -> str:
    if is_login(slot):
        return "login"
    return "API key" if slot == "api-key" or slot.startswith("api-key:") else "token"


_UNNAMED = {"subscription-1": "primary token", "subscription-2": "secondary token", "api-key": "API key"}


def label_for(runner, slot: str) -> str:
    """The name a person knows this credential by: a token's name, a login's
    account. A known kind with no name gets a friendly default, never the slot
    string."""
    known = slot in _UNNAMED or slot.startswith(("token:", "api-key:")) or is_login(slot)
    if not known:
        return slot
    entry = entry_for(runner, slot)
    name = ((entry.account if is_login(slot) else entry.name) if entry is not None else "") or ""
    return name.strip() or _UNNAMED.get(slot) or f"an unnamed {_noun(slot)}"


def _removable(runner, slot: str) -> bool:
    entry = entry_for(runner, slot)
    return entry is not None and not _would_leave_no_fallback(runner, {entry.key})


def is_login(slot: str) -> bool:
    return slot == _BOX_LOGIN or slot.startswith(_BOX_LOGIN + ":")


def title_for(runner, slot: str) -> str:
    """`The "<label>" token|login|API key on <runner> no longer works` — never the slot."""
    return f'The "{label_for(runner, slot)}" {_noun(slot)} on {runner.name} no longer works'


def body_for(runner, slot: str) -> str:
    """One plain sentence: what is wrong and what fixes it."""
    noun = _noun(slot)
    if is_login(slot):
        what = "Its sign-in has lapsed."
        fix = "Sign it in again"
    else:
        what = "Claude rejects it."
        fix = f"Replace it with a new {noun}"
    if _removable(runner, slot):
        return f"{what} {fix}, or remove it."
    if entry_for(runner, slot) is not None and not is_login(slot):
        return f"{what} It is this runner's only {noun}, so it can't be removed. {fix}."
    return f"{what} {fix}."


def _routed_slugs(runner) -> list[str]:
    return list(agents_routed_to(runner).values_list("slug", flat=True))


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    for runner in _runners():
        dead = signed_out_checks(runner)
        if not dead:
            continue
        scopes = (f"runner:{runner.pk}", f"workspace:{runner.workspace_id}",
                  *[f"agent:{s}" for s in _routed_slugs(runner)])
        for check in dead:
            slot = str(check["name"])[len(PROBE_PREFIX):]
            detail = str(check.get("detail") or "")
            out.append(Raise(
                subject_key=f"runner:{runner.pk}:cred:{slot}",
                workspace=runner.workspace_id,
                title=title_for(runner, slot),
                url=runner_path(runner),
                scopes=scopes,
                body=body_for(runner, slot),
                fingerprint=f"{slot}\n{_stable(detail)}",
            ))
    return out


def _runner_of(alert):
    parts = alert.subject_key.split(":")
    if len(parts) < 4 or parts[0] != "runner" or parts[2] != "cred":
        return None, ""
    try:
        pk = uuid.UUID(parts[1])
    except ValueError:
        return None, ""
    return Runner.objects.filter(pk=pk).select_related("owner").first(), ":".join(parts[3:])


def _name(user) -> str:
    return (user.get_full_name() or "").strip() or user.email or user.username


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    runner, slot = _runner_of(alert)
    if runner is None:
        return []
    if can_administer_runner(user, runner):
        noun = _noun(slot)
        replace = "Sign in again" if is_login(slot) else f"Replace {noun}"
        out = [Action("replace", replace, True, href=runner_path(runner))]
        if _removable(runner, slot):
            out.append(Action("remove", f"Remove {noun}", True))
        return out
    # An owner of an agent this box runs (named on the alert, and still routed
    # here) cannot fix the box, but can tell the person who can.
    slugs = [s.removeprefix("agent:") for s in alert.scopes or [] if s.startswith("agent:")]
    if (runner.owner_id and runner.owner_id != user.pk and slugs
            and agents_routed_to(runner).filter(slug__in=slugs, owner=user).exists()):
        return [Action("ask_owner", f"Ask {_name(runner.owner)} to fix it", False,
                       notify=(runner.owner_id,))]
    return []


def perform(alert, user, action_id) -> None:
    """`remove` drops the entry from the runner's list. `replace` is a link and
    `ask_owner` a notify, so neither reaches here."""
    if action_id != "remove":
        raise ValueError(f"runner_credential has no handler for {action_id!r}")
    runner, slot = _runner_of(alert)
    entry = entry_for(runner, slot) if runner is not None else None
    if entry is None:
        raise ValueError("That credential is no longer on this runner.")
    try:
        if not remove_runner_credential(runner, entry.key, by=user):
            raise ValueError("That credential is no longer on this runner.")
    except CredentialRefused as exc:
        raise ValueError(str(exc)) from exc
    # The box's failing check was about the entry just removed. Under an older
    # box's slot names (`subscription-1`) the next token moves into that slot, so
    # the stale check would reopen this alert against a token nobody has tested.
    # Drop it; the box reports that slot afresh on its next probe.
    checks = (runner.health or {}).get("checks") or []
    kept = [c for c in checks if c.get("name") != PROBE_PREFIX + slot]
    if len(kept) != len(checks):
        Runner.objects.filter(pk=runner.pk).update(health={**(runner.health or {}), "checks": kept})
