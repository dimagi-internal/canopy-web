"""A site that speaks for its visitors by signed assertion: a keypair, a workspace with an
agent the site may reach, and a contact token signed for a visitor. Plain helpers,
shared by the contact socket, runs, metadata and widget suites.
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.tokens import assertions
from apps.tokens.models import AppCredential, AppCredentialAgent
from apps.workspaces.models import Workspace, WorkspaceMembership


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


def _world():
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    priv, pub = _keypair()
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.workspace = ws
    app.public_keys = [pub]
    app.save(update_fields=["workspace", "public_keys"])
    AppCredentialAgent.objects.create(app=app, agent=agent)
    return owner, ws, app, priv


def _contact_token(priv, sub="u-42"):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    assertion = jwt.encode(
        {"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
         "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())},
        priv, algorithm="EdDSA",
    )
    return Client().post("/api/auth/contact-token", data={"assertion": assertion},
                         content_type="application/json").json()["token"]
