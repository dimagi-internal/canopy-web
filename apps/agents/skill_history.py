"""An agent's skill history, pulled from its repo with its OWNER's GitHub grant.

WHOSE CREDENTIAL. canopy-web holds no GitHub credential belonging to an agent.
It reads with `Agent.owner`'s canopy-agents App grant — never the viewer's, and
never a runner's shared read-only token. `repo_url` is editable by any workspace
editor and the sync runs with a credential the triggering user does not hold, so
whatever that credential reaches, an editor can aim this page at. The owner's
grant is bounded by the repositories the owner chose on GitHub's installation
screen; the fleet token is not. See
`docs/superpowers/specs/2026-09-18-agent-skill-history-design.md`.

Because of that, `repo_url` is validated to be exactly
`https://github.com/<owner>/<repo>[.git]` before anything else happens —
before the sync claim, before `access_token_for`, before any subprocess. A
non-github.com host, a local path, a `file://`/`ssh://` scheme, embedded
credentials, or a value git would read as another option (leading `-`) are
all rejected there; only a bare GitHub HTTPS URL ever reaches git or the
token.

LINE COUNTS ASSUME A LINEAR (squash-merged) HISTORY. `lines_after` is a
running sum of `git log --numstat`, which omits merge commits' diffs, so a
change that reached the branch only through a merge commit is not counted.

RUNS IN THE REQUEST. A full clone of ACE (the largest agent) is 23 MB / 1.6 s
and the log is 0.2 s, so there is no worker. NOT `--filter=blob:none`: that
makes `--numstat` fetch every blob lazily and did not finish in five minutes.
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from django.db import transaction
from django.utils import timezone

from apps.canopy_sessions.invalidation import mark_dirty
from apps.tokens import github_app

from .models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from .skill_history_parse import LOG_FORMAT, build_groups, parse_log

CREDENTIAL_STATES = ("ok", "no_repo", "no_owner", "owner_not_connected", "repo_not_granted")
# TOTAL budget for the clone plus every read that follows it against the same
# clone — not per command. A slow clone must not hand the log its own fresh
# 60s, or a sync could run past the 90s debounce window and collide with the
# next one for the same agent.
GIT_TIMEOUT_S = 60
MAX_CLONE_BYTES = 200 * 1024 * 1024
DEBOUNCE = dt.timedelta(seconds=90)
STALE_AFTER = dt.timedelta(hours=1)

# Only a bare https://github.com/<owner>/<repo>[.git] path may pass validation.
_REPO_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPO_URL_HELP = "History can only be read from a https://github.com/<owner>/<repo> repository"

# `revision_diff` builds a live GitHub API URL from caller-supplied `sha` and
# `skill` — both are validated against these before anything is sent, so a
# malformed value (e.g. a path-traversal attempt in `skill`) is refused
# without ever reaching `access_token_for` or `requests.get`.
# 7..64: an abbreviated sha up to a full SHA-256 object id.
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# The MCP cap on one diff's patch text (global-constraints.md).
DIFF_CAP_BYTES = 20 * 1024

# Markers in a failed `clone`'s stderr that mean the grant cannot reach this
# repository (missing, private-to-someone-else, or plain wrong) as opposed to
# a transient network problem. "could not resolve host" is deliberately NOT
# here — a DNS hiccup is not a verdict on the grant, so it falls through to
# the generic "ok" (transient) branch below instead.
_REPO_NOT_GRANTED_MARKERS = (
    "not found", "does not exist", "authentication failed", "could not read",
    "does not appear to be a git repository", "403", "404",
)


class SyncError(Exception):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


def resource_uri(agent: Agent) -> str:
    return f"skill-history://{agent.slug}"


def _validate_repo_url(repo_url: str) -> None:
    """Refuse anything but a bare GitHub HTTPS repository URL.

    `repo_url` is attacker-reachable (any workspace editor) and the sync runs
    with a credential that editor does not hold, so whatever host this string
    names is a host the owner's token would be sent to via `_clone`'s
    URL-scoped `http.extraheader`. Rejected here, before git or GitHub is ever
    called: a non-`github.com` host, `http`/`file`/`ssh`/no scheme, a userinfo
    or port, and anything git's own option parser could mistake for a flag
    (a value starting with `-` never even matches `https://...`, so it is
    rejected by the scheme check alone).
    """
    try:
        parsed = urlsplit(repo_url)
        path = parsed.path[: -len(".git")] if parsed.path.endswith(".git") else parsed.path
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and not parsed.username
            and not parsed.password
            and parsed.port is None
            and bool(_REPO_PATH_RE.match(path))
        )
    except ValueError:
        valid = False
    if not valid:
        raise SyncError("repo_not_granted", _REPO_URL_HELP)


def credential_state(agent: Agent) -> str:
    """What stands between this agent and a history, as far as is knowable
    without cloning. `repo_not_granted` is normally only learnt by trying, so
    it comes from the last attempt — except an invalid `repo_url`, which is
    knowable up front, so it is recomputed here rather than read stale off a
    row from before the URL was broken."""
    if not agent.repo_url:
        return "no_repo"
    if agent.owner_id is None:
        return "no_owner"
    try:
        _validate_repo_url(agent.repo_url)
    except SyncError:
        return "repo_not_granted"
    row = SkillHistorySync.objects.filter(agent=agent).first()
    # Only states learnt by TRYING are carried over; no_repo / no_owner are
    # recomputed above, so a fixed repo_url is not reported as still missing.
    if row and row.last_state in ("owner_not_connected", "repo_not_granted"):
        return row.last_state
    return "ok"


def is_stale(agent: Agent) -> bool:
    row = SkillHistorySync.objects.filter(agent=agent).first()
    return row is None or row.synced_at is None or timezone.now() - row.synced_at > STALE_AFTER


def _attempt_due(row: SkillHistorySync | None) -> bool:
    """Stale, AND no attempt (successful or not) within the last STALE_AFTER.

    The second half is the debounce for FAILED attempts: a failure never moves
    `synced_at`, so without it an agent whose owner has not connected GitHub,
    or whose repo is not granted, would refresh the owner's token and clone on
    every page load. An owner who fixes access presses Sync (which forces).
    """
    if row is None:
        return True
    now = timezone.now()
    stale = row.synced_at is None or now - row.synced_at > STALE_AFTER
    attempted_recently = row.last_attempt_at is not None and now - row.last_attempt_at <= STALE_AFTER
    return stale and not attempted_recently


def due_for_auto_sync(agent: Agent) -> bool:
    """Whether reading the page should sync first (see `get_skill_history`)."""
    if credential_state(agent) in ("no_repo", "no_owner"):
        return False
    return _attempt_due(SkillHistorySync.objects.filter(agent=agent).first())


def _github_login(user) -> str:
    conn = getattr(user, "github_connection", None)
    return getattr(conn, "github_login", "") or ""


def _remaining(deadline: float) -> float:
    """Seconds left in the total 60s git budget, or raise if it is spent."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SyncError("ok", "git operations exceeded the 60s total timeout")
    return remaining


