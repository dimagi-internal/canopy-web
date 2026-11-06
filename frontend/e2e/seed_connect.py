"""E2E seed for agent-connect.spec.ts: the workspace's Salesforce app, and one admin per
scenario, each with an agent of their own that runs on one cloud machine. Run after seed.py
via `manage.py shell -c`.

The Salesforce the app points at is the fake one `fake_salesforce.py` serves.

An admin who finishes a connect needs a vault key their browser opens with a PRF passkey,
and the agent's machine vouched for by that same key (what approving the machine does), or
the Connected page has nobody it may store the sign-in for. A PRF seal can only be made by
the authenticator, so the key is made here with a passphrase + Secret Key seal (its private
half known to this script, which signs the vouch), and the test adds the passkey to it the
way a person does, under Settings. The Secret Key goes to .auth/<name>-secret-key.txt.

`connect-noprf` instead starts with a key nobody can open (like vault-new-key.spec.ts's
people): the test replaces it with one held by a passkey alone, then that passkey stops
giving PRF output, so the key cannot open at all.
"""
import os

from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone

from apps.agents.credentials import zk
from apps.agents.models import Agent, AgentVaultEndorsement
from apps.connections.models import Connection, ConnectionApp
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.models import Passkey, UserVaultKeyset
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace
from tests.fixtures.machines import a_machine
from tests.fixtures.vault_zk import ref

PASSPHRASE = "connect e2e passphrase"
User = get_user_model()
ws = Workspace.objects.get(slug="dimagi")

app = ConnectionApp.objects.filter(workspace=ws, provider="salesforce").first() or ConnectionApp(
    workspace=ws, provider="salesforce")
app.client_id = "3MVG-e2e-client"
app.client_secret = "e2e-client-secret"
app.options = {"login_url": "https://login.salesforce.com", "access_token_seconds": 1800}
app.removed_at = None
app.save()


def _b64u(n):
    return ref.b64u(os.urandom(n))


def _mint(u):
    st = SessionStore()
    st["_auth_user_id"] = str(u.pk)
    st["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
    st["_auth_user_hash"] = u.get_session_auth_hash()
    st.set_expiry(24 * 3600)
    st.save()
    return st.session_key


# (person, agent slug, agent name, how their key starts)
PEOPLE = [("connect-ok", "eva", "Eva", "known"), ("connect-retry", "iris", "Iris", "known"),
          ("connect-noprf", "bea", "Bea", "lost")]
auth = os.path.join("frontend", "e2e", ".auth")
os.makedirs(auth, exist_ok=True)
for name, slug, title, start in PEOPLE:
    person, _ = User.objects.get_or_create(username=f"e2e-{name}", defaults={"email": f"e2e-{name}@dimagi.com"})
    wsvc.ensure_member(ws, person)
    # A database kept from an earlier run: start them over.
    Passkey.objects.filter(user=person).delete()
    agent, _ = Agent.objects.update_or_create(slug=slug, defaults=dict(
        name=title, email=f"{slug}@dimagi-ai.com", description=f"{title}, connected in e2e.",
        persona="An agent for the connect e2e.", workspace=ws, owner=person))
    Connection.objects.filter(agent=agent).delete()
    AgentVaultEndorsement.objects.filter(agent=agent).delete()

    if start == "known":
        priv = ec.generate_private_key(ec.SECP256R1())
        scalar = priv.private_numbers().private_value.to_bytes(32, "big")
        secret_key = os.urandom(16)
        seal = ref.seal_user_key_2skd(scalar, person.pk, PASSPHRASE, secret_key, os.urandom(16), os.urandom(12))
        UserVaultKeyset.objects.update_or_create(user=person, defaults={
            "public_jwk": ref.jwk(priv.public_key()), "seals": [seal]})
        with open(os.path.join(auth, f"{name}-secret-key.txt"), "w") as f:
            f.write(ref.secret_key_code(secret_key))
    else:
        UserVaultKeyset.objects.update_or_create(user=person, defaults={
            "public_jwk": {"kty": "EC", "crv": "P-256", "x": _b64u(32), "y": _b64u(32)},
            "seals": [{"alg": "2skd", "kdf": "pbkdf2-sha256", "i": 650000, "s": _b64u(16), "n": _b64u(12),
                       "ct": _b64u(48)}]})
        priv = None

    # The agent's one machine: a cloud box the admin runs, its keys approved.
    runner, _ = Runner.objects.update_or_create(name=f"{slug}-cloud", defaults=dict(
        kind=Runner.CLOUD, host=f"{slug}-cloud", owner=person, workspace=ws, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now()))
    RunnerDeviceKey.objects.filter(runner=runner).delete()
    RunnerAssignment.objects.get_or_create(agent=agent, runner=runner, source="", actor="", defaults={"rank": 0})
    key, _sign, _ecdh = a_machine(runner, person)
    if priv is not None:
        # The admin vouched for it with their own key, as approving it does.
        e = ref.endorse(agent.pk, "device", key.ecdh_jkt, zk.public_key(key.ecdh_public_jwk), priv, person.pk)
        AgentVaultEndorsement.objects.create(agent=agent, kind="device", target=key.ecdh_jkt, endorser=person,
                                             key_jkt=e["key_jkt"], endorser_jkt=e["by_jkt"], sig=e["sig"])

    with open(os.path.join(auth, f"{name}.txt"), "w") as f:
        f.write(_mint(person))
    # A later visit, for after registering a passkey (which counts as a step-up for that session).
    with open(os.path.join(auth, f"{name}-later.txt"), "w") as f:
        f.write(_mint(person))

print("seeded connect: " + ", ".join(f"{n} admin of {s}" for n, s, _, _ in PEOPLE))
