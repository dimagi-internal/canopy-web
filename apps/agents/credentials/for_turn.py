"""What one turn really gets, by name and kind, never a value: `TurnContext.credentials`.

Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md` § 3.3, stage 3. `authority.py` is a
static table by kind and can't say this alone: what a turn gets also depends on
`turn_agent_tokens.refusal`, `delegations.github_refusal` (a review's `reads_for` gets no GitHub
identity), `caller_tokens.is_caller_scoped` (an editor's whole-agent turn gets a read-only token that
reads as the asker) and `interface.page_turn` (the agent's own login is withheld on a site's page).

This asks those same functions and states the result. **It adds no rule**, and any answer that needs the
runner is `PENDING`. It only reads, and reads the turn as it would be stamped, on a copy.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from . import authority

#: Whether the turn gets it.
YES, NO, PENDING = "yes", "no", "pending"


@dataclass(frozen=True)
class CredentialLine:
    name: str
    kind: str
    gets: str
    why: str = ""


_LINES = (("canopy_login", authority.AGENT_ONLY), ("github_identity", authority.DELEGATED),
          ("caller_token", "caller"), ("agent_vault", authority.VAULT))


def _on_a_runner(available) -> str:
    """Handed out only once a box has claimed the turn: this statement never promises a box."""
    return YES if available else PENDING


def statement(context, agent, turn, *, runner=None) -> tuple[CredentialLine, ...]:
    from apps.harness import caller_tokens, turn_agent_tokens  # noqa: PLC0415 — they ask the rule
    from apps.harness.models import Turn  # noqa: PLC0415 — harness imports this package

    from . import delegations  # noqa: PLC0415 — it reads harness modules that ask the rule

    if context.refused:
        return tuple(CredentialLine(name, kind, NO, "a refused turn does not run")
                     for name, kind in _LINES)
    if turn is None:
        return _without_a_turn(context)
    stamped = copy.copy(turn)
    stamped.capability = context.stamped or ""
    placement = authority.DIRECT if turn.origin == Turn.ORIGIN_LOCAL else authority.RUNNER

    lines = []
    why_not = turn_agent_tokens.refusal(stamped)
    lines.append(CredentialLine("canopy_login", authority.AGENT_ONLY,
                                NO if why_not else _on_a_runner(runner is not None), why_not))
    why_not = delegations.github_refusal(stamped)
    lines.append(CredentialLine("github_identity", authority.DELEGATED,
                                NO if why_not else _on_a_runner(runner is not None), why_not))
    lines.append(_caller_token(context, stamped, caller_tokens))
    # A confined turn's environment is an allowlist that never holds the agent's `.env` (the runner's
    # `_confined_env`). That is the runner's enforcement of `access == confined`, stated here.
    if context.stamped:
        lines.append(CredentialLine("agent_vault", authority.VAULT, NO,
                                    "a confined turn's environment never holds the agent's own values"))
    else:
        lines.append(CredentialLine("agent_vault", authority.VAULT,
                                    _on_a_runner(runner is not None) if authority.hands_out(placement, authority.VAULT)
                                    else NO, authority.stance(placement, authority.VAULT).why))
    return (*lines, *_withheld_on_page(stamped, agent))


def _caller_token(context, stamped, caller_tokens) -> CredentialLine:
    if context.stamped:
        return CredentialLine("caller_token", "caller", YES, "the tools of its profile, as the asker")
    if caller_tokens.is_caller_scoped(stamped):
        return CredentialLine("caller_token", "caller", YES, "every tool, reading as the asker")
    return CredentialLine("caller_token", "caller", NO, "it acts as the agent, or as no one")


def _withheld_on_page(stamped, agent) -> list[CredentialLine]:
    from .. import interface  # noqa: PLC0415 — interface reads models

    page = interface.page_turn(stamped, agent) if agent is not None else None
    if page is None:
        return []
    return [CredentialLine(name, authority.AGENT_ONLY, NO,
                           f"the agent's own login at {page['site']}: on that site's page it reaches the "
                           "site only as the person, under their act-as-you permission")
            for name in page["withheld"]]


def _without_a_turn(context) -> tuple[CredentialLine, ...]:
    """A preview. A confined answer is known already; the whole agent's waits for the turn."""
    gets, why = (NO, "a caller's turn is never given them") if context.stamped else (PENDING, "")
    return (CredentialLine("canopy_login", authority.AGENT_ONLY, gets, why),
            CredentialLine("github_identity", authority.DELEGATED, gets, why),
            CredentialLine("caller_token", "caller", YES if context.stamped else PENDING),
            CredentialLine("agent_vault", authority.VAULT, gets, why))
