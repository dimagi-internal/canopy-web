from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import Field

from apps.common.schemas import StrictModel


class PasskeyOut(StrictModel):
    id: int
    #: The WebAuthn credential id (base64url) — matches a vault keyset's
    #: `passkeys[].credential_id`, so a page can say which passkey unlocks the vault.
    credential_id: str
    label: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
    #: Synced through a password manager (iCloud Keychain…) rather than device-bound.
    backed_up: bool
    #: Reported the WebAuthn PRF extension at registration — can hold a vault
    #: key seal (canopy-web#1550). None: registered before that was asked.
    prf_enabled: bool | None = None


class PasskeyOptionsOut(StrictModel):
    #: PublicKeyCredentialCreationOptions / RequestOptions in their JSON form
    #: (binary fields base64url) — what navigator.credentials.create/get take
    #: once those fields are decoded.
    options: dict[str, Any]


class PasskeyRegisterIn(StrictModel):
    #: The browser's PublicKeyCredential, serialized to JSON (base64url fields).
    credential: dict[str, Any]
    label: str = Field(default="", max_length=120)


class PasskeyAssertIn(StrictModel):
    credential: dict[str, Any]


class StepUpStatusOut(StrictModel):
    #: A passkey was confirmed on this browser session within the window.
    active: bool
    until: dt.datetime | None = None
    has_passkey: bool
    #: Only a person at a browser can step up; a token never can.
    browser: bool
    window_seconds: int


# ---- the person's zero-knowledge vault keyset (canopy-web#1550) --------------

class VaultSetupOut(StrictModel):
    #: The SPA must ask for a passkey and a vault key before anything else: a
    #: person at a browser with no vault key yet. False for a token or a
    #: token-minted session, which can never make one.
    required: bool
    has_passkey: bool
    has_vault_key: bool



class VaultKeysetIn(StrictModel):
    #: EC P-256 PUBLIC JWK {kty, crv, x, y}.
    public_jwk: dict[str, Any]
    #: Every seal to keep — the list is replaced. docs/vault/zk-v1 §4.
    seals: list[dict[str, Any]]
    #: Start a NEW key pair (drops every member wrap to the old one).
    replace: bool = False


class VaultKeysetPasskeyOut(StrictModel):
    credential_id: str
    label: str
    #: The authenticator reported PRF support at registration (None: unknown).
    prf_enabled: bool | None = None
    #: The keyset has a seal for this passkey.
    sealed: bool


class VaultKeysetOut(StrictModel):
    #: The caller's user id as a string — bound into the seal and member wraps.
    user_id: str
    #: base64url SHA-256("canopy-vault/v1/prf|{user_id}") — `prf.eval.first`.
    prf_input: str
    exists: bool
    public_jwk: dict[str, Any] | None = None
    seals: list[dict[str, Any]] = Field(default_factory=list)
    passkeys: list[VaultKeysetPasskeyOut] = Field(default_factory=list)
    updated_at: dt.datetime | None = None
