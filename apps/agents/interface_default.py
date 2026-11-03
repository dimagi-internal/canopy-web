"""The default interface, apart from `interface.py` so the Agent model can use it as a
field default without importing the whole access machinery.

Every agent has an interface from the moment it exists: this one, until its owner
changes it. See `apps/agents/interface.py` and `docs/architecture/access.md`.
"""
from __future__ import annotations

import re

def default_interface() -> dict:
    """No door and no read-mode override. A fresh dict every call."""
    return {"capabilities": {}}


# --- the default read profile ---------------------------------------------------------------
#
# What a person who holds `read` (and not `write`) gets from an agent that published no `ask` of
# its own: one confined "read mode", built here from the agent and pinned exactly by
# `tests/test_default_read_profile.py` so a code change that widens every agent at once cannot
# pass unnoticed. Spec: `docs/superpowers/specs/2026-11-03-agent-read-mode-and-doors-design.md`.

#: The two names of read mode: `read` (the default, canopy's own profile) and `ask` (an agent's
#: own published override, and every turn stamped before read mode existed).
READ_MODE = "read"
ASK_MODE = "ask"
READ_MODE_NAMES = (ASK_MODE, READ_MODE)


def is_read_mode(name: str | None) -> bool:
    """THE one place that says `read` and `ask` are the same thing. The decision, `opens`,
    `profile`, the envelope and admission matching all ask this; the enforcement layers never
    read a capability's name, so it is a decision-time alias only. Stored names are never
    rewritten."""
    return name in READ_MODE_NAMES


#: Bump on ANY change to what `default_read_profile` returns; the envelope records it.
READ_PROFILE_VERSION = 1

#: The skill whose presence in the agent's published catalog gives the default its entry.
ANSWER_CALLER_SKILL = "answer-caller"

# github.com/<owner>/<repo>[.git] (https or ssh); anything else means "no readable repo".
_GITHUB = re.compile(r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_REF = re.compile(r"^[A-Za-z0-9._-]+$")


def _github_repo(agent) -> tuple[str, str] | None:
    m = _GITHUB.search((getattr(agent, "repo_url", "") or "").strip())
    return (m["owner"], m["repo"]) if m else None


def _repo_reads(agent) -> list[str]:
    """Unauthenticated `curl` reads of the agent's own repo, and nothing else. `gh` is not logged
    in inside a confined session, so these are plain GET patterns against the public API."""
    found = _github_repo(agent)
    if found is None:
        return []
    owner, repo = found
    ref = (getattr(agent, "repo_ref", "") or "main").strip()
    ref = ref if _REF.match(ref) else "main"
    api = f"https://api.github.com/repos/{owner}/{repo}"
    return [
        f'curl -sS -H "Accept: application/vnd.github.raw" {api}/contents/*',
        f"curl -sS {api}/contents/*",
        f"curl -sS {api}/git/trees/{ref}?recursive=1",
        f"curl -sS https://raw.githubusercontent.com/{owner}/{repo}/{ref}/*",
    ]


def default_read_profile(agent, *, skills=None) -> dict:
    """The confined profile read mode runs in when the agent publishes no `ask` override.

    A pure function of the agent: its slug (the scratch folder and its mail script), whether it has
    a mailbox (`email`), its GitHub repo (`repo_url`/`repo_ref`) and whether its published skill
    catalog has `answer-caller` (`skills`: the bare skill names; read from `agent.skills` when not
    given). Same fields as a published capability, so the runner, the guard and the caller token
    read it unchanged.
    """
    slug = agent.slug
    scratch = f"{{cwd}}/.{slug}-read/*"
    names = set(skills) if skills is not None else set(agent.skills.values_list("name", flat=True))
    bash: list[str] = []
    if (getattr(agent, "email", "") or "").strip():
        bash += [
            "canopy email read --repo . {thread_id}",
            f"canopy email review-receipt --repo . --body-file {scratch}",
            f"bin/{slug}-email --reply-all --thread-id {{thread_id}} "
            f"--subject-file {scratch} --body-file {scratch}",
        ]
    bash += _repo_reads(agent)
    return {
        "description": f"Ask {slug} a question. It answers from its own repo and docs; "
                       "it cannot change anything but draft its reply.",
        "entry": (f"/{slug}:{ANSWER_CALLER_SKILL} --thread {{thread_id}}"
                  if ANSWER_CALLER_SKILL in names else None),
        "tools": ["Read", "Grep", "Glob", "Write", "mcp__*canopy-web__who_is_asking"],
        "bash": bash,
        "read_paths": ["{cwd}/**"],
        "write_paths": [scratch],
        "pages": [],
        "sites": [],
        "ceiling": [],
    }