def _run_git(args: list[str], *, cwd: str, deadline: float) -> str:
    """A LOCAL command against an already-cloned bare repo — never touches
    the network and never sees a token."""
    timeout = _remaining(deadline)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        res = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise SyncError("ok", f"git {args[0]} timed out") from e
    except OSError as e:
        raise SyncError("ok", f"could not run git {args[0]}: {e}") from e
    if res.returncode != 0:
        err = (res.stderr or "").strip()
        raise SyncError("ok", f"git {args[0]} failed: {err.splitlines()[-1] if err else res.returncode}")
    return res.stdout


def auth_header(token: str) -> str:
    """The `Authorization` header git needs for github.com over HTTPS.

    BASIC, not Bearer, and the difference is not cosmetic: GitHub's REST API
    accepts `Bearer <token>`, but git-over-HTTPS answers it with "invalid
    credentials" and git then falls back to asking for a username — which, with
    `GIT_TERMINAL_PROMPT=0`, surfaces as "could not read Username". That is what
    shipped first, and no test caught it: the sync tests rewrite the GitHub URL
    to a LOCAL repo (`insteadOf`), so nothing in them ever authenticates. This
    function exists to be asserted on directly, because the only other place the
    form is checked is a live clone.

    The username is the conventional `x-access-token` placeholder; GitHub reads
    the credential from the password half.
    """
    return "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _clone(agent: Agent, token: str, dest: str, deadline: float) -> None:
    """The one command that touches the network, and the only one that ever
    sees the token.

    The token rides an environment-scoped, URL-specific git config key
    (`http.https://github.com/.extraheader`) — never argv, never a `-c` flag —
    so it cannot appear in a process list (a `-c http.extraheader=...` flag,
    which the previous version of this code used, DOES show up in one; that
    was the bug) and it is redacted below regardless for any stderr git still
    produces. `protocol.allow=never` + `protocol.https.allow=always` close off
    every other transport `repo_url` could reach even if `_validate_repo_url`
    were ever wrong — belt and suspenders, since validation is the real gate.
    `--` terminates option parsing before the URL and destination, so a
    `repo_url` git might otherwise read as a flag is always a positional arg.
    """
    ref = agent.repo_ref or "main"
    timeout = _remaining(deadline)
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": auth_header(token),
    }
    cmd = [
        "git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
        "clone", "--quiet", "--bare", "--single-branch", "--branch", ref,
        "--", agent.repo_url, dest,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise SyncError("ok", "git clone timed out") from e
    except OSError as e:
        raise SyncError("ok", f"could not run git clone: {e}") from e
    if res.returncode != 0:
        err = (res.stderr or "").replace(token, "***").strip()
        low = err.lower()
        if any(marker in low for marker in _REPO_NOT_GRANTED_MARKERS):
            raise SyncError("repo_not_granted",
                             f"could not clone the repository: {err.splitlines()[-1] if err else 'no detail'}")
        raise SyncError("ok", f"git clone failed: {err.splitlines()[-1] if err else res.returncode}")


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _clone_and_read(agent: Agent, token: str) -> tuple[str, str, dict[str, str], set[str]]:
    """Clone, then return (head_sha, log_text, agent_files, skills_at_head)."""
    deadline = time.monotonic() + GIT_TIMEOUT_S
    with tempfile.TemporaryDirectory(prefix="skill-history-") as tmp:
        dest = str(Path(tmp) / "repo.git")
        _clone(agent, token, dest, deadline)
        if _dir_size(Path(dest)) > MAX_CLONE_BYTES:
            raise SyncError("ok", f"repository is larger than {MAX_CLONE_BYTES // (1024 * 1024)} MB")
        head = _run_git(["rev-parse", "HEAD"], cwd=dest, deadline=deadline).strip()
        log = _run_git(["log", "--reverse", "--no-renames", f"--format={LOG_FORMAT}", "--numstat",
                        "HEAD", "--", "skills/*/SKILL.md"], cwd=dest, deadline=deadline)
        present = {
            line.split("/", 1)[1]
            for line in _run_git(["ls-tree", "-d", "--name-only", "HEAD", "skills/"],
                                 cwd=dest, deadline=deadline).splitlines()
            if "/" in line
        }
        files: dict[str, str] = {}
        listing = _run_git(["ls-tree", "--name-only", "HEAD", "agents/"], cwd=dest, deadline=deadline)
        # splitlines, not split: a filename with a space is one path.
        for path in listing.splitlines():
            if path.endswith(".md"):
                files[path] = _run_git(["show", f"HEAD:{path}"], cwd=dest, deadline=deadline)
        return head, log, files, present


def _claim(agent: Agent) -> SkillHistorySync | None:
    """Take the per-agent sync claim, or return None if another sync holds it.

    Even a forced sync honours an in-flight claim: two overlapping syncs would
    each delete-and-reinsert the commits and collide on the unique constraint.
    Stamps `last_attempt_at` in its own committed transaction, so the attempt
    is recorded however the sync then ends — including an unexpected raise.
    """
    with transaction.atomic():
        row, _ = SkillHistorySync.objects.select_for_update().get_or_create(agent=agent)
        now = timezone.now()
        if row.sync_started_at and now - row.sync_started_at < DEBOUNCE:
            return None
        row.sync_started_at = now
        row.last_attempt_at = now
        row.save(update_fields=["sync_started_at", "last_attempt_at"])
        return row


def _record_failure(row: SkillHistorySync, state: str, message: str) -> SkillHistorySync:
    row.last_state = state
    row.last_error = message
    row.sync_started_at = None
    row.save(update_fields=["last_state", "last_error", "sync_started_at"])
    return row


def sync(agent: Agent, *, force: bool = False) -> SkillHistorySync:
    """Re-read the agent's history from its repo, and return the stored row.

    `force` skips only the "is it due?" check (stale, and not attempted within
    the last hour) — never an in-flight claim, which returns the stored row.
    """
    if not agent.repo_url or agent.owner_id is None:
        row, _ = SkillHistorySync.objects.get_or_create(agent=agent)
        state = "no_repo" if not agent.repo_url else "no_owner"
        return _record_failure(row, state, "")

    try:
        _validate_repo_url(agent.repo_url)
    except SyncError as e:
        # Knowable without a claim, git, or a token — so none of those are
        # touched for a repo_url that could never have worked.
        row, _ = SkillHistorySync.objects.get_or_create(agent=agent)
        return _record_failure(row, e.state, str(e))

    if not force and not _attempt_due(SkillHistorySync.objects.filter(agent=agent).first()):
        return SkillHistorySync.objects.get(agent=agent)

    row = _claim(agent)
    if row is None:
        return SkillHistorySync.objects.get(agent=agent)

    try:
        return _do_sync(agent, row)
    except Exception:
        # Whatever happened, the claim must not outlive this attempt — a
        # wedged claim would hold every future sync off for the full 90s
        # debounce window over an error nobody anticipated and handled below.
        SkillHistorySync.objects.filter(pk=row.pk).update(sync_started_at=None)
        raise


def _do_sync(agent: Agent, row: SkillHistorySync) -> SkillHistorySync:
    try:
        token = github_app.access_token_for(agent.owner)
    except (github_app.GitHubAuthError, github_app.GitHubNotConfigured) as e:
        return _record_failure(row, "owner_not_connected", str(e))
    except requests.RequestException as e:
        # A dropped connection, a timeout, or a 5xx talking to GitHub is not a
        # verdict on this agent's credential or repo — it is transient, so
        # `last_state` stays "ok" and only `last_error` records what happened.
        return _record_failure(row, "ok", f"could not reach GitHub: {e}")

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


def _display_name(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or "").strip() or user.email or ""


def history_payload(agent: Agent, viewer) -> dict:
    """The compact page payload: commits once, revisions as index tuples.

    ~150 KB for ACE. Bodies are deliberately absent — they are what the MCP
    tool is for, and would roughly triple the page's download.

    The viewer fields exist so the page can say WHO must act: the credential
    is the owner's, so only the owner can connect GitHub, and syncing is an
    editor action. The role comes from the one authorizer
    (`wsvc.has_role_at_least`), never a query of its own.
    """
    from apps.workspaces import services as wsvc

    row = SkillHistorySync.objects.filter(agent=agent).first()
    commits = list(SkillHistoryCommit.objects.filter(agent=agent).order_by("committed_at", "id"))
    index = {c.id: i for i, c in enumerate(commits)}
    per_skill: dict[str, list[list[int]]] = {}
    for r in (SkillRevision.objects.filter(commit__agent=agent)
              .order_by("commit__committed_at", "commit_id").values_list("skill", "commit_id", "lines_after", "added", "deleted")):
        per_skill.setdefault(r[0], []).append([index[r[1]], r[2], r[3], r[4]])
    return {
        "agent": agent.slug,
        "repo_url": agent.repo_url,
        "head_sha": row.head_sha if row else "",
        "synced_at": row.synced_at if row else None,
        "synced_with": row.synced_with if row else "",
        "last_error": row.last_error if row else "",
        "credential_state": credential_state(agent),
        "owner_name": _display_name(agent.owner),
        "viewer_is_owner": agent.owner_id is not None and agent.owner_id == getattr(viewer, "pk", None),
        "viewer_can_sync": wsvc.has_role_at_least(viewer, agent.workspace_id,
                                                  wsvc.WorkspaceMembership.EDITOR),
        "install_url": github_app.install_url() if github_app.is_configured() else "",
        "groups": row.groups if row else [],
        "checks": row.checks if row else {},
        "present": row.present if row else [],
        "commits": [{"sha": c.sha, "date": c.committed_at.date().isoformat(), "subject": c.subject} for c in commits],
        "skills": [{"name": n, "revisions": v} for n, v in sorted(per_skill.items())],
    }


def skills_in_group(agent: Agent, group: str) -> list[str]:
    row = SkillHistorySync.objects.filter(agent=agent).first()
    if row is None:
        return []
    for g in row.groups:
        if g["title"] == group:
            checkers = [c for c, checked in row.checks.items() if checked in g["skills"]]
            return list(g["skills"]) + checkers
    return []


#: How many revisions a caller gets when it does not say. A READING budget, not
#: a storage one: ACE's commit bodies average ~1.8 KB, so the old default of 300
#: returned ~640 KB — past what a tool result may carry, and the caller is an
#: assistant with a context window. Measured 2026-09-20: 80 revisions of
#: `idea-to-pdd` came back as 166 KB and were refused outright.
DEFAULT_REVISIONS = 25
#: Hard ceiling a caller can ask for.
MAX_REVISIONS = 300
#: Bodies are summarised at this length in a LIST. The full body is one call
#: away — ask for that one `commit` — so the list stays readable rather than
#: making every question cost every word ever written about the skill.
BODY_PREVIEW = 700
#: A single commit is a deliberate read of that commit: nothing is elided.
BODY_FULL = 4000


def skill_revisions(agent: Agent, *, skill: str | None, group: str | None,
                    since: dt.date | None, until: dt.date | None,
                    limit: int | None = None,
                    commit: str | None = None) -> dict:
    """Revisions newest first, with bodies — the read the assistant reasons over.

    `commit` is a sha or sha prefix (7..64 hex), matched by prefix — the
    History page's commit selection. Raises SyncError on a malformed one.
    """
    if commit is not None and not _SHA_RE.match(commit):
        raise SyncError("ok", f"'{commit}' is not a valid commit sha")
    row = SkillHistorySync.objects.filter(agent=agent).first()
    qs = SkillRevision.objects.filter(commit__agent=agent).select_related("commit")
    if commit:
        qs = qs.filter(commit__sha__startswith=commit.lower())
    if skill:
        qs = qs.filter(skill=skill)
    if group:
        qs = qs.filter(skill__in=skills_in_group(agent, group))
    if since:
        qs = qs.filter(commit__committed_at__date__gte=since)
    if until:
        qs = qs.filter(commit__committed_at__date__lte=until)
    qs = qs.order_by("-commit__committed_at", "-commit_id")
    limit = max(1, min(limit if limit is not None else DEFAULT_REVISIONS, MAX_REVISIONS))
    # A one-commit read is deliberate; a list is a survey. Only the survey elides.
    body_cap = BODY_FULL if commit else BODY_PREVIEW
    rows = list(qs[: limit + 1])
    checks = row.checks if row else {}
    return {
        "agent": agent.slug,
        "skill": skill,
        "group": group,
        "commit": commit,
        "checked_by": sorted(c for c, s in checks.items() if skill and s == skill),
        "checks": checks.get(skill) if skill else None,
        "truncated": len(rows) > limit,
        "revisions": [
            {
                "sha": r.commit.sha,
                "date": r.commit.committed_at.date().isoformat(),
                "skill": r.skill,
                "subject": r.commit.subject,
                "body": r.commit.body[:body_cap],
                "body_truncated": len(r.commit.body) > body_cap,
                "lines_after": r.lines_after,
                "line_change": r.added - r.deleted,
            }
            for r in rows[:limit]
        ],
    }


def _owner_repo(agent: Agent) -> str:
    """`owner/repo`, derived straight from a VALIDATED GitHub URL.

    Deliberately not `definition_key` (which folds in the ref, normalises
    `.git`/scp-style/case, and exists to compare two repo spellings — a wider
    job than this one). `revision_diff` sends the owner's token to whatever
    host this produces, so it re-validates `agent.repo_url` itself rather than
    trusting a caller who parsed it another way; only a bare
    `https://github.com/<owner>/<repo>[.git]` ever reaches here.
    """
    _validate_repo_url(agent.repo_url)
    path = urlsplit(agent.repo_url).path
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path.strip("/")


def revision_diff(agent: Agent, sha: str, skill: str) -> dict:
    """One commit's change to one SKILL.md, fetched live — never stored.

    `sha` and `skill` are validated BEFORE the repo URL, the owner's token, or
    GitHub are ever touched: both are used to build the request URL, so a
    malformed value (e.g. `skill="../../user"`) is refused outright rather
    than reaching `access_token_for` or `requests.get`.
    """
    if not _SHA_RE.match(sha or ""):
        raise SyncError("ok", f"'{sha}' is not a valid commit sha")
    if not _SKILL_NAME_RE.match(skill or ""):
        raise SyncError("ok", f"'{skill}' is not a valid skill name")
    owner_repo = _owner_repo(agent)  # raises SyncError("repo_not_granted", ...) if not GitHub

    token = github_app.access_token_for(agent.owner)
    resp = requests.get(
        f"{github_app.GITHUB_API}/repos/{owner_repo}/commits/{sha}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=github_app.HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SyncError("repo_not_granted", f"GitHub returned {resp.status_code} for commit {sha[:12]}")
    target = f"skills/{skill}/SKILL.md"
    patch = next((f.get("patch") or "" for f in resp.json().get("files", []) if f.get("filename") == target), "")
    raw = patch.encode()
    truncated = len(raw) > DIFF_CAP_BYTES
    if truncated:
        patch = raw[:DIFF_CAP_BYTES].decode(errors="ignore")
    return {"sha": sha, "skill": skill, "patch": patch, "truncated": truncated}
