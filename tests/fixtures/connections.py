"""What the connections suites share: the fake Salesforce (faked at the
`requests` boundary), a stored-connection helper (plain helpers, imported by
name), and the `salesforce_configured` fixture (a plugin fixture, registered in
the root conftest's `pytest_plugins`)."""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.common.encryption import encrypt_secret
from apps.connections.models import Connection

SF = {
    "SALESFORCE_CLIENT_ID": "3MVG-client",
    "SALESFORCE_CLIENT_SECRET": "shh",
    "SALESFORCE_LOGIN_URL": "https://login.salesforce.com",
}
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


def _stored(owner_kw, by, *, access="00D!cached", expires_in=timedelta(hours=1), refresh="5Aep_r"):
    return Connection.objects.create(
        provider="salesforce", account_id="00Dorg/005user", account_label="jj@dimagi.com",
        instance_url="https://dimagi.my.salesforce.com",
        refresh_token_enc=encrypt_secret(refresh),
        access_token_enc=encrypt_secret(access) if access else "",
        access_token_expires_at=(timezone.now() + expires_in) if access else None,
        connected_by=by, **owner_kw,
    )


# ---- fixtures (registered in the root conftest; never autouse) ------------------

@pytest.fixture
def salesforce_configured(settings):
    """Salesforce set up on this deployment. Suites ask for it with
    `pytestmark = pytest.mark.usefixtures("salesforce_configured")`."""
    for k, v in SF.items():
        setattr(settings, k, v)
    return settings
