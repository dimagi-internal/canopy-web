"""Alert source `runner_code`: a live runner is executing code that isn't what shipped.

Two ways to be wrong, one alert per runner, ranked:

1. `branch` — a SOURCE-mode runner (PYTHONPATH into a working checkout) reports
   a branch other than `main`: another process left it on stale or wrong code.
2. `outdated` — an INSTALLED runner reports the sha it was built from, and it
   differs from what the deployed server expects for its kind
   (`Runner.expected_code_sha`). Empty on either side is UNKNOWN, never stale.

An installed box updates ITSELF: the heartbeat that reports a differing sha
kicks its updater (`runner.update_available`), a 30-minute timer is the
fallback, and it defers while a turn is in flight. So right after a deploy every
live box is briefly `outdated`, and that is not a problem. A new `outdated`
alert waits `OUTDATED_GRACE` from when this server knew the new expectation —
the later of this process starting (a deploy restarts it) and the expected
sha's commit time. An alert already open stays open through the grace, so a
deploy does not resolve and reopen every long-stuck box.

A box NEWER than what shipped (its commit timestamp is later than the expected
one) raises nothing: the deploy catches up, or someone installed a branch on
purpose. With no timestamp to order the two, it counts as `outdated` — a box too
old to report one is the one most likely to be behind.

Only a box that is heartbeating raises it (`live_status` online, degraded or
paused). A quiet box's branch and sha are only its LAST report, and a dark box is
the fleet's normal state, so it stays a fact on the runner's detail page; the
alert resolves when the box goes quiet and reopens if it comes back still wrong.
That is the raise bar: only what a person can fix now, and it heals itself.

`action`, not `blocking`: the box still runs turns, just on the wrong code. The
fix is made on the machine (the body says where to look and what to run), so the one action is
a link to the runner's detail for its owner and admins. No dismiss: the code is
either fixed or the box goes quiet. Private runners (`workspace_id` NULL) are
skipped, as for `runner_credential`: an alert needs a workspace to live in.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from django.utils import timezone

from apps.attention.alerts import Action, Raise
from apps.attention.models import Alert

from .alerts_runner_credential import runner_path
from .models import HEARTBEAT_ONLINE_WINDOW, Runner
from .routing import agents_routed_to
from .runners import can_administer_runner

KIND = "runner_code"
SEVERITY = "action"
CONFIRM_WITHIN = timedelta(hours=2)
CLOSING_ACTION_IDS = ("open",)

#: How long a live box may stay behind a new deploy before it is a problem: the
#: updater's 30-minute timer, plus room for a turn in flight to finish.
OUTDATED_GRACE = timedelta(hours=2)

#: When this process started — a deploy restarts it, so it bounds when the
#: server began expecting the sha it serves.
STARTED_AT = timezone.now()

UPDATE_CMD = "runner/canopy_runner/scripts/install-runner.sh"
BRANCH_CMD = "git -C ~/emdash-projects/canopy-web checkout main && git pull"

_LIVE = (Runner.ONLINE, Runner.DEGRADED, Runner.PAUSED)


def fault(runner) -> str:
    """`branch`, `outdated`, or "" (nothing wrong, unknown, or ahead of the deploy)."""
    if runner.code_branch and runner.code_branch != "main":
        return "branch"
    expected = runner.expected_code_sha()
    if not (runner.code_sha and expected) or runner.code_sha == expected:
        return ""
    mine, shipped = runner.code_committed_at or 0, runner.expected_code_committed_at()
    return "" if mine and shipped and mine > shipped else "outdated"


def _raise(runner, kind: str) -> Raise:
    if kind == "branch":
        title = f'{runner.name} is running branch "{runner.code_branch}", not main'
        body = f"Its turns run stale or wrong code. On that machine: {BRANCH_CMD}"
        fingerprint = f"branch:{runner.code_branch}"
    else:
        version = runner.code_version or "an unknown version"
        title = f"{runner.name} is running an older runner than shipped"
        updater = "update_runner.sh" if runner.kind == Runner.CLOUD else "~/.canopy/updater.log"
        body = (f"It runs {version} built from {runner.code_sha[:12]}; "
                f"{runner.expected_code_sha()[:12]} has shipped and its updater hasn't installed it. "
                f"On that machine check {updater} and ~/.canopy/hold-updates, or reinstall: {UPDATE_CMD}")
        fingerprint = f"outdated:{runner.expected_code_sha()}"
    slugs = agents_routed_to(runner).values_list("slug", flat=True)
    return Raise(
        subject_key=f"runner_code:{runner.pk}",
        workspace=runner.workspace_id,
        title=title,
        url=runner_path(runner),
        scopes=(f"runner:{runner.pk}", f"workspace:{runner.workspace_id}", *[f"agent:{s}" for s in slugs]),
        body=body,
        fingerprint=fingerprint,
    )


def _in_grace(runner, now) -> bool:
    """Is this box still within its chance to update itself to the deployed sha?"""
    committed = runner.expected_code_committed_at()
    known_since = max(STARTED_AT, datetime.fromtimestamp(committed, tz=UTC)) if committed else STARTED_AT
    return now - known_since < OUTDATED_GRACE


def reconcile(now) -> list[Raise]:
    fresh = timezone.now() - HEARTBEAT_ONLINE_WINDOW
    runners = (Runner.objects.exclude(status=Runner.RETIRED)
               .filter(workspace_id__isnull=False, last_heartbeat_at__gte=fresh))
    already_open = set(Alert.objects.filter(kind=KIND, status=Alert.OPEN).values_list("subject_key", flat=True))
    out: list[Raise] = []
    for runner in runners:
        if runner.live_status not in _LIVE:
            continue
        kind = fault(runner)
        if not kind:
            continue
        if kind == "outdated" and _in_grace(runner, now) and f"runner_code:{runner.pk}" not in already_open:
            continue
        out.append(_raise(runner, kind))
    return out


def _runner_of(alert):
    prefix, _, pk = alert.subject_key.partition(":")
    if prefix != "runner_code":
        return None
    try:
        pk = uuid.UUID(pk)
    except ValueError:
        return None
    return Runner.objects.filter(pk=pk).select_related("owner").first()


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    runner = _runner_of(alert)
    if runner is None or not can_administer_runner(user, runner):
        return []
    return [Action("open", "Open runner", True, href=runner_path(runner))]


def perform(alert, user, action_id) -> None:
    """`open` is a link; nothing runs here."""
    raise ValueError(f"runner_code has no handler for {action_id!r}")
