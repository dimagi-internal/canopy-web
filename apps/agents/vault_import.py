"""Import an agent's secret VALUES from its 1Password vault into canopy-web.

The goal, stated by Jonathan on 2026-09-05: *someone could plausibly create a
completely new agent without direct access to the cloud box or 1Password.* The
mint (canopy-web#675) removed the box from the mailbox path; this removes the
vault from everything else — canopy-web ends up holding the values, and a person
provisioning a new agent needs neither.

WHY THE `op` CLI AND NOT THE PYTHON SDK. 1Password ships an official Python SDK
that takes a service-account token directly, which is the obvious choice. It has
no cp314 wheel. This deployment's image is python:3.12-slim (so it would install
in production) while a local checkout resolves to 3.14 (so it would not) — a
split that guarantees a test suite green in one place and broken in the other,
for a reason unrelated to anything being tested. The CLI is a static binary with
no Python coupling at all, and it is the SAME tool bootstrap_agents.sh already
runs on the box, so `op://` refs with spaces, UUID item names and document
filenames all behave identically in both places rather than nearly identically.

WHAT IS NEVER IMPORTED. A ref the agent declares `local_only` (per-human tokens
minted on a workstation) has no vault copy to read and must not be invented here.
"""
from __future__ import annotations

import os
import subprocess
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


def op_read(ref: str, *, token: str, timeout: int = 30) -> str:
    """Resolve one op:// reference as the given service account.

    The token goes in the ENVIRONMENT, never argv — anything on a command line is
    readable by every other process on the host via /proc.
    """
    env = {**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token}
    res = subprocess.run(
        ["op", "read", ref], capture_output=True, text=True, env=env, timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "op read failed").strip().splitlines()[-1][:300])
    return res.stdout.rstrip("\n")


def resolve_items(items: list[ImportItem], *, token: str, reader=None) -> tuple[dict, list[dict]]:
    """Run the plan. Returns (values to store, per-ref failures).

    One ref failing must not abandon the other forty-four: a vault that is missing
    a single item is the normal state of a half-provisioned agent, and an
    all-or-nothing import would make it unprovisionable. Failures are REPORTED,
    which is the part that makes a stale ref visible instead of silent.
    """
    # Resolved at CALL time, not bound as a default: a default argument captures
    # op_read at import, which silently makes the real path unsubstitutable and
    # means a test that thinks it is exercising this loop is shelling out to a
    # binary instead. Caught by its own test.
    reader = reader or op_read

    values: dict[str, str] = {}
    failures: list[dict] = []
    for item in items:
        if item.kind == "value":
            values[item.name] = item.value
        elif item.kind == "op":
            try:
                got = reader(item.ref, token=token)
            except Exception as exc:  # noqa: BLE001 - reported per ref, never fatal
                failures.append({"name": item.name, "ref": item.ref, "error": str(exc)})
                continue
            if got:
                values[item.name] = got
            else:
                # An empty read is a MISS, not a value. Storing "" would make the
                # screen show the ref as set while it resolves to nothing.
                failures.append({"name": item.name, "ref": item.ref, "error": "resolved empty"})
    return values, failures
