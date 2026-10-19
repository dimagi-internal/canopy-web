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

    def addresses(self, agent) -> list[dict]: ...

    def reply(self, agent, *, thread_id: str, to: str, subject: str = "", body: str) -> str: ...


class ReplyRefused(Exception):
    """Nothing was sent: hosted mail is not installed, or it refused the reply."""


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


def addresses(agent) -> list[dict]:
    """Every address the agent has (`[{"address", "backend", "primary"}]`); without a
    provider, just its own, as a Google mailbox."""
    if _provider is not None:
        return _provider.addresses(agent)
    email = (agent.email or "").strip().lower()
    return [{"address": email, "backend": "gmail", "primary": True}] if email else []


def reply(agent, *, thread_id: str, to: str, subject: str = "", body: str) -> str:
    """Reply on a canopy-hosted thread as the agent, from the thread's own address.
    Returns the sent Message-ID; raises ReplyRefused when nothing was sent."""
    if _provider is None:
        raise ReplyRefused("hosted mail is not installed")
    return _provider.reply(agent, thread_id=thread_id, to=to, subject=subject, body=body)
