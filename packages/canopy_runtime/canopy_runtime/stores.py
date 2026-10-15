"""Secret stores — resolve a runtime.yaml secret *reference name* to its value.

The reconciler never holds a secret in the repo or in canopy-web; it asks a store
to resolve a reference (e.g. ``canopy-pat``) at run time. 1Password is the fleet's
single source of truth on both laptop and cloud (see the Agent Runtime Registry
spec), so ``OnePasswordStore`` is the real implementation; ``EnvVarStore`` is a
dependency-free fallback for tests and bare boxes.

Design choices:
  * **`op` CLI, not the async SDK.** The CLI is already installed on the laptop and
    trivially installed on a Linux cloud box, it is synchronous (the runner is
    sync), and it authenticates non-interactively from ``OP_SERVICE_ACCOUNT_TOKEN``
    — the exact token a cloud box carries. No new Python dependency, no asyncio.
  * **Ordered vault search.** A store is built with an ordered vault list
    (``[Agent-<Slug>, Canopy-Shared]``); ``resolve`` returns the first *non-empty*
    hit, so a per-agent secret shadows a shared one and a scaffolded-but-empty
    placeholder is treated as "not provisioned yet" (→ the reconciler surfaces
    "needs bootstrap") rather than a value.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

# Our scaffolded 1Password items (canopy's `canopy agent vault`) store
# the value in a single password field labelled "credential", so a reference
# resolves as op://<vault>/<name>/credential.
CREDENTIAL_FIELD = "credential"


class SecretNotFoundError(Exception):
    """No non-empty value for a reference in any of the store's vaults."""


class SecretStoreError(Exception):
    """The store itself failed (not signed in, op missing, unexpected error)."""


@runtime_checkable
class SecretStore(Protocol):
    def resolve(self, name: str) -> str:
        """Return the secret value for reference ``name`` or raise SecretNotFoundError."""
        ...

    def resolve_optional(self, name: str) -> str | None:
        """Like resolve, but return None instead of raising when absent/empty."""
        ...

    def persist(self, name: str, value: str, *, vault: str | None = None) -> None:
        """Write ``value`` for reference ``name`` (used once, at mint time)."""
        ...


class EnvVarStore:
    """Reads ``CANOPY_SECRET_<NAME>`` from the environment. For tests and boxes
    with no 1Password. Read-only — ``persist`` raises."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._env = environ if environ is not None else os.environ

    @staticmethod
    def _key(name: str) -> str:
        return "CANOPY_SECRET_" + name.upper().replace("-", "_")

    def resolve_optional(self, name: str) -> str | None:
        return self._env.get(self._key(name)) or None

    def resolve(self, name: str) -> str:
        value = self.resolve_optional(name)
        if value is None:
            raise SecretNotFoundError(f"{self._key(name)} not set")
        return value

    def persist(self, name: str, value: str, *, vault: str | None = None) -> None:
        raise NotImplementedError("EnvVarStore is read-only")


class OnePasswordStore:
    """Resolve/persist secrets via the 1Password ``op`` CLI over an ordered vault
    list. Non-interactive when ``OP_SERVICE_ACCOUNT_TOKEN`` is set (a cloud box);
    otherwise it rides an interactive ``op signin`` session (your laptop)."""

    def __init__(
        self,
        vaults: Sequence[str],
        *,
        field: str = CREDENTIAL_FIELD,
        op_bin: str = "op",
        account: str | None = None,
        runner=subprocess.run,
    ) -> None:
        if not vaults:
            raise ValueError("OnePasswordStore needs at least one vault")
        self.vaults = list(vaults)
        self.field = field
        self.op_bin = op_bin
        self.account = account
        self._run = runner

    # -- internals ---------------------------------------------------------
    def _op(self, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [self.op_bin, *args]
        if self.account:
            cmd += ["--account", self.account]
        try:
            return self._run(cmd, capture_output=True, text=True)
        except FileNotFoundError as exc:  # op not installed
            raise SecretStoreError(f"1Password CLI not found: {self.op_bin}") from exc

    def _read(self, vault: str, name: str) -> str | None:
        # `op read` exits non-zero if the item/field is missing; we treat that as
        # "not in this vault, try the next" rather than an error.
        res = self._op(["read", f"op://{vault}/{name}/{self.field}"])
        if res.returncode != 0:
            return None
        value = (res.stdout or "").strip()
        return value or None  # an empty placeholder is "not provisioned yet"

    # -- SecretStore -------------------------------------------------------
    def resolve_optional(self, name: str) -> str | None:
        for vault in self.vaults:
            value = self._read(vault, name)
            if value is not None:
                return value
        return None

    def resolve(self, name: str) -> str:
        value = self.resolve_optional(name)
        if value is None:
            raise SecretNotFoundError(
                f"{name!r} has no value in any of {self.vaults} "
                f"(field {self.field!r}) — mint it or run needs-bootstrap"
            )
        return value

    def persist(self, name: str, value: str, *, vault: str | None = None) -> None:
        # Default to the FIRST vault (the agent's own), never the shared fallback —
        # writing a per-agent minted token into Canopy-Shared would leak it fleet-wide.
        target = vault or self.vaults[0]
        res = self._op(
            ["item", "edit", name, "--vault", target, f"{self.field}={value}"]
        )
        if res.returncode != 0:
            raise SecretStoreError(
                f"failed to persist {name!r} to {target!r}: {res.stderr.strip()}"
            )
