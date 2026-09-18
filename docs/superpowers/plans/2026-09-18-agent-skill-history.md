# Agent Skill History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A History section on every agent's workspace that shows how each of its skills changed (from the agent repo's git history), drillable from all skills → group → skill → commit, with the in-app assistant aware of the current selection.

**Architecture:** canopy-web clones the agent's `repo_url` with its **owner's** GitHub App grant, parses `git log --numstat` over `skills/*/SKILL.md` plus `agents/*.md` frontmatter, and stores a per-agent snapshot (three tables). A REST endpoint serves a compact payload to a new React section; the section declares its selection through the existing page contract (`usePageState` / `usePageAction` / `useResource`), and two new MCP tools let the widget's agent read a skill's history and one commit's diff.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2, PostgreSQL, `git` CLI, PyYAML, FastMCP (in-process), React 19 + Vite + Tailwind 4 + `canopy-ui`, vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-18-agent-skill-history-design.md` — read it first; this plan implements it and argues from it.

## Global Constraints

- **No claims in UI copy.** Every label is a count, a date, or a commit message. Never "learned", "improved", "got better", and no named eras ("build", "harden").
- **Credential:** only `github_app.access_token_for(agent.owner)`. Never the viewer's connection, never `RunnerCredential.github_token`.
- **Token only in `-c http.extraheader=Authorization: Bearer <token>`** — never in a URL, an exception message, a log line or `last_error`.
- **Full clone**, never `--filter=blob:none` (lazy blob fetches make `--numstat` take minutes).
- Bounds: **60 s** total git timeout; refuse a clone larger than **200 MB**; sync debounce window **90 s**; auto-sync when the last success is older than **1 hour**.
- MCP caps: `skill_history` returns at most **300** revisions (`truncated: true` beyond); `skill_revision_diff` truncates the patch to **20 KB**.
- Resource URI: **`skill-history://<agent-slug>`**. Backing tool: **`skill_history`**.
- Page actions: **`selectSkill({skill})`, `showCommit({sha})`, `setTimeline({date})`** — throw on unknown skill/sha/date.
- URL params: **`group`, `skill`, `commit`, `at` (YYYY-MM-DD)**.
- Route: **`/w/:workspace/agents/:slug/history`**, rail label **History**.
- `credential_state` values: **`ok`, `no_repo`, `no_owner`, `owner_not_connected`, `repo_not_granted`**.
- Framework tier: everything backend lives in `apps/agents` / `apps/mcp` (both framework). Never import a product app.
- Frontend uses semantic tokens only (`bg-card`, `border-border`, `text-foreground`, `text-muted-foreground`, `text-primary`, `bg-primary`, `text-info`, …) — no raw palette literals.
- A route docstring is published API documentation — rationale goes in `#` comments.
- After any `schemas.py`/`api.py` change: `cd frontend && npm run gen:api:local` and commit `generated.ts`.
- Commits end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`. Open the PR with `gh pr merge <n> --auto` (no strategy flag).

## File Structure

**Backend**
- `apps/agents/skill_history_parse.py` — pure functions: parse `git log` output, parse agent-file frontmatter, build groups + checking-skill pairings. No Django, no subprocess.
- `apps/agents/skill_history.py` — the sync service: credential resolution, clone, run git, persist snapshot, debounce, error classification, `mark_dirty`; plus the read helpers the API and MCP share (`history_payload`, `skill_revisions`, `revision_diff`).
- `apps/agents/models.py` — add `SkillHistorySync`, `SkillHistoryCommit`, `SkillRevision`.
- `apps/agents/migrations/0023_skill_history.py` — generated.
- `apps/agents/schemas.py` — `SkillHistoryOut` and friends.
- `apps/agents/api.py` — `GET/POST /{slug}/skill-history/…`.
- `apps/mcp/tools/skill_history.py` + register in `apps/mcp/tools/__init__.py`.
- `Dockerfile` — add `git`. `pyproject.toml` — declare `pyyaml` directly.

**Frontend**
- `frontend/src/api/agents.ts` — `getSkillHistory`, `syncSkillHistory`, types.
- `frontend/src/pages/agents/skillHistory/model.ts` — pure derivations (timeline arrays, tiles, panel view models). All logic the UI needs, testable without React.
- `frontend/src/pages/agents/skillHistory/SkillHistoryChart.tsx`
- `frontend/src/pages/agents/skillHistory/SkillHistoryLanes.tsx`
- `frontend/src/pages/agents/skillHistory/SkillHistoryPanel.tsx`
- `frontend/src/pages/agents/AgentHistorySection.tsx` — container: fetch/sync, URL state, timeline playback, page contract.
- `frontend/src/router.tsx`, `frontend/src/components/agents/AgentLeftNav.tsx`, `frontend/src/widget/pageContext.ts` — wiring.

**Tests**
- `tests/test_skill_history_parse.py`, `tests/test_skill_history_sync.py`, `tests/test_skill_history_api.py`
- `apps/mcp/tests/test_skill_history_tools.py`
- `frontend/src/pages/agents/skillHistory/model.test.ts`
- `frontend/src/pages/agents/AgentHistorySection.test.tsx`

---

### Task 1: Parse git history and agent files (pure)

**Files:**
- Create: `apps/agents/skill_history_parse.py`
- Modify: `pyproject.toml` (declare `pyyaml`)
- Test: `tests/test_skill_history_parse.py`

**Interfaces:**
- Produces:
  - `LOG_FORMAT: str` — the `--format=` argument the sync passes to `git log`.
  - `@dataclass ParsedCommit(sha: str, committed_at: datetime, subject: str, body: str, files: list[tuple[str, int, int]])` — `files` is `(skill_name, added, deleted)`.
  - `parse_log(text: str) -> list[ParsedCommit]` — oldest first.
  - `@dataclass Group(title: str, kind: str, num: str, skills: list[str])` — `kind` ∈ `"phase" | "agent" | "none"`.
  - `build_groups(agent_files: dict[str, str], skill_names: set[str]) -> tuple[list[Group], dict[str, str]]` — returns groups and `checks: {checking_skill: checked_skill}`.

- [ ] **Step 1: Declare PyYAML**

It is currently only transitive. Run: `uv add pyyaml`
Expected: `pyproject.toml` gains `pyyaml` under `dependencies`; `uv.lock` updated.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_skill_history_parse.py
"""The parse half of skill history: git's output format and agent frontmatter.

The log is produced by REAL git in a throwaway repo rather than a hand-written
string, because git's output format is the contract — a fixture typed by hand
tests the author's idea of the format, not git's.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from apps.agents.skill_history_parse import LOG_FORMAT, build_groups, parse_log


def _git(repo: Path, *args: str, env_date: str | None = None) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if env_date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = env_date
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


def _write(repo: Path, rel: str, lines: int) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"line {i}\n" for i in range(lines)))


def _log(repo: Path) -> str:
    return _git(repo, "log", "--reverse", "--no-renames", f"--format={LOG_FORMAT}",
                "--numstat", "--", "skills/*/SKILL.md")


def test_parse_log_reads_creation_edits_body_and_removal(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "skills/alpha/SKILL.md", 10)
    _write(repo, "README.md", 3)  # not a skill: must not appear
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add alpha", env_date="2026-04-01T10:00:00+00:00")
    _write(repo, "skills/alpha/SKILL.md", 14)
    _write(repo, "skills/beta/SKILL.md", 5)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix(alpha): grow it\n\nBecause run 20260401-0900 showed a gap.",
         env_date="2026-04-02T10:00:00+00:00")
    _git(repo, "rm", "-q", "-r", "skills/beta")
    _git(repo, "commit", "-q", "-m", "chore: retire beta", env_date="2026-04-03T10:00:00+00:00")

    commits = parse_log(_log(repo))

    assert [c.subject for c in commits] == ["feat: add alpha", "fix(alpha): grow it", "chore: retire beta"]
    assert commits[0].files == [("alpha", 10, 0)]
    assert sorted(commits[1].files) == [("alpha", 4, 0), ("beta", 5, 0)]
    assert commits[1].body == "Because run 20260401-0900 showed a gap."
    assert commits[2].files == [("beta", 0, 5)]
    assert commits[0].committed_at.isoformat() == "2026-04-01T10:00:00+00:00"
    assert len(commits[0].sha) == 40


def test_parse_log_of_nothing_is_empty():
    assert parse_log("") == []


ACE_LIKE = {
    "agents/idea-to-design.md": """---
name: idea-to-design
phase_ordinal: 1
phase_display: Idea to Design
skills:
  - { name: idea-to-pdd, has_judge: true, qa_skill: idea-to-pdd-qa, eval_skill: idea-to-pdd-eval }
---
body
""",
    "agents/targeting-analyst.md": """---
name: targeting-analyst
skills:
  - { name: target-geographies, has_judge: false }
---
""",
    "agents/orchestrator.md": "---\nname: orchestrator\n---\nno skills list\n",
    "agents/broken.md": "---\nname: [unclosed\n---\n",
}


def test_build_groups_orders_phases_then_agents_then_unassigned():
    skills = {"idea-to-pdd", "idea-to-pdd-qa", "idea-to-pdd-eval", "target-geographies",
              "shipping", "cycle-grade", "cycle-grade-eval"}

    groups, checks = build_groups(ACE_LIKE, skills)

    assert [(g.title, g.kind, g.num) for g in groups] == [
        ("Idea to Design", "phase", "01"),
        ("Targeting Analyst", "agent", ""),
        ("Not assigned to an agent", "none", ""),
    ]
    assert groups[0].skills == ["idea-to-pdd"]
    assert groups[2].skills == ["cycle-grade", "shipping"]
    # frontmatter pairing, and the -eval suffix convention for an unlisted pair
    assert checks == {"idea-to-pdd-qa": "idea-to-pdd", "idea-to-pdd-eval": "idea-to-pdd",
                      "cycle-grade-eval": "cycle-grade"}


def test_build_groups_without_agent_files_is_one_flat_group():
    groups, checks = build_groups({}, {"b", "a"})
    assert [(g.title, g.skills) for g in groups] == [("Not assigned to an agent", ["a", "b"])]
    assert checks == {}


def test_a_listed_skill_missing_from_the_repo_is_dropped():
    groups, _ = build_groups(ACE_LIKE, {"target-geographies"})
    assert [(g.title, g.skills) for g in groups] == [("Targeting Analyst", ["target-geographies"])]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_skill_history_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.agents.skill_history_parse'`

- [ ] **Step 4: Implement**

