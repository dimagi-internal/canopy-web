"""The tenant's shared 1Password vault (spec 2026-09-07)."""
from __future__ import annotations


def set_shared_vault(workspace, *, vault=None, service_key=None):
    """Set the workspace's shared vault name and/or its service-account token.

    The sibling of agents.services.set_agent_vault one level up, and
    non-clobbering on the key for the same reason: renaming a vault must not
    wipe the credential that reads it.

    The key belongs HERE rather than on any agent because what it unlocks is
    shared by definition — the gog OAuth clients every agent in the tenant uses.
    Keeping it off the agents is what preserves the per-agent split's property
    (Agent.op_vault, 2026-09-06): no single stored credential reads both this
    vault and an agent's own.
    """
    from apps.common.encryption import encrypt_secret

    fields = []
    if vault is not None:
        workspace.shared_op_vault = vault.strip()
        fields.append("shared_op_vault")
    if service_key and service_key.strip():
        workspace.shared_op_sa_token_enc = encrypt_secret(service_key.strip())
        fields.append("shared_op_sa_token_enc")
    if fields:
        workspace.save(update_fields=[*fields, "updated_at"])
    return shared_vault_status(workspace)


def shared_vault_status(workspace):
    """Masked status — the name, and WHETHER a key is set. Never the key."""
    from .schemas import SharedVaultOut

    source = workspace.shared_vault_source() or workspace
    return SharedVaultOut(
        vault=source.shared_op_vault,
        key_set=bool(source.shared_op_sa_token_enc),
        inherited_from="" if source.pk == workspace.pk else source.slug,
    )
