"""An agent's skill history, pulled from its repo with its OWNER's GitHub grant.

WHOSE CREDENTIAL. canopy-web holds no GitHub credential belonging to an agent.
It reads with `Agent.owner`'s canopy-agents App grant — never the viewer's, and
never a runner's shared read-only token. `repo_url` is editable by any workspace
editor and the sync runs with a credential the triggering user does not hold, so
whatever that credential reaches, an editor can aim this page at. The owner's
grant is bounded by the repositories the owner chose on GitHub's installation
screen; the fleet token is not. See
`docs/superpowers/specs/2026-09-18-agent-skill-history-design.md`.

RUNS IN THE REQUEST. A full clone of ACE (the largest agent) is 23 MB / 1.6 s
and the log is 0.2 s, so there is no worker. NOT `--filter=blob:none`: that
makes `--numstat` fetch every blob lazily and did not finish in five minutes.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import tempfile
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.canopy_sessions.invalidation import mark_dirty
from apps.tokens import github_app

from .models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from .skill_history_parse import LOG_FORMAT, build_groups, parse_log

CREDENTIAL_STATES = ("ok", "no_repo", "no_owner", "owner_not_connected", "repo_not_granted")
GIT_TIMEOUT_S = 60
MAX_CLONE_BYTES = 200 * 1024 * 1024
DEBOUNCE = dt.timedelta(seconds=90)
STALE_AFTER = dt.timedelta(hours=1)


class SyncError(Exception):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def resource_uri(agent: Agent) -> str:
    return f"skill-history://{agent.slug}"


def credential_state(agent: Agent) -> str:
    """What stands between this agent and a history, as far as is knowable
    without cloning. `repo_not_granted` is only learnt by trying, so it comes
    from the last attempt."""
    if not agent.repo_url:
        return "no_repo"
    if agent.owner_id is None:
        return "no_owner"
    row = SkillHistorySync.objects.filter(agent=agent).first()
    # Only states learnt by TRYING are carried over; no_repo / no_owner are
    # recomputed above, so a fixed repo_url is not reported as still missing.
    if row and row.last_state in ("owner_not_connected", "repo_not_granted"):
        return row.last_state
    return "ok"


def is_stale(agent: Agent) -> bool:
    row = SkillHistorySync.objects.filter(agent=agent).first()
    return row is None or row.synced_at is None or timezone.now() - row.synced_at > STALE_AFTER


def _github_login(user) -> str:
    conn = getattr(user, "github_connection", None)
    return getattr(conn, "github_login", "") or ""


def _run_git(args: list[str], *, token: str, cwd: str | None = None) -> str:
    # The token rides a config header, never argv's URL, so it cannot appear in
    # a process list or in git's own error text — and it is scrubbed from any
    # error we record regardless.
    cmd = ["git", "-c", f"http.extraheader=Authorization: Bearer {token}", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=GIT_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as e:
        raise SyncError("ok", f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from e
    if res.returncode != 0:
        err = (res.stderr or "").replace(token, "***").strip()
        low = err.lower()
        if args[0] == "clone" and any(s in low for s in (
            "not found", "does not exist", "authentication failed", "could not read", "does not appear to be a git repository",
            "403", "404", "could not resolve host",
        )):
            raise SyncError("repo_not_granted", f"could not clone {args[-2]}: {err.splitlines()[-1] if err else 'no detail'}")
        raise SyncError("ok", f"git {args[0]} failed: {err.splitlines()[-1] if err else res.returncode}")
    return res.stdout


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _clone_and_read(agent: Agent, token: str) -> tuple[str, str, dict[str, str], set[str]]:
    """Clone, then return (head_sha, log_text, agent_files, skills_at_head)."""
    with tempfile.TemporaryDirectory(prefix="skill-history-") as tmp:
        dest = str(Path(tmp) / "repo.git")
        ref = agent.repo_ref or "main"
        _run_git(["clone", "--quiet", "--bare", "--single-branch", "--branch", ref, agent.repo_url, dest], token=token)
        if _dir_size(Path(dest)) > MAX_CLONE_BYTES:
            raise SyncError("ok", f"repository is larger than {MAX_CLONE_BYTES // (1024 * 1024)} MB")
        head = _run_git(["rev-parse", "HEAD"], token=token, cwd=dest).strip()
        log = _run_git(["log", "--reverse", "--no-renames", f"--format={LOG_FORMAT}", "--numstat",
                        "HEAD", "--", "skills/*/SKILL.md"], token=token, cwd=dest)
        present = {
            line.split("/", 1)[1]
            for line in _run_git(["ls-tree", "-d", "--name-only", "HEAD", "skills/"], token=token, cwd=dest).split()
            if "/" in line
        }
        files: dict[str, str] = {}
        listing = _run_git(["ls-tree", "--name-only", "HEAD", "agents/"], token=token, cwd=dest)
        for path in listing.split():
            if path.endswith(".md"):
                files[path] = _run_git(["show", f"HEAD:{path}"], token=token, cwd=dest)
        return head, log, files, present


def _claim(agent: Agent, force: bool) -> SkillHistorySync | None:
    """Take the per-agent sync claim, or return None if another sync holds it."""
    with transaction.atomic():
        row, _ = SkillHistorySync.objects.select_for_update().get_or_create(agent=agent)
        now = timezone.now()
        if row.sync_started_at and now - row.sync_started_at < DEBOUNCE and not force:
            return None
        row.sync_started_at = now
        row.save(update_fields=["sync_started_at"])
        return row


def _record_failure(row: SkillHistorySync, state: str, message: str) -> SkillHistorySync:
    row.last_state = state
    row.last_error = message
    row.sync_started_at = None
    row.save(update_fields=["last_state", "last_error", "sync_started_at"])
    return row


def sync(agent: Agent, *, force: bool = False) -> SkillHistorySync:
    if not agent.repo_url or agent.owner_id is None:
        row, _ = SkillHistorySync.objects.get_or_create(agent=agent)
        state = "no_repo" if not agent.repo_url else "no_owner"
        return _record_failure(row, state, "")

    row = _claim(agent, force)
    if row is None:
        return SkillHistorySync.objects.get(agent=agent)

    try:
        token = github_app.access_token_for(agent.owner)
    except (github_app.GitHubAuthError, github_app.GitHubNotConfigured) as e:
        return _record_failure(row, "owner_not_connected", str(e))

    try:
        head, log, files, present = _clone_and_read(agent, token)
    except SyncError as e:
        return _record_failure(row, e.state, str(e).replace(token, "***"))

    commits = parse_log(log)
    skill_names = {name for c in commits for name, _, _ in c.files}
    groups, checks = build_groups(files, skill_names)

    with transaction.atomic():
        SkillHistoryCommit.objects.filter(agent=agent).delete()
        running: dict[str, int] = {}
        revisions: list[SkillRevision] = []
        for c in commits:
            obj = SkillHistoryCommit.objects.create(
                agent=agent, sha=c.sha, committed_at=c.committed_at, subject=c.subject, body=c.body,
            )
            for name, added, deleted in c.files:
                running[name] = running.get(name, 0) + added - deleted
                revisions.append(SkillRevision(commit=obj, skill=name, lines_after=running[name],
                                               added=added, deleted=deleted))
        SkillRevision.objects.bulk_create(revisions)
        row.head_sha = head
        row.synced_at = timezone.now()
        row.synced_with = _github_login(agent.owner)
        row.last_state = "ok"
        row.last_error = ""
        row.sync_started_at = None
        row.groups = [g.__dict__ for g in groups]
        row.checks = checks
        row.present = sorted(present)
        row.save()
        mark_dirty(resource_uri(agent))
    return row
