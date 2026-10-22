"""Alert source `runner_credential`: a cloud runner's Claude credential is signed out.

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

Resolves when the probe stops failing (the next reconcile no longer reports it).
`set_token` is a link to the runner's credentials; taking it does not close the
alert by itself — the box re-probing the new value does.

This replaces the once-a-day `claude_auth_required` mail in `credential_notify`:
the alert's own blocking notice reaches the same people (the runner's owner and
admins, who hold `set_token`).

Private runners (`workspace_id` NULL) are skipped in v1: an alert needs a
workspace to live in (`Alert.workspace` is NOT NULL) and a private box's
credentials are its owner's alone.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta

from apps.attention.alerts import DISMISS, Action, Raise

from .credential_notify import runner_path
from .models import Runner, RunnerLogin
from .routing import agents_routed_to
from .runners import _first_values, can_administer_runner

KIND = "runner_credential"
SEVERITY = "blocking"
CONFIRM_WITHIN = timedelta(hours=2)
CLOSING_ACTION_IDS = ("set_token", "dismiss")

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


def is_signed_out(check: dict) -> bool:
    detail = str(check.get("detail") or "").lower()
    return check.get("status") == "fail" and any(m in detail for m in SIGNED_OUT_MARKERS)


def _stable(detail: str) -> str:
    return _PROBED_AT.sub("", detail or "").strip()


def label_for(runner, slot: str) -> str:
    """The name a person knows this credential by.

    Slots are the box's cascade labels: `subscription-1`/`subscription-2` (the
    first two tokens on the list, from an older bundle), `token:<key>` /
    `api-key:<key>` (a list entry), `box-login[:<key>]` (a login held on the
    box). The tokens are found the way the status view finds them
    (`runners._first_values`); a login by its key, named by its account.
    """
    if slot in ("subscription-1", "subscription-2"):
        first = slot == "subscription-1"
        entry = _first_values(runner)["claude_token" if first else "claude_token_secondary"]
        name = (entry.name if entry is not None else "").strip()
        return name or ("primary token" if first else "secondary token")
    if slot == _BOX_LOGIN or slot.startswith(_BOX_LOGIN + ":"):
        key = slot.partition(":")[2] or RunnerLogin.DEFAULT
        login = RunnerLogin.objects.filter(runner=runner, kind=RunnerLogin.LOGIN, key=key).first()
        return (login.account if login is not None else "").strip() or slot
    for prefix, kind in (("token:", RunnerLogin.TOKEN), ("api-key:", RunnerLogin.API_KEY)):
        if slot.startswith(prefix):
            entry = RunnerLogin.objects.filter(runner=runner, kind=kind, key=slot[len(prefix):]).first()
            return (entry.name if entry is not None else "").strip() or slot
    return slot


def _routed_slugs(runner) -> list[str]:
    return list(agents_routed_to(runner).values_list("slug", flat=True))


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    runners = Runner.objects.exclude(status=Runner.RETIRED).filter(workspace_id__isnull=False)
    for runner in runners:
        checks = [c for c in ((runner.health or {}).get("checks") or [])
                  if isinstance(c, dict) and str(c.get("name", "")).startswith(PROBE_PREFIX)]
        dead = [c for c in checks if is_signed_out(c)]
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
                title=f'The "{label_for(runner, slot)}" token on {runner.name} is signed out',
                url=runner_path(runner),
                scopes=scopes,
                body=detail,
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
        label = label_for(runner, slot)
        return [Action("set_token", f'Set a new "{label}" token', True, href=runner_path(runner)), DISMISS]
    # An owner of an agent this box runs (named on the alert, and still routed
    # here) cannot fix the box, but can tell the person who can.
    slugs = [s.removeprefix("agent:") for s in alert.scopes or [] if s.startswith("agent:")]
    if (runner.owner_id and runner.owner_id != user.pk and slugs
            and agents_routed_to(runner).filter(slug__in=slugs, owner=user).exists()):
        return [Action("ask_owner", f"Ask {_name(runner.owner)} to fix it", False,
                       notify=(runner.owner_id,))]
    return []


def perform(alert, user, action_id) -> None:
    """No server-side handlers: `set_token` is a link, `ask_owner` a notify and
    `dismiss` the framework's."""
    raise ValueError(f"runner_credential has no handler for {action_id!r}")
