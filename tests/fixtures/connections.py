"""What the connections suites share: the fake Salesforce and the fake Google (faked
at the `requests` boundary), a workspace's Salesforce app (`sf_app`), a person's home
workspace (`home`), a person's vault key (`vault_key`: a connection is sealed to it)
a stored connection helper (`_stored`), one sealed to its owner as connect
seals it (`sealed_to_owner`), and what the owner's browser sends to share one
(`browser_share`), and a world to share one in (`ShareWorld`) — plain helpers,
imported by name. This module is also registered in the root conftest's
`pytest_plugins`, for fixtures; it declares none today."""
from __future__ import annotations

import hashlib
import json
from unittest import mock

from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.credentials import zk, zk_seal
from apps.agents.models import Agent, AgentAdmin, AgentVaultEndorsement
from apps.connections import recipients as rcpt
from apps.connections import sharing
from apps.connections.models import Connection, ConnectionApp, ConnectionGrant
from apps.connections.providers import google as _google
from apps.harness.models import Runner, RunnerAssignment
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.machines import a_machine
from tests.fixtures.vault_zk import conn_ref
from tests.fixtures.vault_zk import ref as zk_ref

CLIENT_ID = "3MVG-client"
CLIENT_SECRET = "shh"
SF_OPTIONS = {"login_url": "https://login.salesforce.com", "access_token_seconds": 1800}
TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"
REVOKE_URL = "https://login.salesforce.com/services/oauth2/revoke"
ID_URL = "https://login.salesforce.com/id/00Dorg/005user"
REDIRECT = "https://canopy.example/connections/salesforce/callback"


# ---- the fake Salesforce -----------------------------------------------------

def _resp(status=200, body=None, text=None):
    r = mock.Mock()
    r.status_code = status
    if text is not None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = body if body is not None else {}
    return r


def _token_body(**over):
    body = {
        "access_token": "00D!access_1",
        "refresh_token": "5Aep_refresh_1",
        "instance_url": "https://dimagi.my.salesforce.com",
        "id": ID_URL,
        "token_type": "Bearer",
        "scope": "api refresh_token",
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def _identity_body(**over):
    body = {"organization_id": "00Dorg", "user_id": "005user", "username": "jj@dimagi.com"}
    body.update(over)
    return body


class FakeSF:
    """Routes `requests.post` / `requests.get` by URL; records every call."""

    def __init__(self, token=None, identity=None, revoke=None):
        self.token = token if token is not None else [_resp(body=_token_body())]
        self.identity = identity if identity is not None else _resp(body=_identity_body())
        self.revoke = revoke if revoke is not None else _resp()
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, what):
        if isinstance(what, list):
            what = what.pop(0) if len(what) > 1 else what[0]
        if isinstance(what, Exception):
            raise what
        return what

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
        assert timeout == 15
        self.calls.append(("POST", url, dict(data or {})))
        if url == TOKEN_URL:
            return self._next(self.token)
        if url == REVOKE_URL:
            return self._next(self.revoke)
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, headers=None, timeout=None):
        assert timeout == 15
        self.calls.append(("GET", url, dict(headers or {})))
        return self._next(self.identity)

    def token_calls(self):
        return [c for c in self.calls if c[1] == TOKEN_URL]

    def __enter__(self):
        self._p = mock.patch.multiple(
            "apps.connections.providers.base.requests", post=self.post, get=self.get,
        )
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


