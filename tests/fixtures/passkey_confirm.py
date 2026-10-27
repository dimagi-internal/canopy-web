"""Confirming an act-as-you permission with a passkey, as the panel does: a real
WebAuthn assertion (`apps.passkeys.testing.SoftAuthenticator`) through the panel's
own token, at `/api/embed/host-grant/confirm/begin|finish`."""
from __future__ import annotations

import json

from django.test import Client

from apps.passkeys.models import Passkey
from apps.passkeys.testing import SoftAuthenticator


def give_passkey(user) -> SoftAuthenticator:
    """Register a software passkey for `user` and return it."""
    auth = SoftAuthenticator()
    Passkey.objects.create(user=user, credential_id=auth.id, public_key=auth._cose_public_key(),
                           sign_count=0, label="test")
    return auth


def panel(raw_token: str) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw_token}")


def begin(raw_token: str):
    return panel(raw_token).post("/api/embed/host-grant/confirm/begin", content_type="application/json")


def finish(raw_token: str, credential: dict):
    return panel(raw_token).post("/api/embed/host-grant/confirm/finish",
                                 data=json.dumps({"credential": credential}),
                                 content_type="application/json")


def confirm(raw_token: str, auth: SoftAuthenticator):
    """The whole ceremony: begin, sign the challenge, finish. Returns finish's response."""
    started = begin(raw_token)
    assert started.status_code == 200, started.content
    return finish(raw_token, auth.get(started.json()["options"]))
