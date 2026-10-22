"""Where an agent's secret values live — the declaration, not the fetching.

canopy-web does NOT resolve these. Jonathan, 2026-09-06: *the service account and
vault should be used on the runner… canopy-web should just store what it needs or
to send to the runner.*

A first version had canopy-web hold a service key, shell out to `op`, and store
all 45 values. That makes canopy-web a second copy of every credential, free to
drift from the vault and worth attacking for the whole set — the thing it was
told twice not to become. The runner already has 1Password access and already
resolves secrets there; what it lacked was WHICH vault, per agent (it derived
`Agent-<Slug>` in bash) and a key scoped to it.

So canopy-web is the custodian: it stores the vault name and the service token,
hands both to the runner over the existing credential-resolve route, and keeps
only the secrets that genuinely originate here — the browser-minted gog-token.
This module is what remains: a read of the declaration, used to say how much of
an agent the runner will be able to resolve.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImportItem:
    """One planned write. `ref` is set iff kind == 'op'."""

    name: str
    kind: str  # "op" | "value" | "skip"
    ref: str = ""
    value: str = ""
    reason: str = ""


def plan_import(declared: list[str], sources: dict) -> list[ImportItem]:
    """Decide what to fetch, what to copy, and what to leave alone.

    Pure — no vault, no network. The interesting decisions all live here so they
    can be asserted without a 1Password account, which is also why they are not
    inlined into the subprocess loop below.
    """
    out: list[ImportItem] = []
    for name in declared:
        src = sources.get(name)
        if not isinstance(src, dict):
            out.append(ImportItem(name, "skip", reason="runtime.yaml does not say where this lives"))
        elif src.get("local_only"):
            # Minted per-human on a workstation. There is no vault copy, and
            # inventing one would make the screen claim a value it does not have.
            out.append(ImportItem(name, "skip", reason="local-only: minted per machine, never in the vault"))
        elif src.get("op"):
            out.append(ImportItem(name, "op", ref=str(src["op"])))
        elif "value" in src:
            # Declared literal — a base URL, an APK version. Not a secret at all,
            # and reading it from a vault would be a round trip to fetch a
            # constant the declaration already carries.
            out.append(ImportItem(name, "value", value=str(src["value"])))
        else:
            out.append(ImportItem(name, "skip", reason="no op: or value: in the declaration"))
    return out
