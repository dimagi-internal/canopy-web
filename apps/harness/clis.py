"""Which agent CLI runs a turn: the vocabulary, what a runner reports it has, and
the one rule that picks (spec 2026-10-26-multi-cli-agents).

A CLI is the program a turn runs in — Claude Code or OpenAI's Codex. It is not a
surface (`Runner.engine`: where a laptop opens a session) and never inferred from
one. An agent allows an ordered list (`Agent.clis`); the claim picks one for each
turn and records it (`Turn.cli`); a conversation keeps the one it started on
(`Session.cli`), because its history lives in that CLI's own store.

Imports nothing from the models at module level: both `agents.models` and
`harness.models` read these constants.
"""
from __future__ import annotations

CLAUDE_CODE = "claude-code"
CODEX = "codex"
CLIS: tuple[str, ...] = (CLAUDE_CODE, CODEX)
CHOICES = [(CLAUDE_CODE, "Claude Code"), (CODEX, "Codex")]
#: What an agent, a repo turn or an agentless chat runs on when nothing says otherwise.
DEFAULT: list[str] = [CLAUDE_CODE]
#: The CLIs a conversation holding a host's sensitive data may run on. Only Claude
#: Code has a zero-data-retention runner today (runner_requirements.SENSITIVE_DATA).
SENSITIVE_CLIS = frozenset({CLAUDE_CODE})


def default_list() -> list[str]:
    """A fresh `DEFAULT` for a JSONField default (never the shared list itself)."""
    return list(DEFAULT)
