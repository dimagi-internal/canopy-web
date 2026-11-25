"""One workspace vault's log: what changed and who read what, newest first.

Nothing new is recorded here; it reads three records that already exist and were never
shown on the vault's page:

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

from django.db.models import Count, Max

from apps.events.models import SettingChange
from apps.harness.models import RunnerCredentialRead

from ..models import AgentCredentialRead, Vault, VaultSecret

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


def secret_rows(vault: Vault, *, activity: bool) -> list[dict]:
    """Each secret's name and when it last changed; with `activity` (the Log's audience) also by
    whom, and its reads — counted from the same records `entries` shows (runner reads by vault,
    agent reads by `<vault name>/<secret>`)."""
    rows = VaultSecret.objects.filter(vault=vault).select_related("updated_by").order_by("name")
    if not activity:
        return [{"name": s.name, "updated_at": s.updated_at} for s in rows]
    reads: dict[str, tuple] = {}

    def add(name: str, last, n: int) -> None:
        prev_last, prev_n = reads.get(name, (None, 0))
        reads[name] = (max(filter(None, (prev_last, last)), default=None), prev_n + n)

    for r in (RunnerCredentialRead.objects.filter(vault=vault).values("name")
              .annotate(last=Max("created_at"), n=Count("id"))):
        add(r["name"], r["last"], r["n"])
    prefix = f"{vault.name}/"
    for r in (AgentCredentialRead.objects.filter(name__startswith=prefix).values("name")
              .annotate(last=Max("created_at"), n=Count("id"))):
        add(r["name"][len(prefix):], r["last"], r["n"])
    return [{"name": s.name, "updated_at": s.updated_at,
             "updated_by": (s.updated_by.email or "") if s.updated_by_id else "",
             "last_read_at": reads.get(s.name, (None, 0))[0], "read_count": reads.get(s.name, (None, 0))[1]}
            for s in rows]
