"""What the connections suites share: the fake Salesforce (faked at the
`requests` boundary), a workspace's Salesforce app (`sf_app`), a person's home
workspace (`home`) and a stored-connection helper (`_stored`) — plain helpers,
imported by name. This module is also registered in the root conftest's
`pytest_plugins`, for fixtures; it declares none today."""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.common.encryption import encrypt_secret
from apps.connections.models import Connection, ConnectionApp
from apps.workspaces.models import Workspace, WorkspaceMembership

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


def _stored(owner_kw, by, *, app=None, access="00D!cached", expires_in=timedelta(hours=1), refresh="5Aep_r",
            **fields):
    """A stored Salesforce connection, made with `app` — by default the agent's
    workspace's app, or the person's `home` workspace's (each created if missing)."""
    if app is None:
        owner = owner_kw.get("agent") or owner_kw.get("user")
        app = sf_app(owner.workspace if "agent" in owner_kw else home(owner))
    values = {"provider": app.provider, "account_id": "00Dorg/005user", "account_label": "jj@dimagi.com",
              "instance_url": "https://dimagi.my.salesforce.com", **fields}
    return Connection.objects.create(
        app=app,
        refresh_token_enc=encrypt_secret(refresh),
        access_token_enc=encrypt_secret(access) if access else "",
        access_token_expires_at=(timezone.now() + expires_in) if access else None,
        connected_by=by, **values, **owner_kw,
    )
