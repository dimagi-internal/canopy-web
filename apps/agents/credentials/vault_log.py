"""One workspace vault's log: what changed and who read what, newest first.

Nothing new is recorded here; it reads three records that already exist and were never
shown on the vault's page (Jonathan, 2026-11-06: "and then logs"):

* **changes** — every write route on a vault is `@gate(..., setting=True)`, so the audit
  plumbing (`apps/events/audit.py`) leaves a `SettingChange` row naming who, through what
  (browser, CLI, MCP) and what changed. A vault's rows are the ones whose path is under
  `/api/vaults/<id>/`.
* **machine reads** — a runner reading its own Claude credentials from the vault
  (`RunnerCredentialRead`, which names the vault).
* **agent reads** — an agent's turn reading the vault as a layer under its own. The agent's
  read log names those `<vault name>/<secret>` (`credential_resolve`), so they are matched by
  the vault's CURRENT name: a read made before a rename is not shown here (it is still on the
  agent's own log).

Never a value: names, who, when and from where.
"""
from __future__ import annotations

from apps.events.models import SettingChange
from apps.harness.models import RunnerCredentialRead

from ..models import AgentCredentialRead, Vault

_VIA = {"ui": "browser", "mcp": "MCP tool", "cli": "canopy CLI", "api": "API", "runner": "runner"}


def entries(vault: Vault, *, limit: int = 200) -> list[dict]:
    limit = max(1, min(limit, 500))
    out: list[dict] = []
    for c in (SettingChange.objects.filter(path__startswith=f"/api/vaults/{vault.pk}/")
              .order_by("-created_at")[:limit]):
        out.append({"at": c.created_at, "kind": "change", "who": c.actor_email or "someone",
                    "what": c.summary, "where": _VIA.get(c.via, c.via), "turn_id": ""})
    for r in (RunnerCredentialRead.objects.filter(vault=vault).select_related("runner")
              .order_by("-created_at")[:limit]):
        out.append({"at": r.created_at, "kind": "read", "who": r.runner.name,
                    "what": f"read {r.name}", "where": "its own credentials", "turn_id": ""})
    prefix = f"{vault.name}/"
    for r in (AgentCredentialRead.objects.filter(name__startswith=prefix)
              .select_related("agent", "runner", "user").order_by("-created_at")[:limit]):
        where = r.runner.name if r.runner else (r.user.email if r.user else "")
        out.append({"at": r.created_at, "kind": "read", "who": r.agent.slug,
                    "what": f"read {r.name[len(prefix):]}", "where": where, "turn_id": r.turn_id})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:limit]
