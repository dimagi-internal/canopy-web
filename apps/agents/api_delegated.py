"""A person's own credentials delegated to agents: see them, and withdraw them.

A delegated credential (`credentials.accounts`) is a standing credential from YOUR own
account — your GitHub token, given to ACE, or your Google account, given to Bailey — sealed so that only the machines that run
the agent can open it. You cannot read it back, nor can anyone else; you can see which
agent holds it, which machines can open it (and whose they are), what the provider said
about it, and withdraw it. It is your own, like a token, so it is read by person, not
per organisation: you can withdraw it after you stop being the agent's admin, or leave
its workspace.

    GET    /api/me/delegated-credentials/
    GET    /api/me/connected-accounts/   (just which accounts, for pages: dynamic views)
    DELETE /api/me/delegated-credentials/{id}
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError
from apps.api.route_gates import gate
from apps.events import audit

from .credentials import accounts
from .models import AgentCredential
from .schemas import MyConnectedAccountOut, MyDelegatedCredentialOut

me_router = Router(auth=session_auth, tags=["agents"])


def _mine(user):
    return (AgentCredential.objects.filter(account="delegated", delegated_by=user)
            .select_related("agent", "delegated_by", "delegated_by_agent").order_by("agent__slug", "name"))


@me_router.get("/me/delegated-credentials/", response=list[MyDelegatedCredentialOut],
               summary="The credentials from my own accounts that I delegated to agents")
@gate("self")
def list_my_delegated_credentials(request: HttpRequest) -> list[MyDelegatedCredentialOut]:
    """Every credential you delegated to an agent: which agent, which provider and
    account, which machines can open it (only those — nobody can read it in canopy,
    you included), what the provider last said, and whether it is in force (you are
    still one of the agent's admins). Never the value."""
    return [MyDelegatedCredentialOut(id=c.pk, agent=c.agent.slug, agent_name=c.agent.name,
                                     **accounts.describe(c, c.agent))
            for c in accounts.drop_ended_sessions(list(_mine(request.user)))]


@me_router.get("/me/connected-accounts/", response=list[MyConnectedAccountOut],
               summary="Which of my own accounts agents can use")
@gate("self")
def list_my_connected_accounts(request: HttpRequest) -> list[MyConnectedAccountOut]:
    """The accounts you connected to agents — agent, provider, account, in force — and
    nothing else about the credential. Safe to show on a page (a dynamic view reads it
    to say "Connected"); the full record is GET /me/delegated-credentials/."""
    out = []
    for c in accounts.drop_ended_sessions(list(_mine(request.user))):
        identity = c.identity or {}
        out.append(MyConnectedAccountOut(
            id=c.pk, agent=c.agent.slug, provider=c.provider,
            account=str(identity.get("email") or identity.get("login") or ""),
            in_force=accounts.serves(c, c.agent), session_until=accounts.session_until(c)))
    return out


@me_router.delete("/me/delegated-credentials/{int:credential_id}", response={204: None},
                  summary="Withdraw a credential I delegated to an agent")
@gate("self", setting=True)
def withdraw_my_delegated_credential(request: HttpRequest, credential_id: int) -> Status:
    """Delete it from the agent's vault: no turn gets it from the next one on. A turn
    already running keeps it until it ends, and a machine that opened it may have kept a
    copy, so revoke it at the provider too (for GitHub,
    github.com/settings/personal-access-tokens; for Google, myaccount.google.com/connections)
    — the only true recall."""
    entry = _mine(request.user).filter(pk=credential_id).first()
    if entry is None:
        raise ProblemError(404, "No such credential", type_=TYPE_NOT_FOUND)
    audit.note_change(scope=f"delegated-credential:{entry.agent.slug}/{entry.name}",
                      before={"agent": entry.agent.slug, "name": entry.name, "provider": entry.provider},
                      after=None, summary=f"withdrew the {entry.provider} credential they delegated to "
                                          f"{entry.agent.slug}")
    entry.delete()
    return Status(204, None)
