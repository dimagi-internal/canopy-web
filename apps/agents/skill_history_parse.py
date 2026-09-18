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