```python
# apps/agents/skill_history_parse.py
"""Parse an agent repo's history into skill revisions — pure, no I/O.

Kept apart from the sync so the format contract is testable against real git
without Django, a database, or a network, and so the sync reads as the
sequence of side effects it is.

Grouping comes from the agent repo's own `agents/*.md` frontmatter: a file with
a `skills:` list is a group; `phase_ordinal` / `phase_display` make it a phase.
That is the canopy agent-plugin convention (ACE uses it), not canopy's
invention — an agent without those files gets one flat group, which is correct,
just flat.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import yaml

# Record separator before each commit, unit separators between header fields,
# group separator after the body. Control characters, because a commit subject
# or body can contain any printable character — including the `|` the
# prototype split on.
LOG_FORMAT = "%x1e%H%x1f%cI%x1f%s%x1f%b%x1d"

_SKILL_PATH = re.compile(r"^skills/([^/]+)/SKILL\.md$")
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---", re.S)
UNASSIGNED = "Not assigned to an agent"


@dataclass
class ParsedCommit:
    sha: str
    committed_at: dt.datetime
    subject: str
    body: str
    files: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass
class Group:
    title: str
    kind: str  # "phase" | "agent" | "none"
    num: str
    skills: list[str]


def parse_log(text: str) -> list[ParsedCommit]:
    """`git log --reverse --no-renames --format=LOG_FORMAT --numstat` → commits, oldest first."""
    out: list[ParsedCommit] = []
    for record in text.split("\x1e"):
        if not record.strip():
            continue
        header, _, numstat = record.partition("\x1d")
        sha, date, subject, body = (header.split("\x1f") + ["", "", "", ""])[:4]
        commit = ParsedCommit(
            sha=sha.strip(),
            committed_at=dt.datetime.fromisoformat(date.strip()),
            subject=subject.strip(),
            body=body.strip(),
        )
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            m = _SKILL_PATH.match(path)
            if not m or added == "-":  # "-" = binary; a SKILL.md never is
                continue
            commit.files.append((m.group(1), int(added), int(deleted)))
        if commit.files:
            out.append(commit)
    return out


def _frontmatter(text: str) -> dict:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        # One malformed agent file must not blank the whole page — it just
        # contributes no group, and its skills land in "not assigned".
        return {}
    return data if isinstance(data, dict) else {}


def build_groups(agent_files: dict[str, str], skill_names: set[str]) -> tuple[list[Group], dict[str, str]]:
    """Groups in display order (phases by ordinal, then other agents by title,
    then everything unclaimed), and `{checking_skill: checked_skill}`."""
    phases: list[tuple[int, Group]] = []
    agents: list[Group] = []
    checks: dict[str, str] = {}
    placed: set[str] = set()

    for path in sorted(agent_files):
        fm = _frontmatter(agent_files[path])
        entries = fm.get("skills")
        if not isinstance(entries, list):
            continue
        names: list[str] = []
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if not isinstance(name, str) or name not in skill_names:
                continue
            names.append(name)
            if isinstance(entry, dict):
                for key in ("qa_skill", "eval_skill"):
                    checker = entry.get(key)
                    if isinstance(checker, str) and checker in skill_names:
                        checks[checker] = name
        if not names:
            continue
        placed.update(names)
        ordinal = fm.get("phase_ordinal")
        slug = str(fm.get("name") or path.rsplit("/", 1)[-1].removesuffix(".md"))
        title = str(fm.get("phase_display") or slug.replace("-", " ").title())
        if isinstance(ordinal, int):
            phases.append((ordinal, Group(title, "phase", f"{ordinal:02d}", names)))
        else:
            agents.append(Group(title, "agent", "", names))

    for name in skill_names:
        for suffix in ("-qa", "-eval"):
            base = name.removesuffix(suffix)
            if name.endswith(suffix) and base in skill_names and name not in checks:
                checks[name] = base

    rest = sorted(n for n in skill_names if n not in placed and n not in checks)
    groups = [g for _, g in sorted(phases, key=lambda p: p[0])] + sorted(agents, key=lambda g: g.title)
    if rest:
        groups.append(Group(UNASSIGNED, "none", "", rest))
    return groups, checks
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_skill_history_parse.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/agents/skill_history_parse.py tests/test_skill_history_parse.py pyproject.toml uv.lock
git commit -m "feat(agents): parse an agent repo's skill history from git and agent frontmatter

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Models, migration and the sync service

**Files:**
- Modify: `apps/agents/models.py` (append three models)
- Create: `apps/agents/migrations/0023_skill_history.py` (via `makemigrations`)
- Create: `apps/agents/skill_history.py`
- Modify: `Dockerfile:29-33` (add `git`)
- Test: `tests/test_skill_history_sync.py`

**Interfaces:**
- Consumes: `LOG_FORMAT`, `parse_log`, `build_groups`, `UNASSIGNED` from Task 1; `apps.tokens.github_app.access_token_for`, `GitHubAuthError`, `GitHubNotConfigured`; `apps.canopy_sessions.invalidation.mark_dirty`.
- Produces:
  - Models `SkillHistorySync(agent 1:1, head_sha, synced_at, synced_with, last_error, sync_started_at, groups: JSON, checks: JSON, present: JSON)`, `SkillHistoryCommit(agent FK, sha, committed_at, subject, body)`, `SkillRevision(commit FK, skill, lines_after, added, deleted)`.
  - `resource_uri(agent) -> str`
  - `credential_state(agent) -> str` (one of the five states; `repo_not_granted` is only known after a failed clone and is read from the sync row).
  - `sync(agent, *, force: bool = False) -> SkillHistorySync` — never raises for GitHub/git failures; records them in `last_error` and `credential_state` on the row.
  - `is_stale(agent) -> bool` — no successful sync in the last hour.
  - `class SyncError(Exception)` with attribute `state: str`.

- [ ] **Step 1: Add the models**

Append to `apps/agents/models.py`:

```python
class SkillHistorySync(models.Model):
    """The last pull of this agent's skill history from its repo.

    A CACHE of the repo, replaced wholesale on each sync — the same stance as
    `AgentSkill`. `groups` / `checks` / `present` are a HEAD-only snapshot read
    whole and never queried by field, so they are JSON rather than tables.

    `synced_with` is the GitHub login whose grant produced this history — the
    agent owner's, never the viewer's (see the spec's "Whose credential").
    """

    agent = models.OneToOneField(Agent, on_delete=models.CASCADE, related_name="skill_history_sync")
    head_sha = models.CharField(max_length=40, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    synced_with = models.CharField(max_length=100, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    # One of skill_history.CREDENTIAL_STATES — what the last attempt found.
    last_state = models.CharField(max_length=32, blank=True, default="")
    sync_started_at = models.DateTimeField(null=True, blank=True)
    groups = models.JSONField(default=list, blank=True)
    checks = models.JSONField(default=dict, blank=True)
    present = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"skill-history:{self.agent.slug}@{self.head_sha[:8]}"


class SkillHistoryCommit(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="skill_history_commits")
    sha = models.CharField(max_length=40)
    committed_at = models.DateTimeField()
    subject = models.TextField()
    body = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["committed_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "sha"], name="uniq_agent_skill_history_sha"),
        ]


class SkillRevision(models.Model):
    commit = models.ForeignKey(SkillHistoryCommit, on_delete=models.CASCADE, related_name="revisions")
    skill = models.CharField(max_length=120)
    lines_after = models.IntegerField()
    added = models.IntegerField()
    deleted = models.IntegerField()

    class Meta:
        indexes = [models.Index(fields=["skill"])]
```

- [ ] **Step 2: Generate the migration**

Run: `uv run python manage.py makemigrations agents --name skill_history`
Expected: `apps/agents/migrations/0023_skill_history.py` created with the three models.

- [ ] **Step 3: Add git to the image**

In `Dockerfile`, change the runtime stage's first `apt-get install` line from `curl ca-certificates \` to `curl ca-certificates git \`.

- [ ] **Step 4: Write the failing tests**

```python
# tests/test_skill_history_sync.py
"""Syncing an agent's skill history from its repo.

The repo is a real local git repository and `repo_url` points at it, so the
clone, the log and the frontmatter read are the real commands. Only the GitHub
credential is faked — at `access_token_for`, the one seam the spec names.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents import skill_history
from apps.agents.models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from apps.tokens.github_app import GitHubAuthError
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()
TOKEN = "ghu_SECRETTOKENVALUE"


def _git(repo: Path, *args: str, date: str = "2026-04-01T10:00:00+00:00") -> None:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "agent-repo"
    (r / "skills/alpha").mkdir(parents=True)
    (r / "agents").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(r))
    (r / "skills/alpha/SKILL.md").write_text("a\n" * 10)
    (r / "agents/one.md").write_text("---\nname: one\nphase_ordinal: 1\nphase_display: One\n"
                                     "skills:\n  - { name: alpha, eval_skill: alpha-eval }\n---\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "feat: alpha")
    (r / "skills/alpha-eval").mkdir()
    (r / "skills/alpha-eval/SKILL.md").write_text("e\n" * 4)
    (r / "skills/alpha/SKILL.md").write_text("a\n" * 12)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "fix(alpha): two more lines", date="2026-04-05T10:00:00+00:00")
    return r


@pytest.fixture
def owner():
    return User.objects.create_user(username="own", email="own@dimagi.com")


@pytest.fixture
def agent(owner, repo):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                                repo_url=str(repo), repo_ref="main")


@pytest.fixture
def granted():
    with mock.patch.object(skill_history.github_app, "access_token_for", return_value=TOKEN) as m, \
         mock.patch.object(skill_history, "_github_login", return_value="own-gh"):
        yield m


def test_sync_stores_commits_revisions_groups_and_checks(agent, granted):
    row = skill_history.sync(agent)

    assert row.last_state == "ok" and row.last_error == ""
    assert row.synced_with == "own-gh"
    assert len(row.head_sha) == 40
    assert [c.subject for c in SkillHistoryCommit.objects.filter(agent=agent)] == ["feat: alpha", "fix(alpha): two more lines"]
    alpha = list(SkillRevision.objects.filter(commit__agent=agent, skill="alpha").order_by("commit__committed_at"))
    assert [(r.lines_after, r.added, r.deleted) for r in alpha] == [(10, 10, 0), (12, 2, 0)]
    assert row.groups == [{"title": "One", "kind": "phase", "num": "01", "skills": ["alpha"]}]
    assert row.checks == {"alpha-eval": "alpha"}
    assert sorted(row.present) == ["alpha", "alpha-eval"]


def test_the_credential_is_the_owners_never_the_viewers(agent, granted):
    skill_history.sync(agent)
    granted.assert_called_once_with(agent.owner)


def test_resync_replaces_wholesale(agent, granted):
    skill_history.sync(agent)
    skill_history.sync(agent, force=True)
    assert SkillHistoryCommit.objects.filter(agent=agent).count() == 2


def test_a_sync_in_flight_is_not_started_twice(agent, granted):
    SkillHistorySync.objects.create(agent=agent, sync_started_at=timezone.now())
    with mock.patch.object(skill_history, "_clone_and_read") as clone:
        skill_history.sync(agent)
    clone.assert_not_called()


def test_a_stale_claim_older_than_the_window_is_taken_over(agent, granted):
    SkillHistorySync.objects.create(agent=agent, sync_started_at=timezone.now() - timezone.timedelta(seconds=120))
    row = skill_history.sync(agent)
    assert row.last_state == "ok"


@pytest.mark.parametrize("setup,state", [
    (lambda a: setattr(a, "repo_url", ""), "no_repo"),
    (lambda a: setattr(a, "owner", None), "no_owner"),
])
def test_states_known_before_cloning(agent, setup, state):
    setup(agent)
    agent.save()
    row = skill_history.sync(agent)
    assert row.last_state == state
    assert skill_history.credential_state(agent) == state


def test_owner_not_connected(agent):
    with mock.patch.object(skill_history.github_app, "access_token_for", side_effect=GitHubAuthError("no GitHub connection")):
        row = skill_history.sync(agent)
    assert row.last_state == "owner_not_connected"


def test_a_repo_the_grant_cannot_reach_is_repo_not_granted_and_keeps_the_old_history(agent, granted):
    skill_history.sync(agent)
    agent.repo_url = "/nonexistent/repo"
    agent.save()
    row = skill_history.sync(agent, force=True)
    assert row.last_state == "repo_not_granted"
    assert SkillHistoryCommit.objects.filter(agent=agent).count() == 2  # previous snapshot intact


def test_the_token_never_reaches_the_stored_error(agent, granted):
    agent.repo_url = "https://github.invalid/org/repo"
    agent.save()
    row = skill_history.sync(agent, force=True)
    assert TOKEN not in row.last_error
    assert row.last_error  # something human-readable was recorded


def test_sync_marks_the_resource_dirty(agent, granted):
    with mock.patch.object(skill_history, "mark_dirty") as dirty:
        skill_history.sync(agent)
    dirty.assert_called_once_with("skill-history://ace")


def test_staleness(agent, granted):
    assert skill_history.is_stale(agent)
    skill_history.sync(agent)
    assert not skill_history.is_stale(agent)
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `uv run pytest tests/test_skill_history_sync.py -v`
Expected: FAIL — `ImportError: cannot import name 'skill_history' from 'apps.agents'`.

- [ ] **Step 6: Implement the sync service**

```python
# apps/agents/skill_history.py
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

import requests
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
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_skill_history_sync.py tests/test_skill_history_parse.py -v`
Expected: all pass. If `test_the_token_never_reaches_the_stored_error` is slow (DNS for `github.invalid`), it is still bounded by `GIT_TIMEOUT_S`; `.invalid` fails fast by RFC 2606.

- [ ] **Step 8: Run the boundary test**

Run: `uv run pytest tests/test_architecture_boundary.py -v`
Expected: PASS (`agents` → `tokens` / `canopy_sessions` are framework → framework).

- [ ] **Step 9: Commit**

```bash
git add apps/agents/models.py apps/agents/migrations/0023_skill_history.py apps/agents/skill_history.py Dockerfile tests/test_skill_history_sync.py
git commit -m "feat(agents): sync an agent's skill history from its repo with the owner's GitHub grant

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Read helpers and the REST API

**Files:**
- Modify: `apps/agents/skill_history.py` (append read helpers)
- Modify: `apps/agents/schemas.py` (append schemas)
- Modify: `apps/agents/api.py` (two routes after the skill catalog routes)
- Modify: `frontend/src/api/generated.ts` (regenerated)
- Test: `tests/test_skill_history_api.py`

**Interfaces:**
- Consumes: Task 2's models, `sync`, `is_stale`, `credential_state`.
- Produces:
  - `history_payload(agent) -> dict` matching `SkillHistoryOut`.
  - `skill_revisions(agent, skill: str | None, group: str | None, since: date | None, until: date | None, limit: int = 300) -> dict` (for MCP, Task 4).
  - `GET /api/agents/{slug}/skill-history/` → `SkillHistoryOut`; `POST /api/agents/{slug}/skill-history/sync` → `SkillHistoryOut`.
  - `SkillHistoryOut` fields: `agent: str`, `repo_url: str`, `head_sha: str`, `synced_at: datetime | None`, `synced_with: str`, `last_error: str`, `credential_state: str`, `groups: list[SkillHistoryGroupOut]`, `checks: dict[str, str]`, `present: list[str]`, `commits: list[SkillHistoryCommitOut]`, `skills: list[SkillHistorySkillOut]`.
  - `SkillHistoryGroupOut(title, kind, num, skills: list[str])`; `SkillHistoryCommitOut(sha, date: str 'YYYY-MM-DD', subject)`; `SkillHistorySkillOut(name, revisions: list[list[int]])` where each revision is `[commit_index, lines_after, added, deleted]` and `commit_index` indexes `commits`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_skill_history_api.py
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents import skill_history
from apps.agents.models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def ws_and_users():
    owner = User.objects.create_user(username="o", email="o@dimagi.com")
    viewer = User.objects.create_user(username="v", email="v@dimagi.com")
    outsider = User.objects.create_user(username="x", email="x@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    return ws, owner, viewer, outsider


@pytest.fixture
def agent(ws_and_users):
    ws, owner, _, _ = ws_and_users
    a = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, repo_url="https://github.com/o/ace")
    c1 = SkillHistoryCommit.objects.create(agent=a, sha="a" * 40, subject="feat: alpha",
                                           committed_at=timezone.datetime(2026, 4, 1, tzinfo=timezone.utc))
    c2 = SkillHistoryCommit.objects.create(agent=a, sha="b" * 40, subject="fix: alpha",
                                           committed_at=timezone.datetime(2026, 4, 5, tzinfo=timezone.utc))
    SkillRevision.objects.create(commit=c1, skill="alpha", lines_after=10, added=10, deleted=0)
    SkillRevision.objects.create(commit=c2, skill="alpha", lines_after=12, added=2, deleted=0)
    SkillHistorySync.objects.create(agent=a, head_sha="b" * 40, synced_at=timezone.now(), synced_with="o-gh",
                                    last_state="ok", groups=[{"title": "One", "kind": "phase", "num": "01", "skills": ["alpha"]}],
                                    checks={}, present=["alpha"])
    return a


def test_a_member_reads_the_compact_payload(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    client.force_login(viewer)
    r = client.get("/api/agents/ace/skill-history/")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["credential_state"] == "ok"
    assert body["synced_with"] == "o-gh"
    assert body["commits"] == [
        {"sha": "a" * 40, "date": "2026-04-01", "subject": "feat: alpha"},
        {"sha": "b" * 40, "date": "2026-04-05", "subject": "fix: alpha"},
    ]
    assert body["skills"] == [{"name": "alpha", "revisions": [[0, 10, 10, 0], [1, 12, 2, 0]]}]
    assert body["groups"][0]["title"] == "One"


def test_reading_a_fresh_history_does_not_sync(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    client.force_login(viewer)
    with mock.patch.object(skill_history, "sync") as s:
        client.get("/api/agents/ace/skill-history/")
    s.assert_not_called()


def test_reading_a_stale_history_syncs_first(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    SkillHistorySync.objects.filter(agent=agent).update(synced_at=timezone.now() - timezone.timedelta(hours=2))
    client.force_login(viewer)
    with mock.patch.object(skill_history, "sync") as s:
        client.get("/api/agents/ace/skill-history/")
    s.assert_called_once()


def test_a_non_member_gets_404(client, ws_and_users, agent):
    _, _, _, outsider = ws_and_users
    client.force_login(outsider)
    assert client.get("/api/agents/ace/skill-history/").status_code == 404


def test_a_viewer_cannot_force_a_sync(client, ws_and_users, agent):
    _, _, viewer, _ = ws_and_users
    client.force_login(viewer)
    assert client.post("/api/agents/ace/skill-history/sync").status_code == 403


def test_an_owner_can_force_a_sync(client, ws_and_users, agent):
    _, owner, _, _ = ws_and_users
    client.force_login(owner)
    with mock.patch.object(skill_history, "sync") as s:
        r = client.post("/api/agents/ace/skill-history/sync")
    assert r.status_code == 200
    s.assert_called_once_with(agent, force=True)


def test_skill_revisions_filters_and_caps(agent):
    out = skill_history.skill_revisions(agent, skill="alpha", group=None, since=None, until=None, limit=1)
    assert out["truncated"] is True
    assert out["revisions"][0]["subject"] == "fix: alpha"  # newest first
    assert out["revisions"][0]["line_change"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_skill_history_api.py -v`
Expected: FAIL — 404 on the route / `AttributeError: skill_revisions`.

- [ ] **Step 3: Append read helpers to `apps/agents/skill_history.py`**

```python
def history_payload(agent: Agent) -> dict:
    """The compact page payload: commits once, revisions as index tuples.

    ~150 KB for ACE. Bodies are deliberately absent — they are what the MCP
    tool is for, and would roughly triple the page's download.
    """
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


def skill_revisions(agent: Agent, *, skill: str | None, group: str | None,
                    since: dt.date | None, until: dt.date | None, limit: int = 300) -> dict:
    """Revisions newest first, with bodies — the read the assistant reasons over."""
    row = SkillHistorySync.objects.filter(agent=agent).first()
    qs = SkillRevision.objects.filter(commit__agent=agent).select_related("commit")
    if skill:
        qs = qs.filter(skill=skill)
    if group:
        qs = qs.filter(skill__in=skills_in_group(agent, group))
    if since:
        qs = qs.filter(commit__committed_at__date__gte=since)
    if until:
        qs = qs.filter(commit__committed_at__date__lte=until)
    qs = qs.order_by("-commit__committed_at", "-commit_id")
    limit = max(1, min(limit, 300))
    rows = list(qs[: limit + 1])
    checks = row.checks if row else {}
    return {
        "agent": agent.slug,
        "skill": skill,
        "group": group,
        "checked_by": sorted(c for c, s in checks.items() if skill and s == skill),
        "checks": checks.get(skill) if skill else None,
        "truncated": len(rows) > limit,
        "revisions": [
            {
                "sha": r.commit.sha,
                "date": r.commit.committed_at.date().isoformat(),
                "skill": r.skill,
                "subject": r.commit.subject,
                "body": r.commit.body[:4000],
                "lines_after": r.lines_after,
                "line_change": r.added - r.deleted,
            }
            for r in rows[:limit]
        ],
    }
```

- [ ] **Step 4: Append schemas to `apps/agents/schemas.py`**

```python
# ---- skill history (pulled from the agent's repo) ----
class SkillHistoryGroupOut(StrictModel):
    title: str
    kind: Literal["phase", "agent", "none"]
    num: str
    skills: list[str]


class SkillHistoryCommitOut(StrictModel):
    sha: str
    date: str
    subject: str


class SkillHistorySkillOut(StrictModel):
    name: str
    # Each revision is [commit_index, lines_after, added, deleted]; commit_index
    # indexes SkillHistoryOut.commits. Tuples rather than objects because ACE
    # has ~2,300 of them and the page downloads them all.
    revisions: list[list[int]]


class SkillHistoryOut(StrictModel):
    agent: str
    repo_url: str
    head_sha: str
    synced_at: dt.datetime | None
    synced_with: str
    last_error: str
    credential_state: Literal["ok", "no_repo", "no_owner", "owner_not_connected", "repo_not_granted"]
    groups: list[SkillHistoryGroupOut]
    checks: dict[str, str]
    present: list[str]
    commits: list[SkillHistoryCommitOut]
    skills: list[SkillHistorySkillOut]
```

If `Literal` is not already imported in `schemas.py`, add `from typing import Literal`.

- [ ] **Step 5: Add routes to `apps/agents/api.py`** (directly after `replace_skills`; add `SkillHistoryOut` to the schema import list and `from . import skill_history` to the imports)

```python
# ---- skill history ----
# Reading syncs first when the stored history is over an hour old: opening the
# page is the trigger (no scheduler exists, and a stale history costs one
# click). The sync itself is debounced per agent, so many open tabs clone once.
@router.get("/{slug}/skill-history/", response=SkillHistoryOut,
            summary="How the agent's skills changed, from its repository's history")
def get_skill_history(request: HttpRequest, slug: str) -> SkillHistoryOut:
    agent = _get_agent_or_404(request, slug)
    # Retries after a failed attempt too (a failure never sets synced_at), so an
    # owner who connects GitHub sees history on the next page load. Failures are
    # fast — no token, or a clone refused — and the per-agent claim debounces.
    if skill_history.is_stale(agent) and skill_history.credential_state(agent) not in ("no_repo", "no_owner"):
        skill_history.sync(agent)
    return SkillHistoryOut(**skill_history.history_payload(agent))


@router.post("/{slug}/skill-history/sync", response=SkillHistoryOut,
             summary="Re-read the agent's skill history from its repository now")
def sync_skill_history(request: HttpRequest, slug: str) -> SkillHistoryOut:
    agent = _agent_for_write(request, slug)
    skill_history.sync(agent, force=True)
    return SkillHistoryOut(**skill_history.history_payload(agent))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_skill_history_api.py tests/test_skill_history_sync.py -v`
Expected: all pass.

- [ ] **Step 7: Regenerate frontend types**

Run: `cd frontend && npm run gen:api:local`
Expected: `frontend/src/api/generated.ts` gains `SkillHistoryOut`, `SkillHistoryGroupOut`, `SkillHistoryCommitOut`, `SkillHistorySkillOut` and the two paths.

- [ ] **Step 8: Commit**

```bash
git add apps/agents/skill_history.py apps/agents/schemas.py apps/agents/api.py frontend/src/api/generated.ts tests/test_skill_history_api.py
git commit -m "feat(agents): serve an agent's skill history, syncing when over an hour old

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: MCP tools `skill_history` and `skill_revision_diff`

**Files:**
- Modify: `apps/agents/skill_history.py` (append `revision_diff`)
- Create: `apps/mcp/tools/skill_history.py`
- Modify: `apps/mcp/tools/__init__.py`
- Test: `apps/mcp/tests/test_skill_history_tools.py`

**Interfaces:**
- Consumes: `skill_revisions` (Task 3), `github_app.access_token_for`, `github_app.GITHUB_API`, `github_app.HTTP_TIMEOUT`, `apps.workspaces.services.workspace_slugs_for_user_id`, `apps.mcp.audit.current_user_id`, `write_audit`, `apps.mcp.server.mcp`.
- Produces:
  - `revision_diff(agent, sha: str, skill: str) -> dict` → `{"sha", "skill", "patch", "truncated"}`; raises `SyncError` on GitHub failure.
  - MCP tools `skill_history(agent, skill=None, group=None, since=None, until=None, limit=300)` and `skill_revision_diff(agent, sha, skill)`. Both return `{"error": "..."}` for an agent the caller cannot see (same text as a missing one — no existence leak).

- [ ] **Step 1: Write the failing tests**

```python
# apps/mcp/tests/test_skill_history_tools.py
"""`skill_history` / `skill_revision_diff`, driven through the mounted server."""
from __future__ import annotations

import contextlib
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents import skill_history
from apps.agents.models import Agent, SkillHistoryCommit, SkillHistorySync, SkillRevision
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(token="t", client_id=str(user.pk), scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email})
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _call(name, **kw):
    return async_to_sync(mcp.call_tool)(name, kw).structured_content


@pytest.fixture
def world():
    owner = User.objects.create_user(username="o", email="o@dimagi.com")
    outsider = User.objects.create_user(username="x", email="x@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    a = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                             repo_url="https://github.com/dimagi-internal/ace")
    c = SkillHistoryCommit.objects.create(agent=a, sha="c" * 40, subject="fix(idea-to-pdd): read comments",
                                          body="Reviewers were being transcribed by hand.",
                                          committed_at=timezone.datetime(2026, 8, 21, tzinfo=timezone.utc))
    SkillRevision.objects.create(commit=c, skill="idea-to-pdd", lines_after=1333, added=15, deleted=0)
    SkillHistorySync.objects.create(agent=a, last_state="ok", checks={"idea-to-pdd-eval": "idea-to-pdd"})
    return owner, outsider, a


def test_both_tools_are_registered_on_the_mounted_server():
    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"skill_history", "skill_revision_diff"} <= names


def test_skill_history_returns_bodies_and_checking_skills(world):
    owner, _, _ = world
    with as_user(owner):
        out = _call("skill_history", agent="ace", skill="idea-to-pdd")
    assert out["revisions"][0]["body"] == "Reviewers were being transcribed by hand."
    assert out["checked_by"] == ["idea-to-pdd-eval"]


def test_an_outsider_learns_nothing(world):
    _, outsider, _ = world
    with as_user(outsider):
        out = _call("skill_history", agent="ace")
    assert out == {"error": "agent 'ace' not found"}


def test_diff_is_fetched_live_with_the_owners_grant_and_truncated(world):
    owner, _, agent = world
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"files": [
        {"filename": "skills/idea-to-pdd/SKILL.md", "patch": "+x\n" * 20000},
        {"filename": "README.md", "patch": "+y"},
    ]}
    with as_user(owner), \
         mock.patch.object(skill_history.github_app, "access_token_for", return_value="tok") as tok, \
         mock.patch.object(skill_history.requests, "get", return_value=resp) as get:
        out = _call("skill_revision_diff", agent="ace", sha="c" * 40, skill="idea-to-pdd")
    tok.assert_called_once_with(agent.owner)
    assert get.call_args.args[0] == "https://api.github.com/repos/dimagi-internal/ace/commits/" + "c" * 40
    assert out["truncated"] is True
    assert len(out["patch"].encode()) <= 20 * 1024
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest apps/mcp/tests/test_skill_history_tools.py -v`
Expected: FAIL — tools not registered.

- [ ] **Step 3: Append `revision_diff` to `apps/agents/skill_history.py`**

```python
from .definition import definition_key

DIFF_CAP_BYTES = 20 * 1024


def _owner_repo(agent: Agent) -> str:
    """`github.com/org/repo` → `org/repo`, via the one URL normaliser."""
    key = definition_key(agent.repo_url, "")
    host, _, path = key.partition("/")
    if host != "github.com" or path.count("/") != 1:
        raise SyncError("repo_not_granted", f"{agent.repo_url} is not a GitHub repository")
    return path


def revision_diff(agent: Agent, sha: str, skill: str) -> dict:
    """One commit's change to one SKILL.md, fetched live — never stored."""
    token = github_app.access_token_for(agent.owner)
    resp = requests.get(f"{github_app.GITHUB_API}/repos/{_owner_repo(agent)}/commits/{sha}",
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                        timeout=github_app.HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise SyncError("repo_not_granted", f"GitHub returned {resp.status_code} for commit {sha[:12]}")
    target = f"skills/{skill}/SKILL.md"
    patch = next((f.get("patch") or "" for f in resp.json().get("files", []) if f.get("filename") == target), "")
    raw = patch.encode()
    truncated = len(raw) > DIFF_CAP_BYTES
    if truncated:
        patch = raw[:DIFF_CAP_BYTES].decode(errors="ignore")
    return {"sha": sha, "skill": skill, "patch": patch, "truncated": truncated}
```

Move `from .definition import definition_key` up into the module's import block.

- [ ] **Step 4: Create the MCP tool module**

```python
# apps/mcp/tools/skill_history.py
"""An agent's skill history, as MCP tools — the backing tool for the History page.

The page declares `backing_tool: skill_history` with the selected skill, group or
commit; these resolve that selection with the caller's own access applied. The
tenant gate is the one authorizer (`workspace_slugs_for_user_id`), borrowed as
`list_items` borrows it — never a second implementation.
"""
from __future__ import annotations

import datetime as dt

from asgiref.sync import sync_to_async

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp


def _visible_agent(user_id, slug: str):
    from apps.agents.models import Agent
    from apps.workspaces import services as wsvc

    return Agent.objects.filter(slug=slug, workspace_id__in=wsvc.workspace_slugs_for_user_id(user_id)).first()


def _history(user_id, agent, skill, group, since, until, limit):
    from apps.agents import skill_history

    a = _visible_agent(user_id, agent)
    if a is None:
        return {"error": f"agent '{agent}' not found"}
    parse = lambda s: dt.date.fromisoformat(s) if s else None  # noqa: E731
    return skill_history.skill_revisions(a, skill=skill, group=group, since=parse(since), until=parse(until), limit=limit)


def _diff(user_id, agent, sha, skill):
    from apps.agents import skill_history
    from apps.tokens.github_app import GitHubAuthError, GitHubNotConfigured

    a = _visible_agent(user_id, agent)
    if a is None:
        return {"error": f"agent '{agent}' not found"}
    try:
        return skill_history.revision_diff(a, sha, skill)
    except (skill_history.SyncError, GitHubAuthError, GitHubNotConfigured) as e:
        return {"error": str(e)}


@mcp.tool
async def skill_history(
    agent: str,
    skill: str | None = None,
    group: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 300,
) -> dict:
    """How an agent's skills changed, from its repository's git history.

    Returns revisions newest first: date, skill, commit subject AND body (the
    body is where the reason for a change is usually spelled out), lines after,
    and the line change. For a single skill it also names the QA/eval skills
    that check it (`checked_by`) or the skill it checks (`checks`).

    Filters: `skill` name, `group` title (a phase or agent from the History
    page), `since` / `until` as YYYY-MM-DD. `limit` is capped at 300.

    On the History page, read `current_page` first: it says which skill, group
    or commit the user has selected and the date they are looking at.
    """
    user_id = current_user_id()
    out = await sync_to_async(_history, thread_sensitive=True)(user_id, agent, skill, group, since, until, limit)
    await write_audit(user_id=user_id, tool="skill_history",
                      args_summary=f"agent={agent} skill={skill} group={group} -> {len(out.get('revisions', []))}")
    return out


@mcp.tool
async def skill_revision_diff(agent: str, sha: str, skill: str) -> dict:
    """The exact change one commit made to one skill's SKILL.md, as a unified diff.

    Fetched live from GitHub (not stored), truncated to 20 KB. Use it when a
    commit message does not say enough about what actually changed.
    """
    user_id = current_user_id()
    out = await sync_to_async(_diff, thread_sensitive=True)(user_id, agent, sha, skill)
    await write_audit(user_id=user_id, tool="skill_revision_diff", args_summary=f"agent={agent} sha={sha[:12]} skill={skill}")
    return out
```

Register it — in `apps/mcp/tools/__init__.py` add `skill_history,  # noqa: F401` to the import list (alphabetical, after `sessions`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest apps/mcp/tests/test_skill_history_tools.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/agents/skill_history.py apps/mcp/tools/skill_history.py apps/mcp/tools/__init__.py apps/mcp/tests/test_skill_history_tools.py
git commit -m "feat(mcp): skill_history and skill_revision_diff — the History page's backing tools

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Frontend API client and the pure view model

**Files:**
- Modify: `frontend/src/api/agents.ts`
- Create: `frontend/src/pages/agents/skillHistory/model.ts`
- Test: `frontend/src/pages/agents/skillHistory/model.test.ts`

**Interfaces:**
- Consumes: generated `SkillHistoryOut` (Task 3).
- Produces (`model.ts`):
  - `type Selection = { group?: string; skill?: string; commit?: string }`
  - `buildModel(h: SkillHistoryOut): Model` — precomputes `days` (N+1 day axis from first commit date to `synced_at` or last commit), per-skill `{ name, revs: Rev[], firstDay, removedDay: number | null, checks: string | null, checkedBy: string[], group: number | null }`, `commitDay[]`, `commitSkills[]`.
  - `type Rev = { commit: number; day: number; lines: number; added: number; deleted: number }`
  - `dayOf(model, iso: string): number`, `isoOf(model, day: number): string`, `fmtDay(model, day): string` (e.g. `Aug 14`).
  - `totalsAt(model, day) -> { skills, revisions, withChecks, removed }`
  - `tilesAt(model, day, sel) -> GroupRow[]` with `GroupRow = { index, title, kind, num, meta, dimmed, tiles: Tile[] }` and `Tile = { name, state: 'unborn' | 'active' | 'recent' | 'removed', revisions, checkRevisions, lines, selected }`.
  - `weeklyCounts(model, names: string[] | null) -> number[]`, `skillsSeries(model) -> number[]`.
  - `panelAt(model, day, sel) -> Panel` — discriminated union `{kind:'all', week, top}` | `{kind:'group', ...}` | `{kind:'skill', ...}` | `{kind:'commit', ...}` with the exact fields listed in the code below.
  - `groupsOfSelection(model, sel) -> string[]` (skill names in scope for the chart highlight).

- [ ] **Step 1: Add the client functions** to `frontend/src/api/agents.ts`

```ts
export type SkillHistoryOut = Schemas['SkillHistoryOut']

export async function getSkillHistory(slug: string): Promise<SkillHistoryOut> {
  const res = await apiV2.GET('/api/agents/{slug}/skill-history/', { params: { path: { slug } } })
  return unwrap(res, 'getSkillHistory')
}

export async function syncSkillHistory(slug: string): Promise<SkillHistoryOut> {
  const res = await apiV2.POST('/api/agents/{slug}/skill-history/sync', { params: { path: { slug } } })
  return unwrap(res, 'syncSkillHistory')
}
```

- [ ] **Step 2: Write the failing model tests**

```ts
// frontend/src/pages/agents/skillHistory/model.test.ts
import { describe, expect, it } from 'vitest'
import type { SkillHistoryOut } from '@/api/agents'
import { buildModel, dayOf, fmtDay, panelAt, tilesAt, totalsAt, weeklyCounts } from './model'

const H = {
  agent: 'ace', repo_url: 'x', head_sha: 'b', synced_at: '2026-04-20T00:00:00Z', synced_with: 'o',
  last_error: '', credential_state: 'ok',
  groups: [{ title: 'One', kind: 'phase', num: '01', skills: ['alpha'] },
           { title: 'Not assigned to an agent', kind: 'none', num: '', skills: ['gone'] }],
  checks: { 'alpha-eval': 'alpha' },
  present: ['alpha', 'alpha-eval'],
  commits: [
    { sha: 'a1', date: '2026-04-01', subject: 'feat: alpha' },
    { sha: 'b2', date: '2026-04-05', subject: 'fix: alpha and its eval' },
    { sha: 'c3', date: '2026-04-10', subject: 'chore: retire gone' },
  ],
  skills: [
    { name: 'alpha', revisions: [[0, 10, 10, 0], [1, 12, 2, 0]] },
    { name: 'alpha-eval', revisions: [[1, 4, 4, 0]] },
    { name: 'gone', revisions: [[0, 3, 3, 0], [2, 0, 0, 3]] },
  ],
} as unknown as SkillHistoryOut

const m = buildModel(H)

describe('skill history model', () => {
  it('lays days out from the first commit to the sync', () => {
    expect(dayOf(m, '2026-04-01')).toBe(0)
    expect(m.days).toBe(19)
    expect(fmtDay(m, 4)).toBe('Apr 5')
  })

  it('counts what existed on a given day', () => {
    expect(totalsAt(m, 0)).toEqual({ skills: 2, revisions: 2, withChecks: 0, removed: 0 })
    expect(totalsAt(m, 19)).toEqual({ skills: 2, revisions: 5, withChecks: 1, removed: 1 })
  })

  it('gives tiles their state on a day, with checking skills inside the skill they check', () => {
    const rows = tilesAt(m, 4, {})
    const alpha = rows[0].tiles[0]
    expect(alpha).toMatchObject({ name: 'alpha', state: 'recent', revisions: 2, checkRevisions: 1, lines: 12 })
    expect(rows.flatMap((r) => r.tiles.map((t) => t.name))).not.toContain('alpha-eval')
    expect(tilesAt(m, 19, {})[1].tiles[0].state).toBe('removed')
  })

  it('dims every other group when one is selected', () => {
    const rows = tilesAt(m, 19, { group: 'One' })
    expect(rows.map((r) => r.dimmed)).toEqual([false, true])
  })

  it('counts revisions per week for a scope', () => {
    expect(weeklyCounts(m, null)).toEqual([4, 1, 0])
    expect(weeklyCounts(m, ['alpha'])).toEqual([2, 0, 0])
  })

  it('builds the all-skills panel from the 7 days to the date', () => {
    const p = panelAt(m, 8, {})
    expect(p.kind).toBe('all')
    if (p.kind !== 'all') return
    expect(p.week.map((c) => c.subject)).toEqual(['fix: alpha and its eval'])
    expect(p.week[0].skills).toEqual(['alpha', 'alpha-eval'])
    expect(p.top[0]).toMatchObject({ name: 'alpha', revisions: 2 })
  })

  it('builds the skill panel, fading later revisions', () => {
    const p = panelAt(m, 2, { skill: 'alpha' })
    expect(p.kind).toBe('skill')
    if (p.kind !== 'skill') return
    expect(p.revisions).toBe(1)
    expect(p.checkedBy).toEqual(['alpha-eval'])
    expect(p.list.map((r) => [r.subject, r.later, r.change])).toEqual([
      ['fix: alpha and its eval', true, '+2'],
      ['feat: alpha', false, 'created'],
    ])
  })

  it('builds the commit panel with each skill it changed', () => {
    const p = panelAt(m, 19, { commit: 'c3' })
    expect(p.kind).toBe('commit')
    if (p.kind !== 'commit') return
    expect(p.rows).toEqual([{ name: 'gone', change: 'removed', lines: 0 }])
  })
})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/pages/agents/skillHistory/model.test.ts`
Expected: FAIL — cannot resolve `./model`.

- [ ] **Step 4: Implement `model.ts`**

```ts
// frontend/src/pages/agents/skillHistory/model.ts
/**
 * Everything the History page derives from the payload, with no React in it.
 *
 * Ported from the approved prototype (docs/superpowers/specs/
 * 2026-09-18-agent-skill-history-design.md). The page shows counts, dates and
 * commit messages only — nothing here produces a characterisation.
 */
