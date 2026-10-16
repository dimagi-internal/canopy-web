"""Test helpers: grant a step-up to a test client, and a software authenticator.

`grant_step_up(client)` is what a test of a step-up-gated route uses when the
passkey ceremony is not what it is testing. `SoftAuthenticator` is a real (if
minimal) WebAuthn authenticator — an EC P-256 key producing `none` attestations
and ES256 assertions — so the ceremony tests run py_webauthn's actual
verification rather than a mock of it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from apps.passkeys.stepup import SESSION_KEY, STEP_UP_SECONDS


def grant_step_up(client, *, seconds: int = STEP_UP_SECONDS) -> None:
    """Mark a force_login'ed Django test client's session as freshly stepped up.
    A client that is not signed in is left alone (there is no one to step up)."""
    session = client.session
    if "_auth_user_id" not in session:
        return
    session[SESSION_KEY] = {"user": int(session["_auth_user_id"]), "until": time.time() + seconds}
    session.save()


def _b64u(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftAuthenticator:
    """One passkey on one fake device."""

    def __init__(self, *, rp_id: str = "testserver", origin: str = "http://testserver") -> None:
        from cryptography.hazmat.primitives.asymmetric import ec

        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0

    @property
    def id(self) -> str:
        return _b64u(self.credential_id)

    def _cose_public_key(self) -> bytes:
        import cbor2

        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, options: dict) -> bytes:
        return json.dumps({
            "type": kind, "challenge": options["challenge"], "origin": self.origin, "crossOrigin": False,
        }).encode()

    def create(self, options: dict, *, flags: int = 0x45) -> dict:
        """navigator.credentials.create() — flags default UP|UV|AT."""
        import cbor2

        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.sign_count.to_bytes(4, "big")
            + b"\x00" * 16 + len(self.credential_id).to_bytes(2, "big") + self.credential_id
            + self._cose_public_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": self.id, "rawId": self.id, "type": "public-key",
            "response": {
                "clientDataJSON": _b64u(self._client_data("webauthn.create", options)),
                "attestationObject": _b64u(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {}, "authenticatorAttachment": "platform",
        }

    def get(self, options: dict, *, flags: int = 0x05) -> dict:
        """navigator.credentials.get() — flags default UP|UV."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        self.sign_count += 1
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.sign_count.to_bytes(4, "big")
        )
        client_data = self._client_data("webauthn.get", options)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": self.id, "rawId": self.id, "type": "public-key",
            "response": {
                "clientDataJSON": _b64u(client_data),
                "authenticatorData": _b64u(auth_data),
                "signature": _b64u(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {}, "authenticatorAttachment": "platform",
        }
