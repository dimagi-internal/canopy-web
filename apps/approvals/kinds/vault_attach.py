"""Kind: serve an agent from a workspace vault.

Stands in for `POST /api/vaults/{id}/attachments` with `{agent}`: the agent then reads the vault's
secrets in its turns, and everyone with full access to the agent joins the vault. The route needs a
manager of the vault who also administers the agent, and a passkey from a browser; this kind asks for
the same, so it carries `step_up` and only the web page can approve it.
"""
from __future__ import annotations

import uuid

from apps.agents.credentials import vault_members, workspace_vaults
from apps.agents.credentials.zk import ZkError
from apps.agents.models import Agent, Vault
from apps.common.schemas import StrictModel

from . import Described, ExecError, Kind, KindError, register


class Params(StrictModel):
    vault_id: str
    agent: str


def _vault(vault_id: str) -> Vault:
    try:
        vault_id = uuid.UUID(str(vault_id))
    except ValueError:
        raise KindError(f"{vault_id!r} is not a vault id") from None
    v = Vault.objects.filter(pk=vault_id, kind=Vault.WORKSPACE).select_related("workspace").first()
    if v is None:
        raise KindError(f"there is no workspace vault {vault_id}")
    return v


def _agent(slug: str) -> Agent:
    agent = Agent.objects.filter(slug=slug).select_related("workspace").first()
    if agent is None:
        raise KindError(f"there is no agent {slug!r}")
    return agent


@register
class VaultAttachAgent(Kind):
    slug = "vault.attach_agent"
    label = "Give an agent a workspace vault"
    params_model = Params
    needs = "someone who manages the vault and administers the agent, with a passkey confirmation"
    does = ("Serves the agent from the workspace vault: its turns read the vault's secrets, and everyone "
            "with full access to the agent becomes a member of the vault. The agent must be in the "
            "vault's workspace or one below it.")
    step_up = True

    def describe(self, p: Params) -> Described:
        v, agent = _vault(p.vault_id), _agent(p.agent)
        name = agent.name or agent.slug
        return Described(
            workspace=v.workspace_id,
            title=f"Give {name} the vault \"{v.name}\"",
            summary=f"{name}'s turns would read the secrets in \"{v.name}\", and everyone with full access "
                    f"to {name} would join that vault as a member.",
            plan=[f"The agent {name} ({agent.workspace_id}) is served from the vault \"{v.name}\" ({v.workspace_id})",
                  "Its turns read the vault's secrets, layered under the agent's own",
                  "People with full access to the agent become members of the vault",
                  "Approving needs your passkey, as attaching on the vault page does"],
            scopes=[f"vault:{v.pk}", f"agent:{agent.slug}", f"workspace:{v.workspace_id}"])

    def may_approve(self, user, p: Params, d: Described) -> bool:
        v, agent = _vault(p.vault_id), _agent(p.agent)
        return vault_members.is_admin(vault_members.role_of(v, user)) and agent.is_admin(user)

    def execute(self, user, p: Params, request) -> dict:
        v, agent = _vault(p.vault_id), _agent(p.agent)
        try:
            row = workspace_vaults.attach(v, agent=agent, by=user)
        except ZkError as exc:
            raise ExecError(str(exc)) from exc
        return {"vault": str(v.pk), "agent": agent.slug, "attachment": row.pk}
