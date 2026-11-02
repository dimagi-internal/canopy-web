"""What the connections suites share: the fake Salesforce and the fake Google (faked
at the `requests` boundary), a workspace's Salesforce app (`sf_app`), a person's home
workspace (`home`), a person's vault key (`vault_key`: a connection is sealed to it)
and a stored, sealed connection helper (`_stored`) — plain helpers, imported by
name. This module is also registered in the root conftest's
`pytest_plugins`, for fixtures; it declares none today."""
from __future__ import annotations

from unittest import mock

from cryptography.hazmat.primitives.asymmetric import ec

from apps.connections.models import Connection, ConnectionApp
from apps.connections.providers import google as _google
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import Workspace, WorkspaceMembership
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

    def post(self, url, data=None, headers=None, timeout=None):
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

    def post(self, url, data=None, headers=None, timeout=None):
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
