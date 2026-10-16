"""A person's passkeys — the WebAuthn credentials canopy-web asks for a step-up.

Canopy Vault (canopy-web#1538). A passkey is never a way to SIGN IN here (that
stays Google OAuth); it is the second factor a person presents, at a browser,
before the few actions that hand over or change a secret: revealing an agent
credential, setting one from the browser, approving a runner's device key.

One row per authenticator, several per person (a laptop's Touch ID, a phone, a
security key). The public key is stored; nothing secret is.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Passkey(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="passkeys",
    )
    #: The authenticator's credential id, base64url without padding — the form the
    #: browser reports it in, so a lookup never has to re-encode.
    credential_id = models.CharField(max_length=1024, unique=True)
    #: COSE-encoded public key, exactly as `verify_registration_response` returns it.
    public_key = models.BinaryField()
    #: The authenticator's signature counter. Platform passkeys report 0 forever;
    #: a hardware key that goes BACKWARDS is a cloned key, which py_webauthn refuses.
    sign_count = models.PositiveBigIntegerField(default=0)
    #: What the browser said it can reach the authenticator over ("internal",
    #: "hybrid", "usb"…), echoed back in allowCredentials so it picks the right one.
    transports = models.JSONField(default=list, blank=True)
    #: The person's own name for it ("MacBook Touch ID").
    label = models.CharField(max_length=120, blank=True)
    aaguid = models.CharField(max_length=64, blank=True)
    #: Synced (iCloud Keychain, Google Password Manager) rather than device-bound.
    backed_up = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["user_id", "created_at"]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.label or 'passkey'} ({self.user_id})"
