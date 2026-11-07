"""Read mode, and the empty published document every agent starts with.

Kept apart from `published_profiles.py` so the Agent model can use `default_interface` as a field default
without importing the whole access machinery: no host site and no read-mode override, until its owner
changes them. See `apps/agents/published_profiles.py` and `docs/architecture/access.md`.
"""
from __future__ import annotations

def default_interface() -> dict:
    """No host site and no read-mode override. A fresh dict every call."""
    return {"capabilities": {}}


# --- the default read profile ---------------------------------------------------------------
#
# What a person who holds `read` (and not `write`) gets from an agent that published no read mode of
# its own: one confined "read mode", built here from the agent and pinned exactly by
# `tests/test_default_read_profile.py` so a code change that widens every agent at once cannot
# pass unnoticed. Spec: `docs/superpowers/specs/2026-11-03-agent-read-mode-and-doors-design.md`.

#: The two stored names of read mode: `read` (the default, canopy's own profile) and
#: `READ_MODE_STAMP` (an agent's own published override, and every turn stamped before read mode
#: existed). `READ_MODE_STAMP` is the ONE place that older name lives: it is a stored and wire value
#: (`Turn.capability`, an `AgentGrant`/`AdmissionRule` naming it, the published document's key, the
#: envelope's `capability`), never a word a person reads. New read-mode entries are still written under
#: it, because renaming it would split live conversations (`session_naming.restricted_key`).
READ_MODE_NAME = "read"
READ_MODE_STAMP = "ask"
READ_MODE_NAMES = (READ_MODE_STAMP, READ_MODE_NAME)


def is_read_mode(name: str | None) -> bool:
    """THE one place that says `read` and `READ_MODE_STAMP` are the same thing. The decision, `opens`,
    `profile`, the envelope and admission matching all ask this; the enforcement layers never
    read a profile's name, so it is a decision-time alias only. Stored names are never
    rewritten."""
    return name in READ_MODE_NAMES


#: Bump on ANY change to what `default_read_profile` returns; the envelope records it.
READ_PROFILE_VERSION = 4

#: The skill whose presence in the agent's published catalog gives the default its entry.
ANSWER_CALLER_SKILL = "answer-caller"

#: The scratch folders a read-mode turn may write its draft into. `-read` is the name canopy gives it;
#: the other is the fleet's stored folder name: the fleet template's `answer-caller` skill (ace, echo,
#: eva, hal, trellis) drafts under it, and published read modes list it in `write_paths`, so listing
#: both lets those skills work unchanged. Nothing else is writable.
SCRATCH_SUFFIXES = (READ_MODE_NAME, READ_MODE_STAMP)


def default_read_profile(agent, *, skills=None) -> dict:
    """The confined profile read mode runs in when the agent publishes no read-mode override.

    A pure function of the agent: its slug (the scratch folders and its mail script), whether it has
    a mailbox (`email`) and whether its published skill catalog has `answer-caller` (`skills`: the bare skill names; read from `agent.skills` when not
    given). Same fields as a published profile, so the runner, the guard and the caller token
    read it unchanged.
    """
    slug = agent.slug
    scratch = [f"{{cwd}}/.{slug}-{suffix}/*" for suffix in SCRATCH_SUFFIXES]
    names = set(skills) if skills is not None else set(agent.skills.values_list("name", flat=True))
    bash: list[str] = []
    if (getattr(agent, "email", "") or "").strip():
        bash.append("canopy email read --repo . {thread_id}")
        bash += [f"canopy email review-receipt --repo . --body-file {d}" for d in scratch]
        bash += [f"bin/{slug}-email --reply-all --thread-id {{thread_id}} --subject-file {d} --body-file {d}"
                 for d in scratch]
    return {
        "description": f"Read mode: {slug} answers questions from its own repo and docs, "
                       "and cannot change anything but draft its reply.",
        "entry": (f"/{slug}:{ANSWER_CALLER_SKILL} --thread {{thread_id}}"
                  if ANSWER_CALLER_SKILL in names else None),
        # No Grep or Glob: the cloud runner's Claude Code build has neither ("No such tool
        # available"), so listing them only sends the model reaching for a tool that is not
        # there. It reads its own worktree with `Read` (the session's cwd is a worktree of the
        # agent's repo, so no network read is needed); an agent that wants file search overrides
        # with an explicit bash pattern. `ToolSearch` only loads the schema of a deferred MCP tool
        # (canopy-web exposes hundreds, so `who_is_asking` is deferred); the guard still checks
        # every real tool call against this list.
        "tools": ["Read", "Write", "ToolSearch", "mcp__*canopy-web__who_is_asking"],
        "bash": bash,
        "read_paths": ["{cwd}/**"],
        "write_paths": scratch,
        "sites": [],
        "ceiling": [],
    }