import type { SkillHistoryOut } from '@/api/agents'

export type Selection = { group?: string; skill?: string; commit?: string }
export type Rev = { commit: number; day: number; lines: number; added: number; deleted: number }
export type SkillInfo = {
  name: string
  revs: Rev[]
  firstDay: number
  removedDay: number | null
  checks: string | null
  checkedBy: string[]
  group: number | null
}
export type Model = {
  h: SkillHistoryOut
  start: number // epoch ms of day 0 (UTC)
  days: number // index of the last day
  commitDay: number[]
  commitSkills: string[][]
  skills: Map<string, SkillInfo>
  bySha: Map<string, number>
}

const DAY = 86_400_000
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const utc = (iso: string) => Date.parse(iso.slice(0, 10) + 'T00:00:00Z')

export function buildModel(h: SkillHistoryOut): Model {
  const start = h.commits.length ? utc(h.commits[0].date) : Date.now()
  const endIso = h.synced_at ?? h.commits.at(-1)?.date ?? new Date().toISOString()
  const days = Math.max(0, Math.round((utc(endIso) - start) / DAY))
  const commitDay = h.commits.map((c) => Math.round((utc(c.date) - start) / DAY))
  const commitSkills: string[][] = h.commits.map(() => [])
  const present = new Set(h.present)
  const groupOf = new Map<string, number>()
  h.groups.forEach((g, i) => g.skills.forEach((s) => groupOf.set(s, i)))
  const skills = new Map<string, SkillInfo>()
  for (const s of h.skills) {
    const revs = s.revisions.map(([commit, lines, added, deleted]) => ({ commit, day: commitDay[commit], lines, added, deleted }))
    revs.forEach((r) => commitSkills[r.commit].push(s.name))
    const last = revs.at(-1)
    const checks = h.checks[s.name] ?? null
    skills.set(s.name, {
      name: s.name,
      revs,
      firstDay: revs[0]?.day ?? Infinity,
      removedDay: !present.has(s.name) && last ? last.day : null,
      checks,
      checkedBy: Object.entries(h.checks).filter(([, v]) => v === s.name).map(([k]) => k).sort(),
      group: groupOf.get(s.name) ?? (checks ? groupOf.get(checks) ?? null : null),
    })
  }
  commitSkills.forEach((l) => l.sort())
  return { h, start, days, commitDay, commitSkills, skills, bySha: new Map(h.commits.map((c, i) => [c.sha, i])) }
}

