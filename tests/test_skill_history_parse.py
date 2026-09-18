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
