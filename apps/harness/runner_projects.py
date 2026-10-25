"""Adding a repo to a LAPTOP runner as an emdash project, from canopy-web.

A laptop runs an agent in the emdash project named after it, and routing drops a
laptop that has none (`routing.cannot_serve`, `InheritedOrder.missing_repo`).
Making that project used to be a person at that Mac cloning the repo and adding it
in emdash by hand. Now a person asks here (`RunnerProjectRequest`); the box reads
the ask off its heartbeat reply (`RunnerOut.add_projects`), clones the repo under
its own account, opens it in emdash through emdash's own Add Project dialog
(`cdp/emdash_control.mjs add-project`), checks emdash's DB says it exists, and
reports back. docs/architecture/runners.md § Adding a repo to a laptop.

Cloud boxes are not asked: they clone every agent they serve on their own.
"""
from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from apps.realtime import groups

from .models import Runner, RunnerProjectRequest

#: An emdash project name the box can also use as a folder name: no path parts,
#: no leading dash (it becomes a `git clone` argument on the box).
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
#: A repo URL the box will hand to `git clone`: https, ssh or scp-style, no
#: whitespace, never something that reads as an option.
REPO_URL_RE = re.compile(r"^(https://|ssh://|git@)[^\s]+$")

#: How much of the box's own error is kept as the reason.
DETAIL_MAX = 2000


class ProjectRequestError(ValueError):
    """The ask cannot be made; the message says why, in words for the person."""


def can_add_projects(runner: Runner) -> bool:
    """Only a laptop emdash runner makes emdash projects."""
    return runner.kind == Runner.EMDASH


def name_from_repo_url(url: str) -> str:
    """`https://github.com/marshellis/bailey(.git)` → `bailey` — emdash's own
    default for a cloned repo (`extractRepoName`)."""
    tail = (url or "").strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def request_project(runner: Runner, *, repo_url: str, name: str = "", agent=None,
                    user=None) -> RunnerProjectRequest:
    """Ask `runner` to make `repo_url` its emdash project `name` (re-arming the row
    if it was asked before). Already held: recorded as added, nothing to do."""
    if not can_add_projects(runner):
        raise ProjectRequestError(
            "only a laptop (emdash) runner adds repos this way; a cloud box clones the "
            "agents it serves on its own")
    repo_url = (repo_url or "").strip()
    if not REPO_URL_RE.match(repo_url):
        raise ProjectRequestError(
            "the repo URL must be an https://, ssh:// or git@ address with no spaces")
    name = (name or "").strip() or name_from_repo_url(repo_url)
    if not NAME_RE.match(name):
        raise ProjectRequestError(
            f"{name!r} cannot be an emdash project name: use letters, digits, '.', '_' "
            "or '-', starting with a letter or digit")
    held = name in runner.observed_projects()
    now = timezone.now()
    row, _ = RunnerProjectRequest.objects.update_or_create(
        runner=runner, name=name,
        defaults={
            "repo_url": repo_url, "agent": agent, "requested_by": user,
            "status": RunnerProjectRequest.ADDED if held else RunnerProjectRequest.PENDING,
            "detail": "already an emdash project on this runner" if held else "",
            "requested_at": now, "finished_at": now if held else None,
        },
    )
    if not held:
        # The heartbeat carries the ask; the ring only makes the box beat sooner.
        transaction.on_commit(lambda: groups.publish(
            groups.runner_group(runner.pk), {"type": "runner.wake"}))
    return row


def owed(runner: Runner) -> list[RunnerProjectRequest]:
    """The asks this box still owes, after discharging any it already holds — the
    `add_projects` of its heartbeat reply. Observation wins over the box's own
    report: a project that exists is added, whoever made it."""
    if not can_add_projects(runner):
        return []
    pending = list(RunnerProjectRequest.objects.filter(
        runner=runner, status=RunnerProjectRequest.PENDING))
    if not pending:
        return []
    held = set(runner.observed_projects())
    done = [r.pk for r in pending if r.name in held]
    if done:
        RunnerProjectRequest.objects.filter(pk__in=done, status=RunnerProjectRequest.PENDING).update(
            status=RunnerProjectRequest.ADDED, finished_at=timezone.now(),
            detail="emdash reports the project")
    return [r for r in pending if r.pk not in done]


def report_result(runner: Runner, request_id, *, ok: bool, detail: str = "") -> RunnerProjectRequest | None:
    """The box's word on one ask. Only a PENDING ask takes it — one re-armed or
    withdrawn meanwhile is not overwritten by a stale report. A failure marks this
    ask only; it never touches the box's readiness. None: no such ask here."""
    row = RunnerProjectRequest.objects.filter(runner=runner, pk=request_id).first()
    if row is None:
        return None
    if row.status != RunnerProjectRequest.PENDING:
        return row
    # "ok" from the box still has to be true where routing looks: the project in
    # its report. Its next beat discharges it if this one raced the report.
    if ok and row.name not in runner.observed_projects():
        row.detail = ((detail or "").strip() or "added in emdash")[:DETAIL_MAX]
        row.save(update_fields=["detail"])
        return row
    row.status = RunnerProjectRequest.ADDED if ok else RunnerProjectRequest.FAILED
    row.detail = ((detail or "").strip() or ("added" if ok else "the runner could not add it"))[:DETAIL_MAX]
    row.finished_at = timezone.now()
    row.save(update_fields=["status", "detail", "finished_at"])
    return row