class FakeGoogle:
    """Google at the `requests` boundary: the token response carries `expires_in`
    and the granted `scope` as space-separated URLs; userinfo answers for whoever
    `self.user` says signed in."""

    G = "https://www.googleapis.com/auth/"

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.n = 0
        self.granted = f"openid {self.G}userinfo.email {self.G}gmail.readonly {self.G}calendar.readonly"
        self.user = {"sub": "1098", "email": "someone@gmail.com", "email_verified": True}
        self.revoke_status = 200

    def _resp(self, body, status=200):
        r = mock.Mock(status_code=status)
        r.json.return_value = body
        return r

    def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):
        assert timeout == 15
        self.calls.append(("POST", url, dict(data or {})))
        if url == _google.TOKEN_URL:
            self.n += 1
            body = {"access_token": f"ya29.access-{self.n}", "expires_in": 3599, "token_type": "Bearer",
                    "scope": self.granted, "id_token": "eyJ.not.read"}
            if data["grant_type"] == "authorization_code":
                body["refresh_token"] = "1//refresh"
            return self._resp(body)
        if url == _google.REVOKE_URL:
            # Google answers 400 invalid_token for a token already revoked or expired.
            return self._resp({} if self.revoke_status == 200 else {"error": "invalid_token"}, self.revoke_status)
        raise AssertionError(url)

    def revokes(self):
        return [c[2] for c in self.calls if c[1] == _google.REVOKE_URL]

    def get(self, url, headers=None, timeout=None):
        assert url == _google.USERINFO_URL and timeout == 15
        self.calls.append(("GET", url, dict(headers or {})))
        return self._resp(self.user)

    def __enter__(self):
        self._p = mock.patch.multiple("apps.connections.providers.base.requests", post=self.post, get=self.get)
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


# ---- a person's vault key ----------------------------------------------------------

def vault_key(user) -> ec.EllipticCurvePrivateKey:
    """Give `user` a vault keyset (public half stored, as their browser would) and
    return its private key, which only the test holds: what opens a connection
    sealed to them. Replaces any keyset they had."""
    priv = ec.generate_private_key(ec.SECP256R1())
    UserVaultKeyset.objects.update_or_create(user=user, defaults={"public_jwk": zk_ref.jwk(priv.public_key()),
                                                                  "seals": []})
    return priv


# ---- workspaces and their apps ---------------------------------------------------

def sf_app(workspace, *, provider="salesforce", client_id=None, secret=None, **options):
    """The workspace's app for `provider`, set up (as a row: no audit record, so a
    suite's Event assertions are untouched). Created when missing; an existing
    one is updated with whatever is passed. `options` override the Salesforce
    defaults; for another provider they are the whole options."""
    app = ConnectionApp.objects.filter(workspace=workspace, provider=provider).first()
    if app is None:
        app = ConnectionApp(workspace=workspace, provider=provider, client_id=client_id or CLIENT_ID,
                            options={**SF_OPTIONS, **options} if provider == "salesforce" else options)
        app.client_secret = CLIENT_SECRET if secret is None else secret
        app.save()
        return app
    if client_id is not None:
        app.client_id = client_id
    if secret is not None:
        app.client_secret = secret
    if options:
        app.options = {**app.options, **options}
    if client_id is not None or secret is not None or options:
        app.save()
    return app


def home(user):
    """The workspace a person's connections are made in: the first one they are a
    member of, else a new one of their own."""
    slug = (WorkspaceMembership.objects.filter(user=user).order_by("workspace_id")
            .values_list("workspace_id", flat=True).first())
    if slug is not None:
        return Workspace.objects.get(slug=slug)
    ws = Workspace.objects.create(slug=f"{user.username}-home", display_name=user.username, created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.EDITOR)
    return ws


# A stand-in envelope: canopy-web never opens one, so its bytes are never read here.
SEALED = "zks1:test-envelope"


def _stored(owner_kw, by, *, app=None, sealed=SEALED, **fields):
    """A stored Salesforce connection, sealed (`sealed="zks1:…"`), made with `app` —
    by default the agent's workspace's app, or the person's `home` workspace's
    (each created if missing). No token: canopy-web holds none."""
    if app is None:
        owner = owner_kw.get("agent") or owner_kw.get("user")
        app = sf_app(owner.workspace if "agent" in owner_kw else home(owner))
    values = {"provider": app.provider, "account_id": "00Dorg/005user", "account_label": "jj@dimagi.com",
              "instance_url": "https://dimagi.my.salesforce.com", **fields}
    return Connection.objects.create(app=app, sealed=sealed, connected_by=by, **values, **owner_kw)


