"""The e2e API (`backend.sh` serves `fake_salesforce:application`): canopy-web's own ASGI app, with
Salesforce answered in-process, so a connect runs through the real start and callback views.

Only `SalesforceProvider._call`, the one place a Salesforce provider makes an outbound HTTP call,
is replaced, and only in this process: the authorize URL, the code exchange's checks, scope and
host rules, the identity lookup's host check, sealing and storing all run as in production. The
browser's leg (Salesforce's sign-in page) is answered by the test itself (`page.route`), which
sends the browser back to the callback with `code=e2e-<agent slug>`.

The account a code signs in is `<slug>@dimagi-ai.com` (org `00De2e`), so each agent gets its own.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.getcwd())  # the repo root (backend.sh runs from there)

import requests  # noqa: E402,I001  (Django is set up by config.asgi, before any app module)

from config.asgi import application  # noqa: E402,F401  (sets Django up)
from apps.connections.providers.salesforce import SalesforceProvider  # noqa: E402

ORG = "00De2e"
INSTANCE = "https://dimagi-e2e.my.salesforce.com"


def _response(url: str, body: dict, status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.headers["Content-Type"] = "application/json"
    r._content = json.dumps(body).encode()
    return r


def _call(self, method, url, what, **kw):
    if url == self.token_url():
        form = kw.get("data") or {}
        if form.get("grant_type") == "authorization_code":
            slug = str(form.get("code", "")).removeprefix("e2e-") or "someone"
            return _response(url, {
                "access_token": f"00D!e2e-access-{slug}", "refresh_token": f"5Aep-e2e-refresh-{slug}",
                "instance_url": INSTANCE, "id": f"https://login.salesforce.com/id/{ORG}/005{slug}",
                "token_type": "Bearer", "scope": "api refresh_token id",
                "issued_at": "1793500000000", "signature": "e2e"})
        return _response(url, {"access_token": "00D!e2e-access-refreshed", "instance_url": INSTANCE,
                               "scope": "api refresh_token id"})
    if url == self.revoke_url():
        return _response(url, {})
    if what == "identity lookup":
        token = (kw.get("headers") or {}).get("Authorization", "").removeprefix("Bearer ")
        slug = token.removeprefix("00D!e2e-access-")
        return _response(url, {"organization_id": ORG, "user_id": f"005{slug}",
                               "username": f"{slug}@dimagi-ai.com"})
    return _response(url, {"error": "not_found", "error_description": f"the e2e Salesforce has no {url}"}, 404)


SalesforceProvider._call = _call
