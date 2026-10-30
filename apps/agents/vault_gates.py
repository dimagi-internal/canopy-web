"""Who may write an agent's vault: the guards its routes share.

An agent's admin is not enough to change its vault — they must also own or administer the
vault itself — and a browser needs a fresh passkey touch (`docs/architecture/vault.md`).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.api.errors import TYPE_NOT_FOUND, ProblemError
from apps.passkeys import stepup

from .credentials import vault_members
from .gates import agent_for_admin, agent_for_read


def vault_manager(request: HttpRequest, slug: str):
    """The agent's admin AND an owner or admin of its vault, as the workspace vault's
    writes are: someone taken out of an unlinked vault must not be able to rotate its key to
    one of their own machines, nor store or delete values in it."""
    agent = agent_for_admin(request, slug)
    if not vault_members.is_admin(vault_members.role_of(vault_members.vault_for_agent(agent), request.user)):
        raise HttpError(403, "this action requires someone who manages this agent's vault")
    return agent


def zk_admin(request: HttpRequest, slug: str, *, browser_only: bool = False):
    agent = vault_manager(request, slug)
    if browser_only or stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    return agent


def agent_vault(request: HttpRequest, slug: str, *, manage: bool):
    agent = agent_for_read(request, slug)
    vault = vault_members.vault_for_agent(agent)
    if vault is None:
        raise ProblemError(404, "No vault", detail="This agent is in no workspace, so it has no vault.",
                           type_=TYPE_NOT_FOUND)
    if manage:
        if not vault_members.is_admin(vault_members.role_of(vault, request.user)):
            raise HttpError(403, "this action requires someone who manages this vault")
        if stepup.is_browser(request):
            stepup.require_recent_step_up(request)
    return agent, vault