def in_force(conn, *agents, by=None) -> Connection:
    """`conn` as a share leaves it, without the crypto: `active`, its statement lending
    it to `agents` (and its owner agent), with a `ConnectionGrant` row for each.
    For suites about which connection an agent uses, not about sealing."""
    pks = {a.pk for a in agents} | ({conn.agent_id} if conn.agent_id else set())
    for a in agents:
        if a.pk != conn.agent_id:
            ConnectionGrant.objects.get_or_create(connection=conn, agent=a, defaults={"granted_by": by})
    conn.state = Connection.ACTIVE
    conn.statement = {"account_id": conn.account_id, "agents": sorted(str(pk) for pk in pks),
                      "connection": str(conn.pk), "endorsers": [], "my_computers": [], "v": 1,
                      "version": conn.statement_version + 1}
    conn.statement_version += 1
    conn.save()
    return conn


FAKE_REFRESH = "5Aep_refresh_shared"


def _refresh_plain(conn, *, statement_floor=0) -> str:
    return json.dumps({"v": 1, "provider": conn.provider, "refresh_token": FAKE_REFRESH, "client_id": CLIENT_ID,
                       "login_host": "login.salesforce.com", "instance_url": conn.instance_url,
                       "account_id": conn.account_id, "owner_key": conn.owner_key, "issued_at": 1793500000,
                       "scopes": "api refresh_token", "statement_floor": statement_floor},
                      separators=(",", ":"))


def sealed_to_owner(owner_kw, signer, *, app=None, **fields) -> Connection:
    """A connection as connect leaves it: `unshared`, the refresh plaintext sealed to
    `signer`'s member key only (their `vault_key` must exist), `owner_key` its thumbprint."""
    ks = UserVaultKeyset.objects.get(user=signer)
    conn = _stored(owner_kw, signer, app=app, sealed="", owner_key=zk_ref.jkt(zk.public_key(ks.public_jwk)),
                   owner_public_jwk=ks.public_jwk, owner_user_id=str(signer.pk), signer=signer, **fields)
    conn.sealed = zk_seal.seal_to(_refresh_plain(conn), agent_id=conn.binding, name="refresh", kv=0,
                                  recipients=[("member", str(signer.pk), ks.public_jwk)])
    conn.sealed_digest = hashlib.sha256(conn.sealed.encode()).hexdigest()
    conn.save()
    return conn


def endorse(agent, key, user, priv, **row) -> AgentVaultEndorsement:
    """`user`'s zk-v1 §7 endorsement, signed by `priv`, that machine `key`'s ECDH key
    may read `agent`. `row` overrides what is stored, to forge one."""
    e = zk_ref.endorse(agent.pk, "device", key.ecdh_jkt, zk.public_key(key.ecdh_public_jwk), priv, user.pk)
    defaults = {"key_jkt": e["key_jkt"], "endorser_jkt": e["by_jkt"], "sig": e["sig"], **row}
    return AgentVaultEndorsement.objects.update_or_create(agent=agent, kind="device", target=key.ecdh_jkt,
                                                          endorser=user, defaults=defaults)[0]


def browser_share(conn, user, priv, *, agents=(), my_computers=(), endorsers=(), to=None, version=None,
                  sign_with=None, writer=None, canopy_web=None) -> dict:
    """The `sharing.share` keyword arguments the owner's browser sends (contract
    §0.3-0.4): a statement lending `conn` to `agents`, its use on `my_computers` (ECDH
    thumbprints), the other people who may vouch for machines (`endorsers`, person key
    thumbprints) and canopy-web's key (`canopy_web`, a thumbprint; the key is left out
    otherwise), signed with `sign_with` (default `priv`); the refresh token
    sealed to `user` and to `to` (`[(kind, target, public_jwk)]`; default every
    machine canopy-web lists for that statement), with writer MACs by `writer`
    (default `priv`, as `user`)."""
    conn.refresh_from_db()
    st = {"account_id": conn.account_id, "agents": sorted(str(a.pk) for a in agents), "connection": str(conn.pk),
          "endorsers": sorted(endorsers), "my_computers": sorted(my_computers), "v": 1,
          "version": conn.statement_version + 1 if version is None else version}
    if canopy_web is not None:
        st["canopy_web"] = canopy_web
    if to is None:
        to = [r for r in rcpt.recipients(conn, st) if r[0] == "device"]
    owner = ("member", str(user.pk), zk_ref.jwk(priv.public_key()))
    everyone = [owner, *to]
    sealed = zk_seal.seal_to(_refresh_plain(conn, statement_floor=st["version"]), agent_id=conn.binding,
                             name="refresh", kv=0, recipients=everyone)
    pubs = {(k, t): zk.public_key(j) for k, t, j in everyone}
    sig = conn_ref.writer_sig(conn.binding, "refresh", conn.sealed_digest, sealed, writer or priv, "member",
                              str(user.pk), pubs)
    return {"statement": st, "sig": conn_ref.sign_statement(st, sign_with or priv),
            "by_jkt": zk_ref.jkt(priv.public_key()), "sealed": sealed, "sealed_sig": sig,
            "expect": conn.sealed_digest}


