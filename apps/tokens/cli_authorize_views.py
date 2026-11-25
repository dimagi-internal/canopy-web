"""CLI authorize endpoint — gh-style loopback flow for minting a PersonalToken.

A local CLI (the canopy plugin's `/canopy:canopy-web-pat-mint` script)
starts a listener on 127.0.0.1:NNNN, then opens the operator's browser
to /auth/cli/authorize/?cb=http://127.0.0.1:NNNN/cb&state=<nonce>&label=<…>.

This view:
  - Requires the operator to be signed in (bounces through OAuth via
    @login_required + ?next=).
  - Validates the callback URL is a loopback HTTP target (rejecting
    anything that could leak the token to a remote host).
  - GET renders a one-click authorize page.
  - POST issues a ONE-TIME CODE for request.user and 302 redirects to
    <cb>?code=<code>&state=<state>. The local listener POSTs the code and
    state to `/auth/cli/token/` (`cli_token`), which mints the PersonalToken
    and returns it. The raw token is never in a URL, so it never lands in the
    browser's history; the code there is spent within seconds and dies in
    `CODE_TTL_SECONDS` either way.

Ported from ace-web's apps/auth/cli_authorize_views.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from urllib.parse import urlencode, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from apps.common.script_prefix import self_full_path

from .models import PersonalToken

logger = logging.getLogger(__name__)

ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_LABEL = "canopy-cli"
MAX_LABEL_LEN = 64
#: How long the loopback has to trade its code. The listener trades it the
#: moment the browser lands, so two minutes is slack, not a working window.
CODE_TTL_SECONDS = 120
_CODE_KEY = "cli-authorize:code:"


def _code_key(code: str) -> str:
    # Only the code's hash names the entry: the cache never holds a spendable code.
    return _CODE_KEY + hashlib.sha256(code.encode()).hexdigest()


def _validate_callback(cb: str) -> str | None:
    """Return cb if it is a safe loopback HTTP URL, else None.

    Rejects anything that could leak the token to a non-loopback host:
    non-http schemes, non-loopback hosts, embedded userinfo, privileged
    or absent ports.
    """
    if not cb:
        return None
    try:
        u = urlparse(cb)
    except ValueError:
        return None
    if u.scheme != "http":
        return None
    if u.hostname not in ALLOWED_LOOPBACK_HOSTS:
        return None
    if u.username or u.password:
        return None
    if u.port is None:
        return None
    if not (1024 <= u.port <= 65535):
        return None
    return cb


@login_required
@require_http_methods(["GET", "POST"])
def cli_authorize(request: HttpRequest) -> HttpResponse:
    """Mint a PersonalToken for request.user and redirect to a loopback CLI."""
    cb = _validate_callback(request.GET.get("cb", ""))
    state = (request.GET.get("state") or "").strip()
    label = (request.GET.get("label") or DEFAULT_LABEL).strip()[:MAX_LABEL_LEN] or DEFAULT_LABEL

    if not cb:
        return HttpResponseBadRequest("invalid or missing cb (must be http://127.0.0.1:NNNN/...)")
    if not state:
        return HttpResponseBadRequest("missing state")
    # Meant for a person at a browser; a bearer that reaches it (the middleware
    # signs one in) must not turn an hour-long OAuth token into a PAT.
    from .middleware import minting_refusal

    refused = minting_refusal(request)
    if refused:
        return HttpResponseForbidden(refused)

    if request.method == "GET":
        return render(
            request,
            "tokens/cli_authorize.html",
            {
                "label": label,
                "cb_host": urlparse(cb).netloc,
                # NOT request.get_full_path() — under the /canopy script prefix
                # that omits the prefix, so the Authorize button POSTs to a
                # Connect Labs path and 404s. See apps.common.script_prefix.
                "form_action": self_full_path(request),
            },
        )

    # The token is minted at the exchange, not here: until then the cache holds
    # who approved and for what label, never a credential.
    code = secrets.token_urlsafe(32)
    cache.set(_code_key(code), {"user": request.user.pk, "label": label, "state": state},
              CODE_TTL_SECONDS)
    logger.info("cli_authorize: issued a code for %s (label=%r, cb_host=%s)",
                request.user.email, label, urlparse(cb).netloc)
    qs = urlencode({"code": code, "state": state})
    return HttpResponseRedirect(f"{cb}?{qs}")


@csrf_exempt
@require_POST
def cli_token(request: HttpRequest) -> HttpResponse:
    """POST /auth/cli/token/ `{code, state}` → `{token}`: the loopback trades the
    code `cli_authorize` sent it for the PersonalToken. Anonymous by design (the
    CLI holds no credential yet); the code is the proof: random, single use,
    bound to the state of the mint that asked for it, and short-lived."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    code = str(body.get("code") or "") if isinstance(body, dict) else ""
    state = str(body.get("state") or "") if isinstance(body, dict) else ""
    entry = cache.get(_code_key(code)) if code else None
    if entry is not None:
        cache.delete(_code_key(code))  # spent on the first try, right or wrong
    if not entry or not state or not secrets.compare_digest(state, entry.get("state", "")):
        return JsonResponse({"detail": "invalid or expired code"}, status=400)
    user = get_user_model().objects.filter(pk=entry["user"], is_active=True).first()
    if user is None:
        return JsonResponse({"detail": "invalid or expired code"}, status=400)
    raw, token = PersonalToken.create_for_user(user=user, label=entry["label"])
    logger.info("cli_token: minted token for %s (label=%r, pk=%s)", user.email,
                entry["label"], token.pk)
    return JsonResponse({"token": raw})
