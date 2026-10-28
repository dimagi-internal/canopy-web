"""What a runner's Refresh is doing, for the person who pressed it.

The request itself is `Runner.refresh_requested_at`, read by the box off its
heartbeat and discharged when it reports a bootstrap newer than it
(`Runner.refresh_requested`; `refresh_pending` adds canopy's own ask when the
box's CLI is behind the release, which has no asker). This module is the account of it a person reads:

- `versions` — what the box is on (runner code, Claude Code, the canopy CLI),
  read from its own report. Snapshotted at request time (`Runner.refresh_before`)
  so the finished state can say what moved.
- `status` — idle, pending (and what it is waiting on: the box is offline, turns
  are in flight, or its floor between self-restarts), or done (when, how long it
  took, the before → after of what changed, and any of its refresh checks that
  did not come back clean).
- `finished` — the one notice the asker gets when the server sees the request
  discharged, by email and Web Push, linking to the runner's page. A refresh
  that never finishes is the `runner_refresh` alert's
  (`alerts_runner_refresh`), handed to the same person.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape

from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from apps.push.services import send_to_user

from . import clis as cli_vocab
from .alerts_runner_credential import runner_path
from .credential_notify import runner_url
from .models import Runner, Turn

logger = logging.getLogger(__name__)

_VERSION = re.compile(r"\d+\.\d+\.\d+")

#: The checks a refresh itself reports on: a cloud box's bootstrap, a laptop's
#: in-place refresh, and the two installs it updates. One not `ok` after a
#: refresh is what "it finished, but" means.
REFRESH_CHECKS = ("bootstrap", "refresh", "canopy.cli", "claude.version")


def _checks(runner) -> dict[str, dict]:
    return {str(c.get("name")): c for c in (runner.health or {}).get("checks") or [] if isinstance(c, dict)}


def _version_in(check: dict | None) -> str:
    m = _VERSION.search(str((check or {}).get("detail") or ""))
    return m.group(0) if m else ""


def versions(runner) -> dict[str, str]:
    """What the box says it is on: name → version, only the ones it reports."""
    checks = _checks(runner)
    claude = _version_in(checks.get("claude.version"))
    if not claude:
        report = cli_vocab.reported(runner).get(cli_vocab.CLAUDE_CODE)
        claude = (report.version if report else "") or ""
    code = runner.code_version or ""
    if runner.code_sha:
        code = f"{code} ({runner.code_sha[:12]})" if code else runner.code_sha[:12]
    out = {"runner code": code, "Claude Code": claude, "canopy CLI": _version_in(checks.get("canopy.cli"))}
    return {k: v for k, v in out.items() if v}


@dataclass
class Change:
    name: str
    before: str
    after: str


@dataclass
class Status:
    #: `none` (never asked), `pending`, or `done`.
    state: str
    requested_at: datetime | None = None
    requested_by: str = ""
    #: Pending only — why it has not run yet, most immediate first: `offline`,
    #: `busy` (turns in flight), `floor` (too soon after its last self-restart).
    #: Empty: nothing is in the way, it runs at its next heartbeat.
    waiting_on: list[str] = field(default_factory=list)
    in_flight: int = 0
    #: The box's floor: the earliest it will restart itself again.
    eligible_at: datetime | None = None
    finished_at: datetime | None = None
    took_seconds: int | None = None
    #: Done: what moved since the request, and what did not come back clean.
    #: Pending: why a box that is due and idle could not start it.
    changes: list[Change] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def in_flight(runner) -> int:
    return Turn.objects.filter(claimed_by=runner,
                               status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN]).count()


def floor_until(runner) -> datetime | None:
    """When the box says it may next restart itself (`health.refresh_floor_until`)."""
    try:
        at = float((runner.health or {}).get("refresh_floor_until") or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(at, tz=UTC) if at else None


def problems(runner) -> list[str]:
    checks = _checks(runner)
    return [f"{n}: {checks[n].get('detail') or checks[n].get('status')}" for n in REFRESH_CHECKS
            if n in checks and checks[n].get("status") != "ok"]


def changes(runner) -> list[Change]:
    before, after = runner.refresh_before or {}, versions(runner)
    return [Change(name, before.get(name, ""), after.get(name, ""))
            for name in dict.fromkeys([*before, *after]) if before.get(name, "") != after.get(name, "")]


def _who(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.email or user.get_username()


def status(runner, now=None) -> Status:
    if runner.refresh_requested_at is None:
        return Status("none")
    now = now or timezone.now()
    who = _who(runner.refresh_requested_by) if runner.refresh_requested_by_id else ""
    if not runner.refresh_requested():
        finished = runner.refresh_finished_at
        took = int((finished - runner.refresh_requested_at).total_seconds()) if finished else None
        return Status("done", runner.refresh_requested_at, who, finished_at=finished, took_seconds=took,
                      changes=changes(runner), problems=problems(runner))
    waiting: list[str] = []
    if runner.live_status not in (Runner.ONLINE, Runner.DEGRADED):
        waiting.append("offline")
    # A laptop refreshes in place in the background, whatever is running; only a
    # cloud box restarts, so only it waits for its turns to finish.
    busy = in_flight(runner) if runner.kind == Runner.CLOUD else 0
    if busy:
        waiting.append("busy")
    floor = floor_until(runner)
    if floor and floor > now:
        waiting.append("floor")
    # `problems` on a pending refresh too: a box that is due, idle and still not
    # restarting says why in its `refresh` check, and that is the line to show.
    return Status("pending", runner.refresh_requested_at, who, waiting_on=waiting, in_flight=busy,
                  eligible_at=floor if floor and floor > now else None, problems=problems(runner))


def snapshot(runner) -> dict[str, str]:
    """What to compare the finished refresh against: the box as it is now."""
    return versions(runner)


def finished(runner, now) -> None:
    """Called after a beat discharges the request. Claims the finish with a
    conditional update — two beats can arrive together — and the one that wins
    tells the asker, after the commit."""
    if runner.refresh_requested_at is None or runner.refresh_finished_at is not None:
        return
    won = Runner.objects.filter(pk=runner.pk, refresh_requested_at=runner.refresh_requested_at,
                                refresh_finished_at__isnull=True).update(refresh_finished_at=now)
    if not won:
        return
    runner.refresh_finished_at = now
    if runner.refresh_requested_by_id:
        pk = runner.pk
        transaction.on_commit(lambda: _notify(pk))


def message(runner) -> tuple[str, str]:
    """(title, body) of the finished notice."""
    st = status(runner)
    took = f" (took {max(1, round(st.took_seconds / 60))} min)" if st.took_seconds is not None else ""
    title = (f"{runner.name} refreshed{took}" if not st.problems
             else f"{runner.name} refreshed{took}, with problems")
    lines = [f"{c.name}: {c.before or '?'} → {c.after or '?'}" for c in st.changes] or ["Nothing it reports changed version."]
    lines += st.problems
    return title, "\n".join(lines)


def _notify(runner_pk) -> None:
    """Email + push the asker. Best-effort: a failed send never undoes the finish."""
    runner = Runner.objects.filter(pk=runner_pk).select_related("refresh_requested_by").first()
    person = runner.refresh_requested_by if runner else None
    if person is None or not person.is_active:
        return
    try:
        title, body = message(runner)
        link = runner_url(runner)
        if person.email:
            text = f"{body}\n\nOpen {runner.name}: {link}\n\nYou are getting this because you asked for this refresh.\n"
            html = ("".join(f"<p>{escape(line)}</p>" for line in body.splitlines())
                    + f"<p><a href=\"{escape(link)}\">Open {escape(runner.name)}</a></p>"
                    + "<p style=\"color:#78716c;font-size:13px\">You are getting this because you asked "
                      "for this refresh.</p>")
            mail = EmailMultiAlternatives(subject=f"Canopy · {title}", body=text, to=[person.email])
            mail.attach_alternative(html, "text/html")
            mail.send()
        send_to_user(person, title, body.splitlines()[0][:180], runner_path(runner))
    except Exception:  # noqa: BLE001
        logger.exception("refresh notice failed for runner %s", runner_pk)