# ---- a world to share a connection in -------------------------------------------------

class ShareWorld:
    """jj (a person with a vault key) administers eva (their own) and echo (op's,
    by an AgentAdmin row). op runs a cloud box for each; ada, another admin of
    eva, runs eva on her laptop; jj has a computer of their own (plain Claude Code).
    jj vouches for every machine that runs an agent (`endorse`), unless `_box` is told not to."""

    def __init__(self):
        users = get_user_model().objects
        # Every machine by runner name: `(key, signing key, ECDH key)`.
        self.machines: dict = {}
        self.person = users.create_user(username="jj", email="jj@dimagi.com")
        self.person_priv = vault_key(self.person)
        self.op = users.create_user(username="op", email="op@dimagi.com")
        self.ada = users.create_user(username="ada", email="ada@dimagi.com")
        self.ada_priv = vault_key(self.ada)
        self.ws = Workspace.objects.create(slug="w", display_name="W", created_by=self.person)
        for u in (self.person, self.op, self.ada):
            WorkspaceMembership.objects.create(workspace=self.ws, user=u, role=WorkspaceMembership.EDITOR)
        self.app = sf_app(self.ws)
        self.eva = Agent.objects.create(slug="eva", name="Eva", workspace=self.ws, owner=self.person)
        self.echo = Agent.objects.create(slug="echo", name="Echo", workspace=self.ws, owner=self.op)
        AgentAdmin.objects.create(agent=self.echo, user=self.person)
        AgentAdmin.objects.create(agent=self.eva, user=self.op)
        AgentAdmin.objects.create(agent=self.eva, user=self.ada)
        self.eva_box, _, self.eva_ecdh = self._box("eva-cloud", Runner.CLOUD, self.op, self.eva)
        self.echo_box, _, self.echo_ecdh = self._box("echo-cloud", Runner.CLOUD, self.op, self.echo)
        self.ada_laptop, _, _ = self._box("ada-laptop", Runner.EMDASH, self.ada, self.eva)
        self.computer, _, _ = self._box("jj-computer", Runner.COMPUTER, self.person, None)
        self.ada_computer, _, _ = self._box("ada-computer", Runner.COMPUTER, self.ada, None)

    def _box(self, name, kind, owner, agent, *, endorsed=True):
        r = Runner.objects.create(name=name, kind=kind, owner=owner, workspace=self.ws,
                                  last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
        if agent is not None:
            RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
        self.machines[name] = a_machine(r, owner)
        if agent is not None and endorsed:
            endorse(agent, self.machines[name][0], self.person, self.person_priv)
        return self.machines[name]

    def persons(self) -> Connection:
        return sealed_to_owner({"user": self.person}, self.person, app=self.app)

    def evas(self) -> Connection:
        return sealed_to_owner({"agent": self.eva}, self.person, app=self.app)

    def shared(self, conn, agents=(), my_computers=(), to=None, endorsers=(), canopy_web=None) -> Connection:
        return sharing.share(conn, user=self.person, **browser_share(
            conn, self.person, self.person_priv, agents=agents, my_computers=my_computers, to=to,
            endorsers=endorsers, canopy_web=canopy_web))


def revoke_admin(user, agent):
    AgentAdmin.objects.filter(agent=agent, user=user).delete()