export const dayOf = (m: Model, iso: string) => Math.round((utc(iso) - m.start) / DAY)
export const isoOf = (m: Model, day: number) => new Date(m.start + day * DAY).toISOString().slice(0, 10)
export const fmtDay = (m: Model, day: number) => {
  const d = new Date(m.start + day * DAY)
  return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`
}

const upto = (s: SkillInfo, day: number) => s.revs.filter((r) => r.day <= day)
const removedBy = (s: SkillInfo, day: number) => s.removedDay !== null && s.removedDay <= day
const linesAt = (s: SkillInfo, day: number) => upto(s, day).at(-1)?.lines ?? 0

export function totalsAt(m: Model, day: number) {
  let skills = 0, revisions = 0, withChecks = 0, removed = 0
  for (const s of m.skills.values()) {
    const n = upto(s, day).length
    revisions += n
    if (n > 0 && !removedBy(s, day)) skills++
    if (removedBy(s, day)) removed++
    if (!s.checks && n > 0 && !removedBy(s, day) && s.checkedBy.some((c) => upto(m.skills.get(c)!, day).length > 0)) withChecks++
  }
  return { skills, revisions, withChecks, removed }
}

export type Tile = { name: string; state: 'unborn' | 'active' | 'recent' | 'removed'; revisions: number; checkRevisions: number; lines: number; selected: boolean }
export type GroupRow = { index: number; title: string; kind: string; num: string; meta: string; dimmed: boolean; tiles: Tile[] }

export function selectedGroup(m: Model, sel: Selection): number | null {
  if (sel.group !== undefined) return m.h.groups.findIndex((g) => g.title === sel.group)
  if (sel.skill) return m.skills.get(sel.skill)?.group ?? null
  return null
}

export function tilesAt(m: Model, day: number, sel: Selection): GroupRow[] {
  const focus = selectedGroup(m, sel)
  return m.h.groups.map((g, index) => {
    let active = 0, revs = 0
    const tiles = g.skills.map((name): Tile => {
      const s = m.skills.get(name)!
      const n = upto(s, day).length
      const checkRevisions = s.checkedBy.reduce((a, c) => a + upto(m.skills.get(c)!, day).length, 0)
      revs += n + checkRevisions
      const recent = [s, ...s.checkedBy.map((c) => m.skills.get(c)!)].some((x) => x.revs.some((r) => r.day <= day && r.day > day - 7))
      const state = n === 0 ? 'unborn' : removedBy(s, day) ? 'removed' : recent ? 'recent' : 'active'
      if (state === 'active' || state === 'recent') active++
      return { name, state, revisions: n, checkRevisions, lines: linesAt(s, day),
               selected: sel.skill === name || s.checkedBy.includes(sel.skill ?? '') }
    })
    return { index, title: g.title, kind: g.kind, num: g.num, meta: `${active} skills · ${revs} revisions`,
             dimmed: focus !== null && focus !== index, tiles }
  })
}

export function skillsSeries(m: Model): number[] {
  return Array.from({ length: m.days + 1 }, (_, d) => totalsAt(m, d).skills)
}

export function weeklyCounts(m: Model, names: string[] | null): number[] {
  const weeks = new Array(Math.floor(m.days / 7) + 1).fill(0)
  const pool = names ?? [...m.skills.keys()]
  for (const n of pool) m.skills.get(n)?.revs.forEach((r) => { weeks[Math.floor(r.day / 7)]++ })
  return weeks
}

/** Skill names the chart highlights for a selection (null = none). */
export function scopeNames(m: Model, sel: Selection): string[] | null {
  if (sel.commit !== undefined) return m.commitSkills[m.bySha.get(sel.commit) ?? -1] ?? []
  if (sel.skill) return [sel.skill]
  const gi = selectedGroup(m, sel)
  if (gi === null || gi < 0) return null
  return m.h.groups[gi].skills.flatMap((s) => [s, ...(m.skills.get(s)?.checkedBy ?? [])])
}

const change = (r: Rev, prev: Rev | undefined) =>
  !prev ? 'created' : r.lines === 0 && r.deleted > 0 ? 'removed' : `${r.lines - prev.lines >= 0 ? '+' : '−'}${Math.abs(r.lines - prev.lines)}`

type CommitRow = { sha: string; date: string; subject: string; skills: string[] }
export type Panel =
  | { kind: 'all'; week: CommitRow[]; top: { name: string; revisions: number }[] }
  | { kind: 'group'; title: string; kindLabel: string; skills: number; revisions: number; first: string | null;
      rows: { name: string; first: string | null; revisions: number; lines: number | null; checkedBy: string[] }[]; recent: CommitRow[] }
  | { kind: 'skill'; name: string; group: string | null; created: string; lastRevised: string | null; removed: string | null;
      revisions: number; lines: number | null; firstLines: number; checks: string | null; checkedBy: string[];
      series: { day: number; lines: number }[];
      list: { sha: string; date: string; subject: string; change: string; later: boolean }[] }
  | { kind: 'commit'; sha: string; date: string; subject: string; rows: { name: string; change: string; lines: number }[] }

const commitRow = (m: Model, i: number): CommitRow => ({
  sha: m.h.commits[i].sha, date: fmtDay(m, m.commitDay[i]), subject: m.h.commits[i].subject, skills: m.commitSkills[i],
})

export function panelAt(m: Model, day: number, sel: Selection): Panel {
  if (sel.commit !== undefined) {
    const i = m.bySha.get(sel.commit)
    if (i !== undefined) {
      return { kind: 'commit', sha: m.h.commits[i].sha, date: fmtDay(m, m.commitDay[i]), subject: m.h.commits[i].subject,
        rows: m.commitSkills[i].map((name) => {
          const s = m.skills.get(name)!
          const k = s.revs.findIndex((r) => r.commit === i)
          return { name, change: change(s.revs[k], s.revs[k - 1]), lines: s.revs[k].lines }
        }) }
    }
  }
  if (sel.skill && m.skills.has(sel.skill)) {
    const s = m.skills.get(sel.skill)!
    const past = upto(s, day)
    return { kind: 'skill', name: s.name, group: s.group !== null ? m.h.groups[s.group].title : null,
      created: fmtDay(m, s.firstDay), lastRevised: past.length ? fmtDay(m, past.at(-1)!.day) : null,
      removed: s.removedDay !== null ? fmtDay(m, s.removedDay) : null,
      revisions: past.length, lines: removedBy(s, day) || !past.length ? null : past.at(-1)!.lines,
      firstLines: s.revs[0]?.lines ?? 0, checks: s.checks, checkedBy: s.checkedBy,
      series: s.revs.map((r) => ({ day: r.day, lines: r.lines })),
      list: s.revs.map((r, k) => ({ sha: m.h.commits[r.commit].sha, date: fmtDay(m, r.day),
        subject: m.h.commits[r.commit].subject, change: change(r, s.revs[k - 1]), later: r.day > day })).reverse() }
  }
  const gi = selectedGroup(m, sel)
  if (gi !== null && gi >= 0) {
    const g = m.h.groups[gi]
    const names = scopeNames(m, sel) ?? []
    const commits = new Set<number>()
    names.forEach((n) => upto(m.skills.get(n)!, day).forEach((r) => commits.add(r.commit)))
    const firsts = names.map((n) => m.skills.get(n)!.firstDay).filter((d) => d <= day)
    return { kind: 'group', title: g.title, kindLabel: g.kind === 'phase' ? `Phase ${g.num}` : g.kind === 'agent' ? 'Agent' : 'No agent',
      skills: g.skills.filter((n) => upto(m.skills.get(n)!, day).length && !removedBy(m.skills.get(n)!, day)).length,
      revisions: names.reduce((a, n) => a + upto(m.skills.get(n)!, day).length, 0),
      first: firsts.length ? fmtDay(m, Math.min(...firsts)) : null,
      rows: g.skills.map((n) => {
        const s = m.skills.get(n)!
        const k = upto(s, day).length
        return { name: n, first: s.firstDay <= day ? fmtDay(m, s.firstDay) : null, revisions: k,
                 lines: k && !removedBy(s, day) ? linesAt(s, day) : null, checkedBy: s.checkedBy }
      }).sort((a, b) => b.revisions - a.revisions),
      recent: [...commits].sort((a, b) => b - a).slice(0, 10).map((i) => commitRow(m, i)) }
  }
  const week = m.commitDay.map((d, i) => ({ d, i })).filter(({ d }) => d <= day && d > day - 7).reverse().map(({ i }) => commitRow(m, i))
  const top = [...m.skills.values()].map((s) => ({ name: s.name, revisions: upto(s, day).length }))
    .filter((t) => t.revisions > 0).sort((a, b) => b.revisions - a.revisions).slice(0, 10)
  return { kind: 'all', week, top }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/agents/skillHistory/model.test.ts`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/agents.ts frontend/src/pages/agents/skillHistory/model.ts frontend/src/pages/agents/skillHistory/model.test.ts
git commit -m "feat(frontend): skill history client and view model

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: The History section UI

**Files:**
- Create: `frontend/src/pages/agents/skillHistory/SkillHistoryChart.tsx`
- Create: `frontend/src/pages/agents/skillHistory/SkillHistoryLanes.tsx`
- Create: `frontend/src/pages/agents/skillHistory/SkillHistoryPanel.tsx`
- Create: `frontend/src/pages/agents/AgentHistorySection.tsx`
- Modify: `frontend/src/router.tsx` (lazy import + child route `history`)
- Modify: `frontend/src/components/agents/AgentLeftNav.tsx:37-50` (add `{ to: 'history', label: 'History' }` after Skills)
- Modify: `frontend/src/widget/pageContext.ts:48` (add `history` to the section alternation)
- Test: `frontend/src/pages/agents/AgentHistorySection.test.tsx`

**Interfaces:**
- Consumes: Task 5's `buildModel`, `tilesAt`, `panelAt`, `totalsAt`, `skillsSeries`, `weeklyCounts`, `scopeNames`, `fmtDay`, `dayOf`, `isoOf`, `Selection`; `getSkillHistory`, `syncSkillHistory`; `AgentOutletContext`.
- Produces: `AgentHistorySection` (default route element). Selection state is the URL search params `group`, `skill`, `commit`, `at`. Children receive `onSelect(sel: Selection)` and `onDay(day: number)`.

- [ ] **Step 1: Write the failing UI test**

```tsx
// frontend/src/pages/agents/AgentHistorySection.test.tsx
// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const H = {
  agent: 'ace', repo_url: 'x', head_sha: 'b', synced_at: '2026-04-20T00:00:00Z', synced_with: 'owner-gh',
  last_error: '', credential_state: 'ok',
  groups: [{ title: 'One', kind: 'phase', num: '01', skills: ['alpha'] }],
  checks: {}, present: ['alpha'],
  commits: [{ sha: 'a1', date: '2026-04-01', subject: 'feat: alpha' }, { sha: 'b2', date: '2026-04-05', subject: 'fix: alpha grows' }],
  skills: [{ name: 'alpha', revisions: [[0, 10, 10, 0], [1, 12, 2, 0]] }],
}

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getSkillHistory: vi.fn(async () => H),
  syncSkillHistory: vi.fn(async () => H),
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'ace', name: 'ACE' } }),
}))

const { AgentHistorySection } = await import('./AgentHistorySection')

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.search}</div>
}

function renderAt(search = '') {
  render(
    <MemoryRouter initialEntries={[`/w/connect/agents/ace/history${search}`]}>
      <Routes><Route path="/w/:workspace/agents/:slug/history" element={<><AgentHistorySection /><Where /></>} /></Routes>
    </MemoryRouter>,
  )
}

afterEach(() => cleanup())

describe('AgentHistorySection', () => {
  it('drills all skills → group → skill → commit and back through the breadcrumb', async () => {
    renderAt()
    fireEvent.click(await screen.findByRole('button', { name: /Open One/ }))
    expect(screen.getByTestId('where').textContent).toContain('group=One')

    fireEvent.click(screen.getByRole('button', { name: 'Skill alpha' }))
    expect(screen.getByTestId('where').textContent).toContain('skill=alpha')
    expect(screen.getByText('fix: alpha grows')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /fix: alpha grows/ }))
    expect(screen.getByTestId('where').textContent).toContain('commit=b2')

    fireEvent.click(screen.getByRole('button', { name: 'All skills' }))
    expect(screen.getByTestId('where').textContent).toBe('')
  })

  it('restores a selection from the URL', async () => {
    renderAt('?skill=alpha&at=2026-04-02')
    expect(await screen.findByText(/Created Apr 1/)).toBeTruthy()
  })

  it('says whose GitHub access produced the history', async () => {
    renderAt()
    expect(await screen.findByText(/owner-gh/)).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/pages/agents/AgentHistorySection.test.tsx`
Expected: FAIL — cannot resolve `./AgentHistorySection`.

- [ ] **Step 3: Implement the chart**

```tsx
// frontend/src/pages/agents/skillHistory/SkillHistoryChart.tsx
import type { Model } from './model'
import { fmtDay, skillsSeries, weeklyCounts } from './model'

const W = 880, AH = 190, BY = 200, BH = 70

export function SkillHistoryChart({ model, day, scope, scopeLabel, onDay }: {
  model: Model; day: number; scope: string[] | null; scopeLabel: string; onDay: (d: number) => void
}) {
  const N = Math.max(model.days, 1)
  const x = (d: number) => (d / N) * W
  const series = skillsSeries(model)
  const max = Math.max(...series, 1)
  const y = (v: number) => AH - (v / max) * 150
  const line = series.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const area = `M0,${AH} ${series.map((v, i) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} L${W},${AH} Z`
  const all = weeklyCounts(model, null)
  const sel = scope ? weeklyCounts(model, scope) : null
  const bmax = Math.max(...all, 1)
  const bw = W / all.length
  const bars = (w: number[]) => w.map((c, i) => {
    const h = c ? Math.max(1.5, (c / bmax) * BH) : 0
    return `M${(i * bw + 2).toFixed(1)},${(BY + BH - h).toFixed(1)} h${(bw - 4).toFixed(1)} v${h.toFixed(1)} h-${(bw - 4).toFixed(1)} Z`
  }).join(' ')
  const months: number[] = []
  for (let d = 0; d <= model.days; d++) if (fmtDay(model, d).endsWith(' 1')) months.push(d)
  const cx = x(day)

  return (
    <figure className="m-0 flex flex-col gap-1.5">
      <figcaption className="flex items-baseline justify-between text-[13px]">
        <span className="font-semibold text-foreground">Skills in the repo, and SKILL.md revisions per week
          <span className="font-normal text-muted-foreground"> · {scopeLabel}</span></span>
        <span className="text-[12px] text-muted-foreground">Click the chart to move to that date</span>
      </figcaption>
      <svg viewBox={`0 -10 ${W} 320`} className="w-full overflow-visible" role="img"
           aria-label={`Skills over time and revisions per week, ${scopeLabel}`}>
        <path d={area} className="fill-muted" />
        <path d={line} fill="none" className="stroke-primary" strokeWidth={2.5} />
        <path d={bars(all)} className="fill-muted-foreground/40" />
        {sel && <path d={bars(sel)} className="fill-primary" />}
        {months.map((d) => (
          <text key={d} x={x(d)} y={BY + BH + 24} className="fill-muted-foreground font-mono text-[12px]">{fmtDay(model, d).split(' ')[0]}</text>
        ))}
        <text x={W - 4} y={y(max) - 6} textAnchor="end" className="fill-muted-foreground font-mono text-[11px]">{max} skills</text>
        <line x1={cx} x2={cx} y1={-6} y2={BY + BH + 6} className="stroke-foreground" strokeWidth={1.5} />
        <rect x={0} y={-10} width={W} height={BY + BH + 20} fill="transparent" className="cursor-pointer"
              onClick={(e) => {
                const r = (e.currentTarget as SVGRectElement).getBoundingClientRect()
                onDay(Math.round(((e.clientX - r.left) / r.width) * model.days))
              }} />
      </svg>
    </figure>
  )
}
```

- [ ] **Step 4: Implement the lanes**

```tsx
// frontend/src/pages/agents/skillHistory/SkillHistoryLanes.tsx
import type { GroupRow, Selection } from './model'

const LOOK = {
  unborn: 'border border-dashed border-border bg-transparent opacity-50',
  active: 'border border-border bg-card',
  recent: 'border-2 border-primary bg-primary/5',
  removed: 'border border-border bg-muted opacity-70',
} as const

export function SkillHistoryLanes({ rows, onSelect }: { rows: GroupRow[]; onSelect: (s: Selection) => void }) {
  return (
    <section className="flex flex-col">
      <header className="flex flex-wrap items-baseline justify-between gap-3 border-b border-border pb-2">
        <h2 className="m-0 text-[18px] font-semibold text-foreground">Skills by agent</h2>
        <ul className="m-0 flex list-none flex-wrap gap-4 p-0 text-[12px] text-foreground-secondary">
          <li className="flex items-center gap-1.5"><span className="h-1.5 w-5 rounded bg-primary" />revisions</li>
          <li className="flex items-center gap-1.5"><span className="h-1 w-5 rounded bg-info" />its QA / eval skills</li>
          <li className="flex items-center gap-1.5"><span className="h-3 w-3 rounded border-2 border-primary" />revised in the last 7 days</li>
          <li className="flex items-center gap-1.5"><span className="h-3 w-3 rounded border border-dashed border-border" />not yet created</li>
        </ul>
      </header>
      {rows.map((g) => (
        <div key={g.index} className={`flex items-start gap-3 border-b border-border py-2.5 ${g.dimmed ? 'opacity-45' : ''}`}>
          <button type="button" aria-label={`Open ${g.title}`} onClick={() => onSelect({ group: g.title })}
                  className="w-[150px] shrink-0 rounded-md px-1.5 py-1 text-left hover:bg-muted">
            <span className="block font-mono text-[12px] text-primary">{g.kind === 'phase' ? `PHASE ${g.num}` : g.kind === 'agent' ? 'AGENT' : 'NO AGENT'}</span>
            <span className="block text-[14px] font-semibold leading-tight text-foreground">{g.title}</span>
            <span className="block text-[12px] text-muted-foreground">{g.meta}</span>
          </button>
          <div className="flex flex-grow flex-wrap gap-2">
            {g.tiles.map((t) => (
              <button key={t.name} type="button" aria-label={`Skill ${t.name}`} onClick={() => onSelect({ skill: t.name })}
                      title={`${t.name} · ${t.revisions} revisions${t.lines ? `, ${t.lines} lines` : ''}`}
                      className={`flex h-14 w-28 flex-col gap-1.5 rounded-lg px-2 py-1.5 text-left hover:ring-2 hover:ring-primary ${LOOK[t.state]} ${t.selected ? 'ring-[3px] ring-foreground' : ''}`}>
                <span className={`block w-full truncate font-mono text-[10.5px] ${t.state === 'removed' ? 'text-muted-foreground line-through' : 'text-foreground'}`}>{t.name}</span>
                <span className="flex items-center gap-1.5">
                  <span className="block h-[5px] rounded bg-primary" style={{ width: Math.min(t.revisions, 90) }} />
                  <span className="font-mono text-[10px] text-muted-foreground">{t.state === 'removed' ? 'removed' : t.revisions || ''}</span>
                </span>
                {t.checkRevisions > 0 && (
                  <span className="flex items-center gap-1.5">
                    <span className="block h-1 rounded bg-info" style={{ width: Math.min(t.checkRevisions, 90) }} />
                    <span className="font-mono text-[10px] text-info">{t.checkRevisions}</span>
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      ))}
    </section>
  )
}
```

- [ ] **Step 5: Implement the panel**

```tsx
// frontend/src/pages/agents/skillHistory/SkillHistoryPanel.tsx
import type { Panel, Selection } from './model'

type Crumb = { label: string; sel: Selection | null }

function Row({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return <button type="button" onClick={onClick} className="flex w-full gap-2.5 rounded-lg px-2 py-1.5 text-left hover:bg-muted">{children}</button>
}
const Kicker = ({ children }: { children: React.ReactNode }) => (
  <div className="font-mono text-[11.5px] uppercase tracking-wider text-muted-foreground">{children}</div>
)
const Stat = ({ value, label }: { value: React.ReactNode; label: string }) => (
  <div className="flex flex-col"><span className="text-[24px] font-semibold text-foreground">{value}</span><span className="text-[12px] text-muted-foreground">{label}</span></div>
)
const Change = ({ c }: { c: string }) => (
  <span className={`font-mono text-[11px] ${c.startsWith('+') || c === 'created' ? 'text-success' : c === 'removed' ? 'text-muted-foreground' : 'text-destructive'}`}>{c}</span>
)

export function SkillHistoryPanel({ panel, crumbs, dateLabel, onSelect, onDay, commitDay }: {
  panel: Panel; crumbs: Crumb[]; dateLabel: string
  onSelect: (s: Selection) => void; onDay: (day: number) => void; commitDay: (sha: string) => number
}) {
  return (
    <aside className="flex max-h-[calc(100vh-48px)] w-[416px] shrink-0 flex-col overflow-y-auto rounded-2xl border border-border bg-card lg:sticky lg:top-6">
      <nav aria-label="Selection" className="flex flex-wrap items-center gap-2 border-b border-border px-5 py-3.5 text-[13px]">
        {crumbs.map((c, i) => (
          <span key={i} className="flex items-center gap-2">
            {i < crumbs.length - 1
              ? <button type="button" className="text-primary underline underline-offset-2" onClick={() => onSelect(c.sel ?? {})}>{c.label}</button>
              : <span className="font-semibold text-foreground">{c.label}</span>}
            {i < crumbs.length - 1 && <span className="text-muted-foreground">›</span>}
          </span>
        ))}
      </nav>
      <div className="flex flex-col gap-5 p-5">
        {panel.kind === 'all' && <>
          <div><Kicker>7 days to {dateLabel}</Kicker>
            <div className="text-[20px] font-semibold text-foreground">{panel.week.length} commits</div></div>
          <div className="flex flex-col">
            {panel.week.length === 0 && <p className="text-[14px] text-muted-foreground">No skill revisions in these 7 days.</p>}
            {panel.week.map((c) => (
              <div key={c.sha} className="flex flex-col gap-1.5 border-b border-border py-2">
                <Row onClick={() => onSelect({ commit: c.sha })}>
                  <span className="w-11 shrink-0 font-mono text-[11.5px] text-muted-foreground">{c.date}</span>
                  <span className="line-clamp-2 text-[14px] text-foreground">{c.subject}</span>
                </Row>
                <div className="flex flex-wrap gap-1.5 pl-14">
                  {c.skills.map((s) => (
                    <button key={s} type="button" onClick={() => onSelect({ skill: s })}
                            className="rounded-md border border-border bg-background px-1.5 py-0.5 font-mono text-[11.5px] hover:border-primary">{s}</button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div className="flex flex-col gap-1"><Kicker>Most revised, as of {dateLabel}</Kicker>
            {panel.top.map((t) => (
              <Row key={t.name} onClick={() => onSelect({ skill: t.name })}>
                <span className="flex-grow font-mono text-[12px]">{t.name}</span>
                <span className="font-mono text-[12px] text-muted-foreground">{t.revisions}</span>
              </Row>
            ))}
          </div>
        </>}

        {panel.kind === 'group' && <>
          <div><Kicker>{panel.kindLabel}</Kicker><div className="text-[24px] font-semibold text-foreground">{panel.title}</div></div>
          <div className="grid grid-cols-3 gap-3">
            <Stat value={panel.skills} label="skills" /><Stat value={panel.revisions} label="revisions incl. QA / eval" />
            <Stat value={panel.first ?? '—'} label="first revision" />
          </div>
          <div className="flex flex-col">
            {panel.rows.map((r) => (
              <Row key={r.name} onClick={() => onSelect({ skill: r.name })}>
                <span className="flex min-w-0 flex-grow flex-col">
                  <span className="truncate font-mono text-[12.5px] text-foreground">{r.name}</span>
                  {r.checkedBy.length > 0 && <span className="text-[11.5px] text-info">checked by {r.checkedBy.join(', ')}</span>}
                </span>
                <span className="w-12 font-mono text-[11.5px] text-muted-foreground">{r.first ?? '—'}</span>
                <span className="w-10 text-right font-mono text-[12px]">{r.revisions || '—'}</span>
                <span className="w-12 text-right font-mono text-[12px] text-muted-foreground">{r.lines?.toLocaleString() ?? '—'}</span>
              </Row>
            ))}
          </div>
          <div className="flex flex-col"><Kicker>Latest commits, as of {dateLabel}</Kicker>
            {panel.recent.map((c) => (
              <Row key={c.sha} onClick={() => onSelect({ commit: c.sha })}>
                <span className="w-11 shrink-0 font-mono text-[11.5px] text-muted-foreground">{c.date}</span>
                <span className="line-clamp-2 text-[13.5px]">{c.subject}</span>
              </Row>
            ))}
          </div>
        </>}

        {panel.kind === 'skill' && <>
          <div className="flex flex-col gap-1">
            {panel.group && <button type="button" className="self-start font-mono text-[11.5px] text-primary underline" onClick={() => onSelect({ group: panel.group! })}>{panel.group}</button>}
            <div className="break-words font-mono text-[20px] text-foreground">{panel.name}</div>
            <div className="text-[13px] text-foreground-secondary">
              Created {panel.created}{panel.removed ? ` · removed ${panel.removed}` : panel.lastRevised ? ` · last revised ${panel.lastRevised}` : ''}.
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Stat value={panel.revisions} label="revisions" /><Stat value={panel.lines?.toLocaleString() ?? '—'} label="lines now" />
            <Stat value={panel.firstLines.toLocaleString()} label="lines at first" />
          </div>
          {(panel.checks || panel.checkedBy.length > 0) && (
            <div className="flex flex-col gap-1.5"><span className="text-[12px] text-muted-foreground">{panel.checks ? 'Checks' : 'Checked by'}</span>
              <div className="flex flex-wrap gap-1.5">
                {(panel.checks ? [panel.checks] : panel.checkedBy).map((s) => (
                  <button key={s} type="button" onClick={() => onSelect({ skill: s })}
                          className="rounded-md border border-border px-1.5 py-0.5 font-mono text-[11.5px] text-info hover:border-primary">{s}</button>
                ))}
              </div>
            </div>
          )}
          <div className="flex flex-col"><Kicker>Revisions, newest first</Kicker>
            {panel.list.map((r) => (
              <Row key={r.sha} onClick={() => onSelect({ commit: r.sha })}>
                <span className={`flex w-12 shrink-0 flex-col ${r.later ? 'opacity-40' : ''}`}>
                  <span className="font-mono text-[11.5px] text-muted-foreground">{r.date}</span><Change c={r.change} />
                </span>
                <span className={`line-clamp-2 text-[13.5px] ${r.later ? 'opacity-40' : ''}`}>{r.subject}</span>
              </Row>
            ))}
          </div>
        </>}

        {panel.kind === 'commit' && <>
          <div className="flex flex-col gap-2">
            <div className="font-mono text-[12px] text-muted-foreground">commit {panel.sha.slice(0, 8)} · {panel.date}</div>
            <div className="text-[17px] font-medium text-foreground">{panel.subject}</div>
          </div>
          <button type="button" onClick={() => onDay(commitDay(panel.sha))}
                  className="self-start rounded-lg border border-border px-3.5 py-2 text-[13.5px] hover:border-primary">Move timeline to {panel.date}</button>
          <div className="flex flex-col"><Kicker>Skills changed ({panel.rows.length})</Kicker>
            {panel.rows.map((r) => (
              <Row key={r.name} onClick={() => onSelect({ skill: r.name })}>
                <span className="flex-grow truncate font-mono text-[12.5px]">{r.name}</span>
                <Change c={r.change} />
                <span className="w-12 text-right font-mono text-[12px] text-muted-foreground">{r.lines ? r.lines.toLocaleString() : '—'}</span>
              </Row>
            ))}
          </div>
        </>}
      </div>
    </aside>
  )
}
```

- [ ] **Step 6: Implement the container**

```tsx
// frontend/src/pages/agents/AgentHistorySection.tsx
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useOutletContext, useSearchParams } from 'react-router-dom'
import { getSkillHistory, syncSkillHistory, type SkillHistoryOut } from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { WorkbenchSkeleton } from 'canopy-ui'
import { SkillHistoryChart } from './skillHistory/SkillHistoryChart'
import { SkillHistoryLanes } from './skillHistory/SkillHistoryLanes'
import { SkillHistoryPanel } from './skillHistory/SkillHistoryPanel'
import { buildModel, dayOf, fmtDay, isoOf, panelAt, scopeNames, tilesAt, totalsAt, type Selection } from './skillHistory/model'

const STATE_COPY: Record<string, string> = {
  no_repo: 'This agent has no repository configured, so there is no history to read.',
  no_owner: 'History reads through the agent owner’s GitHub connection, and this agent has no owner.',
  owner_not_connected: 'History reads through the agent owner’s GitHub connection. The owner hasn’t connected GitHub.',
  repo_not_granted: 'The owner’s GitHub connection can’t reach this agent’s repository. The owner can add it on GitHub’s installation screen.',
}

export function AgentHistorySection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [data, setData] = useState<SkillHistoryOut | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [params, setParams] = useSearchParams()
  const [playing, setPlaying] = useState(false)
  const timer = useRef<number | null>(null)

  const load = useCallback(() => getSkillHistory(agent.slug).then(setData).catch(() => setData(null)), [agent.slug])
  useEffect(() => { setData(null); void load() }, [load])

  const model = useMemo(() => (data ? buildModel(data) : null), [data])
  const sel: Selection = {
    ...(params.get('group') ? { group: params.get('group')! } : {}),
    ...(params.get('skill') ? { skill: params.get('skill')! } : {}),
    ...(params.get('commit') ? { commit: params.get('commit')! } : {}),
  }
  const at = params.get('at')
  const day = model ? Math.min(model.days, Math.max(0, at ? dayOf(model, at) : model.days)) : 0

  const write = useCallback((next: Selection, nextDay: number | null) => {
    const p = new URLSearchParams()
    if (next.group) p.set('group', next.group)
    if (next.skill) p.set('skill', next.skill)
    if (next.commit) p.set('commit', next.commit)
    if (model && nextDay !== null && nextDay < model.days) p.set('at', isoOf(model, nextDay))
    setParams(p, { replace: false })
  }, [model, setParams])

  const onSelect = (next: Selection) => write(next, day)
  const onDay = (d: number) => { stop(); write(sel, Math.max(0, Math.min(model?.days ?? 0, d))) }
  const stop = () => { if (timer.current) window.clearInterval(timer.current); timer.current = null; setPlaying(false) }
  useEffect(() => () => stop(), [])

  if (!data || !model) return <div className="px-6 py-8"><WorkbenchSkeleton /></div>

  const totals = totalsAt(model, day)
  const scope = scopeNames(model, sel)
  const panel = panelAt(model, day, sel)
  const crumbs = [{ label: 'All skills', sel: {} as Selection }]
  const skillGroup = sel.skill ? model.skills.get(sel.skill)?.group ?? null : null
  if (sel.group) crumbs.push({ label: sel.group, sel: { group: sel.group } })
  if (sel.skill) {
    if (skillGroup !== null) crumbs.push({ label: model.h.groups[skillGroup].title, sel: { group: model.h.groups[skillGroup].title } })
    crumbs.push({ label: sel.skill, sel: { skill: sel.skill } })
  }
  if (sel.commit) crumbs.push({ label: `commit ${sel.commit.slice(0, 8)}`, sel: { commit: sel.commit } })

  const play = () => {
    if (playing) return stop()
    let d = day >= model.days ? 0 : day
    setPlaying(true)
    timer.current = window.setInterval(() => {
      d += 1
      if (d >= model.days) { stop(); write(sel, model.days) } else write(sel, d)
    }, 60)
  }

  const sync = async () => { setSyncing(true); try { setData(await syncSkillHistory(agent.slug)) } finally { setSyncing(false) } }

  return (
    <div className="flex flex-col gap-6 px-6 py-8">
      <header className="flex flex-wrap items-end justify-between gap-6">
        <div className="flex max-w-3xl flex-col gap-2">
          <h1 className="m-0 text-[28px] font-semibold text-foreground">How {agent.name}’s skills changed</h1>
          <p className="m-0 text-[14px] text-foreground-secondary">
            Every skill in the repository, read from its git history. A revision is a commit that changed the skill’s SKILL.md.
            Select a group, a skill, a week on the chart or a commit.
          </p>
          <p className="m-0 text-[12px] text-muted-foreground">
            {data.synced_at ? `Read ${new Date(data.synced_at).toLocaleString()} using ${data.synced_with}’s GitHub access.` : 'Not read yet.'}
            {' '}<button type="button" onClick={sync} disabled={syncing} className="text-primary underline disabled:opacity-50">{syncing ? 'Syncing…' : 'Sync from GitHub'}</button>
          </p>
          {data.credential_state !== 'ok' && (
            <p role="status" className="m-0 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-[13px] text-warning">
              {STATE_COPY[data.credential_state]}
              {(data.credential_state === 'owner_not_connected' || data.credential_state === 'repo_not_granted') && (
                <> <Link to="/settings" className="underline">GitHub settings</Link></>
              )}
            </p>
          )}
          {data.last_error && data.credential_state === 'ok' && <p role="status" className="m-0 text-[12px] text-destructive">Last sync failed: {data.last_error}</p>}
        </div>
        <dl className="m-0 grid grid-cols-4 gap-6">
          {([[totals.skills, 'skills'], [totals.revisions.toLocaleString(), 'revisions'], [totals.withChecks, 'skills with a QA or eval skill'], [totals.removed, 'skills removed']] as const).map(([v, l]) => (
            <div key={l} className="flex flex-col"><dd className="m-0 text-[32px] font-semibold leading-none text-foreground">{v}</dd><dt className="text-[12px] text-muted-foreground">{l}</dt></div>
          ))}
        </dl>
      </header>

      <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3">
        <button type="button" onClick={play} aria-label={playing ? 'Pause' : 'Play from the first commit'}
                className="flex h-11 w-11 items-center justify-center rounded-full bg-primary text-primary-foreground">{playing ? '❚❚' : '▶'}</button>
        <button type="button" aria-label="Back one week" onClick={() => onDay(day - 7)} className="h-11 w-11 rounded-lg border border-border">‹</button>
        <button type="button" aria-label="Forward one week" onClick={() => onDay(day + 7)} className="h-11 w-11 rounded-lg border border-border">›</button>
        <label htmlFor="history-day" className="w-16 font-mono text-[14px]">{fmtDay(model, day)}</label>
        <input id="history-day" type="range" min={0} max={model.days} value={day} onChange={(e) => onDay(Number(e.target.value))} className="flex-grow accent-primary" />
        <button type="button" onClick={() => onDay(model.days)} className="h-11 rounded-lg border border-border px-3 text-[13px]">Latest</button>
      </div>

      <div className="flex flex-col gap-8 lg:flex-row lg:items-start">
        <div className="flex min-w-0 flex-grow flex-col gap-6">
          <SkillHistoryChart model={model} day={day} scope={scope} onDay={onDay}
                             scopeLabel={sel.commit ? `skills in commit ${sel.commit.slice(0, 8)}` : sel.skill ?? sel.group ?? 'all skills'} />
          <SkillHistoryLanes rows={tilesAt(model, day, sel)} onSelect={onSelect} />
          <p className="text-[12px] text-muted-foreground">Groups come from the agent files in the repository. A QA or eval skill is shown inside the skill it checks. Bars are 1px per revision, capped at 90.</p>
        </div>
        <SkillHistoryPanel panel={panel} crumbs={crumbs} dateLabel={fmtDay(model, day)} onSelect={onSelect} onDay={onDay}
                           commitDay={(sha) => model.commitDay[model.bySha.get(sha) ?? 0]} />
      </div>
    </div>
  )
}
```

Note: `❚❚` / `▶` are text glyphs, not emoji; if the project's lint forbids them, replace with the inline stroke SVGs used elsewhere in `canopy-ui`.

- [ ] **Step 7: Wire the route, nav and page-context rule**

In `frontend/src/router.tsx`, next to `AgentSkillsSection`:

```tsx
const AgentHistorySection = lazySection(() =>
  import('./pages/agents/AgentHistorySection').then((m) => ({ default: m.AgentHistorySection })),
)
```

and in the agent children after the `skills` route:

```tsx
          { path: 'history', element: <LazySection><AgentHistorySection /></LazySection> },
```

In `frontend/src/components/agents/AgentLeftNav.tsx`, after `{ to: 'skills', label: 'Skills' },` add `{ to: 'history', label: 'History' },`.

In `frontend/src/widget/pageContext.ts`, change `(items|inbox|turns|tasks|schedules|syncs|skills|runners|overview)` to `(items|inbox|turns|tasks|schedules|syncs|skills|history|runners|overview)`.

- [ ] **Step 8: Run tests**

Run: `cd frontend && npx vitest run src/pages/agents/ src/widget/`
Expected: all pass, including the 3 new UI tests.

- [ ] **Step 9: Type-check and build**

Run: `cd frontend && npm run build`
Expected: succeeds with no type errors.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/pages/agents/AgentHistorySection.tsx frontend/src/pages/agents/AgentHistorySection.test.tsx frontend/src/pages/agents/skillHistory/ frontend/src/router.tsx frontend/src/components/agents/AgentLeftNav.tsx frontend/src/widget/pageContext.ts
git commit -m "feat(agents): History section — drill from all skills to one commit

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Tell the assistant what is selected

**Files:**
- Modify: `frontend/src/pages/agents/AgentHistorySection.tsx`
- Modify: `frontend/src/pages/agents/AgentHistorySection.test.tsx`

**Interfaces:**
- Consumes: `usePageState` (`@/widget/usePageState`), `describeSelection` (`@/widget/pageState`), `usePageAction` (`@/widget/usePageAction`), `useResource` (`@/widget/useResource`), test helpers `currentPageState` (`@/widget/pageState`), `currentSpecs`, `runPageAction` (`@/widget/pageActions`).
- Produces: page state `{ backing_tool: 'skill_history', resource: 'skill-history://<slug>', visible_ids, visible_count, filters: { agent, group, skill, commit, as_of } }`; page actions `selectSkill`, `showCommit`, `setTimeline`.

- [ ] **Step 1: Add the failing tests** (append to `AgentHistorySection.test.tsx`)

```tsx
const { currentPageState } = await import('@/widget/pageState')
const { currentSpecs, runPageAction } = await import('@/widget/pageActions')
import { act } from '@testing-library/react'

describe('the page contract', () => {
  it('declares the selected skill and date to the assistant', async () => {
    renderAt('?skill=alpha&at=2026-04-02')
    await screen.findByText(/Created Apr 1/)
    expect(currentPageState()).toMatchObject({
      backing_tool: 'skill_history',
      resource: 'skill-history://ace',
      visible_ids: ['alpha'],
      filters: { agent: 'ace', skill: 'alpha', as_of: '2026-04-02' },
    })
  })

  it('declares nothing selected at the top level', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    expect(currentPageState()).toMatchObject({ visible_ids: [], filters: { agent: 'ace', as_of: '2026-04-20' } })
  })

  it('offers selectSkill, showCommit and setTimeline', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    expect(currentSpecs().map((s) => s.name)).toEqual(expect.arrayContaining(['selectSkill', 'showCommit', 'setTimeline']))
  })

  it('selectSkill moves the page, and refuses a skill that does not exist', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    await act(() => runPageAction('selectSkill', { skill: 'alpha' }))
    expect(screen.getByTestId('where').textContent).toContain('skill=alpha')
    await expect(runPageAction('selectSkill', { skill: 'nope' })).rejects.toThrow(/no skill named nope/)
  })

  it('showCommit accepts a short sha and refuses an unknown one', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    await act(() => runPageAction('showCommit', { sha: 'b2' }))
    expect(screen.getByTestId('where').textContent).toContain('commit=b2')
    await expect(runPageAction('showCommit', { sha: 'zzz' })).rejects.toThrow(/no commit/)
  })

  it('setTimeline refuses a date outside the history', async () => {
    renderAt()
    await screen.findByText(/owner-gh/)
    await act(() => runPageAction('setTimeline', { date: '2026-04-03' }))
    expect(screen.getByTestId('where').textContent).toContain('at=2026-04-03')
    await expect(runPageAction('setTimeline', { date: '2020-01-01' })).rejects.toThrow(/outside/)
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && npx vitest run src/pages/agents/AgentHistorySection.test.tsx`
Expected: the 6 new tests FAIL (no page state / no actions).

- [ ] **Step 3: Implement** — in `AgentHistorySection.tsx`, add imports:

```tsx
import { describeSelection } from '@/widget/pageState'
import { usePageAction } from '@/widget/usePageAction'
import { usePageState } from '@/widget/usePageState'
import { useResource } from '@/widget/useResource'
```

and, **before** the `if (!data || !model) return …` early return (hooks must run unconditionally):

```tsx
  const resource = `skill-history://${agent.slug}`
  const asOf = model ? isoOf(model, day) : null

  // The selection, not the data: the assistant resolves it through the
  // `skill_history` tool with the caller's own access. Rides the first message
  // and is re-read via `current_page` later (embedding doc §5).
  usePageState(
    () => describeSelection({
      backingTool: 'skill_history',
      resource,
      ids: sel.commit ? [sel.commit] : sel.skill ? [sel.skill] : sel.group ? [sel.group] : [],
      filters: { agent: agent.slug, group: sel.group ?? null, skill: sel.skill ?? null, commit: sel.commit ?? null, as_of: asOf },
    }),
    [agent.slug, sel.group, sel.skill, sel.commit, asOf],
  )

  // A sync from another tab, or one the assistant triggered, repaints this one.
  useResource(resource, load)

  usePageAction('selectSkill', ({ skill }) => {
    if (!model || typeof skill !== 'string' || !model.skills.has(skill)) throw new Error(`no skill named ${String(skill)} in ${agent.slug}'s history`)
    write({ skill }, day)
    return { selected: skill }
  }, {
    description: 'Open one skill’s history on the page the user is viewing',
    parameters: { type: 'object', properties: { skill: { type: 'string' } }, required: ['skill'] },
  })

  usePageAction('showCommit', ({ sha }) => {
    const full = model && typeof sha === 'string' ? model.h.commits.find((c) => c.sha.startsWith(sha))?.sha : undefined
    if (!full) throw new Error(`no commit ${String(sha)} in ${agent.slug}'s skill history`)
    write({ commit: full }, day)
    return { shown: full }
  }, {
    description: 'Open one commit on the page the user is viewing (full or abbreviated sha)',
    parameters: { type: 'object', properties: { sha: { type: 'string' } }, required: ['sha'] },
  })

  usePageAction('setTimeline', ({ date }) => {
    if (!model || typeof date !== 'string') throw new Error('date must be YYYY-MM-DD')
    const d = dayOf(model, date)
    if (Number.isNaN(d) || d < 0 || d > model.days) throw new Error(`${date} is outside this history (${isoOf(model, 0)} to ${isoOf(model, model.days)})`)
    write(sel, d)
    return { date }
  }, {
    description: 'Move the page’s timeline to a date (YYYY-MM-DD)',
    parameters: { type: 'object', properties: { date: { type: 'string' } }, required: ['date'] },
  })
```

`showCommit` resolves an abbreviated sha to the full one before writing the URL, so `sel.commit` is always a full sha and `panelAt`'s exact `bySha` lookup holds.

- [ ] **Step 4: Run tests**

Run: `cd frontend && npx vitest run src/pages/agents/AgentHistorySection.test.tsx src/widget/`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/agents/AgentHistorySection.tsx frontend/src/pages/agents/AgentHistorySection.test.tsx
git commit -m "feat(agents): the assistant knows which skill, commit and date the History page shows

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Docs, full suite, PR, deploy and the live check

**Files:**
- Modify: `CLAUDE.md` (Key URLs list — one line under `/w/:workspace/agents/:slug`)
- Modify: `docs/architecture/api-surface.md` (Agents section — the two routes)
- Modify: `docs/architecture/mcp-surface.md` (tool inventory — the two tools)
- Modify: `docs/superpowers/specs/2026-09-18-agent-skill-history-design.md` (Status line → built, with PR number)

- [ ] **Step 1: Update docs**

In `CLAUDE.md`, in the `/w/:workspace/agents/:slug` sub-routes sentence, add **History** after Skills: "…, Skills, **History** (how each skill changed, from the agent repo's git history, read with the owner's GitHub grant; the in-app assistant sees the selected skill/commit/date)". In `api-surface.md` add `GET /api/agents/{slug}/skill-history/` (member; syncs first when >1 h stale) and `POST /api/agents/{slug}/skill-history/sync` (editor+). In `mcp-surface.md` add `skill_history` and `skill_revision_diff` with one line each.

- [ ] **Step 2: Run the whole backend suite**

Run: `uv run pytest -q`
Expected: all pass (including `test_architecture_boundary.py`, `test_workspace_authorizer_is_sole_gate.py`).

- [ ] **Step 3: Run the whole frontend suite and build**

Run: `cd frontend && npm test && npm run build`
Expected: all pass; build succeeds.

- [ ] **Step 4: Verify generated types are fresh**

Run: `cd frontend && npm run gen:api:local && git status --porcelain src/api/generated.ts`
Expected: no output (unchanged).

- [ ] **Step 5: Commit docs, push, open the PR with auto-merge**

```bash
git add CLAUDE.md docs/architecture/api-surface.md docs/architecture/mcp-surface.md docs/superpowers/specs/2026-09-18-agent-skill-history-design.md
git commit -m "docs: agent skill history — routes, tools, and the History section

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push -u origin HEAD
gh pr create --title "feat(agents): an agent's skill history, and an assistant that knows which skill you mean" --body-file /tmp/pr-body.md
gh pr merge <n> --auto
gh pr view <n> --json autoMergeRequest
```

PR body: summary of the three decisions from the spec, the credential rationale, test commands run with their results, and the line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

- [ ] **Step 6: After merge, deploy and confirm what shipped**

```bash
gh workflow run "Deploy to Labs (AWS)" --ref main
gh run list --workflow="Deploy to Labs (AWS)" --limit 3 --json headSha,conclusion
curl -s https://labs.connect.dimagi.com/canopy/api/openapi.json | grep -c skill-history
```

Expected: the deploy of the merge sha concludes `success`; the grep prints ≥ 1.

- [ ] **Step 7: Live check — the whole chain, asserting the answer**

1. In a browser (not curl — the PWA service worker is in the path), open `https://labs.connect.dimagi.com/canopy/w/connect/agents/ace/history`. Confirm the page renders ACE's groups and "Read … using <owner>’s GitHub access". If the state banner shows instead, fix the named cause (connect GitHub as ACE's owner / add `dimagi-internal/ace` to the installation) and press **Sync from GitHub**.
2. Select `idea-to-pdd`. Open the Canopy AI widget and ask: "explain what types of improvements we've been making to this skill".
3. **Pass only if** the answer names `idea-to-pdd` and cites at least two of its actual commits (e.g. the native Google Doc change #1061, the reviewer-comments change) — not merely that a reply arrived.
4. Ask: "show me the commit where it started reading reviewer comments". **Pass only if** the page moves to that commit (the `showCommit` action ran).

Record the outcome (pass/fail with what was seen) in the PR as a comment.
