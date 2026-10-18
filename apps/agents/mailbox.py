"""Where an agent's address lives — the seam to whichever app hosts mail.

Framework code (the setup checklist, `/credentials/resolve`, agent registration) needs to
know whether an agent's address is canopy-hosted, and to route a new one, but must not
import the product `mail` app (ARCHITECTURE.md). So the mail app registers a provider here
from its `AppConfig.ready`, the same shape as `move.register`. With no provider (mail not
installed) every address is a Google mailbox, which is what it was before hosted mail.
"""
from __future__ import annotations

from typing import Protocol


class MailboxProvider(Protocol):
    def describe(self, agent) -> dict: ...

    def ensure_route(self, agent, *, by) -> str: ...


_provider: MailboxProvider | None = None


def register(provider: MailboxProvider) -> MailboxProvider:
    global _provider
    _provider = provider
    return provider


def describe(agent) -> dict:
    """`{"kind": "hosted"|"google"|"none", ...}` — the mail app's `agent_mailbox`."""
    if _provider is not None:
        return _provider.describe(agent)
    if not agent.email:
        return {"kind": "none"}
    return {"kind": "google", "address": agent.email.strip().lower()}


def ensure_route(agent, *, by) -> str:
    """Route a hosted address to the agent; a no-op ("not-hosted") without a provider."""
    return _provider.ensure_route(agent, by=by) if _provider is not None else "not-hosted"
