"""One greppable log line per vault-key event, so "it won't unlock" is debuggable.

Every line is ``vault.<event> key=value …`` on the ``canopy.vault`` logger. It
says WHO (user id), WHAT client (browser, platform, how the request was
authenticated, which authenticator made each passkey) and WHAT the keyset holds
(seal kinds, which passkeys are sealed), and never a secret: no passphrase, no
Secret Key, no seal contents, no PRF output. The browser reports the failures
only it can see (a passphrase that did not open a seal, a passkey that gave no
PRF) to ``POST /api/vault/client-report``, which lands here too.
"""
from __future__ import annotations

import logging
import re

from django.http import HttpRequest

logger = logging.getLogger("canopy.vault")

#: Best-effort names for common passkey providers, by AAGUID
#: (github.com/passkeydeveloper/passkey-authenticator-aaguids). The raw
#: aaguid is always logged too, so an unlisted provider is still identifiable.
AAGUID_NAMES: dict[str, str] = {
    "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
    "fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "iCloud Keychain",
    "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager",
    "adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome on Mac",
    "d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
    "531126d6-e717-415c-9320-3d9aa6981239": "Dashlane",
}

_SAFE = re.compile(r"^[\w.:/@+\-]+$")


def authenticator_name(aaguid: str) -> str:
    return AAGUID_NAMES.get((aaguid or "").lower(), "unknown")


def _val(v: object) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\n", " ").replace("\r", " ")[:300]
    return s if s and _SAFE.match(s) else '"' + s.replace('"', "'") + '"'


def client_of(request: HttpRequest) -> dict[str, str]:
    """What is talking to canopy. A browser says so in its User-Agent (and the
    ``Sec-CH-UA`` hints on Chromium); a token or MCP call says how it
    authenticated instead — the canopy CLI, a runner or an MCP client carries
    no passkey, so this tells "a person at a browser" from "a machine"."""
    meta = getattr(request, "META", {}) or {}
    method = getattr(request, "auth_method", None)
    via_mcp = bool(getattr(request, "via_mcp", False))
    return {
        "kind": "mcp" if via_mcp else ("token:" + str(method) if method else "browser-session"),
        "ua": meta.get("HTTP_USER_AGENT", "")[:200],
        "ch_ua": meta.get("HTTP_SEC_CH_UA", "")[:200],
        "platform": meta.get("HTTP_SEC_CH_UA_PLATFORM", "").strip('"'),
        "origin": meta.get("HTTP_ORIGIN", "")[:100],
    }


def keyset_summary(user) -> dict[str, object]:
    """Seal kinds and passkey states for ``user`` — enough to see why an unlock
    cannot work (e.g. only a ``2skd`` passphrase seal, no ``prf`` seal)."""
    from apps.passkeys.models import Passkey, UserVaultKeyset  # noqa: PLC0415

    ks = UserVaultKeyset.objects.filter(user=user).first()
    seals = list(ks.seals) if ks else []
    sealed = {s.get("cred") for s in seals}
    passkeys = [
        f"{authenticator_name(p.aaguid)}(aaguid={p.aaguid or '-'},prf={p.prf_enabled},"
        f"sealed={p.credential_id in sealed},transports={'/'.join(p.transports or []) or '-'},"
        f"cred={p.credential_id[:8]})"
        for p in Passkey.objects.filter(user=user)
    ]
    return {
        "keyset": ks is not None,
        "seals": ",".join(sorted(str(s.get("alg")) for s in seals)) or "-",
        "passkeys": ";".join(passkeys) or "-",
    }


def log(event: str, request: HttpRequest, *, level: int = logging.INFO, **fields: object) -> None:
    user = getattr(request, "user", None)
    base: dict[str, object] = {"user": getattr(user, "pk", None)}
    base.update(client_of(request))
    if user is not None and getattr(user, "is_authenticated", False):
        try:
            base.update(keyset_summary(user))
        except Exception:  # noqa: BLE001 — a diagnostic must never break the request
            base["keyset_summary"] = "unavailable"
    base.update(fields)
    logger.log(level, "vault.%s %s", event, " ".join(f"{k}={_val(v)}" for k, v in base.items()))
