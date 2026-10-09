"""Bearer-token authentication middleware.

If the incoming request carries `Authorization: Bearer <raw>` and
`request.user` isn't already authenticated, look the PAT up and stamp
the resolved user onto the request. Downstream middleware
(`LoginRequiredMiddleware`) + Ninja's `DjangoSessionAuth` then see a
real authenticated user, identical to a session-cookie flow.

Three token types resolve here, and only two of them produce a `request.user`.
A `ContactToken` names somebody with no canopy account — it sets
`request.contact` and deliberately leaves the request anonymous, so nothing
written for users can be handed one by accident. See `ContactToken`'s docstring.

CSRF: Bearer-authenticated requests are stateless and not vulnerable to
cross-site forgery, but Django's `CsrfViewMiddleware` doesn't know that
— it only short-circuits on session cookies. We set
`request._dont_enforce_csrf_checks = True` so unsafe-method PAT
callers don't get a 403.

Ordering matters in `config/settings.MIDDLEWARE`:
  1. `django.contrib.sessions.middleware.SessionMiddleware`
  2. `django.contrib.auth.middleware.AuthenticationMiddleware`
  3. **`apps.tokens.middleware.BearerTokenAuthMiddleware`**  ← here
  4. `apps.common.middleware.LoginRequiredMiddleware`
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.utils import timezone

logger = logging.getLogger(__name__)


class BearerTokenAuthMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        refused = self._authenticate(request)
        if refused is not None:
            return refused
        return self.get_response(request)

    @staticmethod
    def _authenticate(request: HttpRequest) -> HttpResponse | None:
        if _authenticate_mcp_call(request):
            return None
        header = request.META.get("HTTP_AUTHORIZATION", "")
        raw = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if not raw:
            return

        user = getattr(request, "user", None)
        already_signed_in = user is not None and getattr(user, "is_authenticated", False)

        # An HCP service access token (apps/contacts/hcp_oauth.py): an app canopy
        # does not operate, acting under ONE person's grant. It is no canopy user,
        # so `request.user` stays anonymous, and it opens /api/hcp/v1/ and nothing
        # else — refused here, before any view, everywhere else.
        from apps.contacts import hcp_oauth

        if raw.startswith(hcp_oauth.ACCESS_PREFIX):
            from django.http import JsonResponse

            tok = hcp_oauth.authenticate_access(raw)
            if tok is None or not request.path_info.startswith("/api/hcp/v1/"):
                detail = ("the access token is not valid" if tok is None
                          else "an HCP access token opens /api/hcp/v1/ only")
                return JsonResponse(
                    {"type": "https://hcp.me/problems/invalid-token", "title": "invalid token",
                     "status": 401, "detail": detail, "instance": request.path_info,
                     "hcp_version": "1.0"},
                    status=401, content_type="application/problem+json",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
            request.hcp_access = tok
            request.auth_method = "hcp-oauth"
            request.auth_credential = _credential("hcp-oauth", tok.pk, tok.client.name)
            request._dont_enforce_csrf_checks = True
            return None

        from apps.tokens.models import ContactToken, DelegatedToken, PersonalToken

        if not already_signed_in:
            token = PersonalToken.lookup(raw)
            if token is not None:
                PersonalToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
                request.user = token.user
                # How this request authenticated — the assurance a turn's
                # initiator records (apps/harness/initiator.py). Absent means
                # canopy's own session did it.
                request.auth_method = "pat"
                # WHICH token — not just "a PAT". Without it a script on someone's
                # PAT and the web UI recorded identical turns (2026-10-05).
                request.auth_credential = _credential(
                    "oauth" if token.oauth_grant_id else "pat", token.pk, token.label)
                request._dont_enforce_csrf_checks = True
                return

        # A CONTACT token first, because it is the one that must never be
        # mistaken for a user. It resolves to no `request.user` at all, so
        # `LoginRequiredMiddleware` refuses every path except the ones
        # explicitly opened to contacts — the surface fails closed by default
        # rather than by each view remembering to check.
        ctok = ContactToken.lookup(raw)
        if ctok is not None:
            request.contact = ctok.contact
            request.delegated_app = ctok.app
            # The site's runner requirements (ZDR), from the token — never from
            # anything the request itself carries.
            request.runner_requirements = tuple(ctok.runner_requirements or ())
            request.auth_method = "contact"
            request.auth_credential = _credential("contact", ctok.pk, ctok.app.name)
            request._dont_enforce_csrf_checks = True
            return

        dtok = DelegatedToken.lookup(raw)
        if dtok is None or not dtok.user.is_active:
            return None

        # `site ∩ user` (apps/tokens/delegation.py): a site's token is not the
        # user's whole canopy. Refused HERE, before any view, and loudly — a
        # 401 would read as an expired token and send a host re-minting one
        # that is equally refused. Only when the token IS the identity: with a
        # canopy session already present the person at the browser is acting.
        from apps.tokens import delegation

        if not already_signed_in and not delegation.reaches(request.method, request.path_info):
            from django.http import JsonResponse

            detail = delegation.refusal(request.path_info)
            return JsonResponse(
                {"type": "about:blank", "title": detail, "status": 403, "detail": detail},
                status=403, content_type="application/problem+json",
            )

        # WHICH app is acting, for the surfaces whose answer depends on it
        # (`/api/embed/agents`). Kept here rather than re-resolved per view so
        # the app can only ever come from the token that authenticated the
        # request — never from a path, query or body the caller controls, which
        # is what stops one host reading another's agent allowlist. `None` on
        # every other auth path: there is no app behind a plain browser
        # request, and those surfaces must refuse rather than default.
        #
        # Stamped even when a SESSION already signed this request in, which is
        # not a detail: canopy embedding its own widget is same-origin, so the
        # browser attaches canopy's session cookie to the frame's XHRs. The
        # early return this used to take meant the `Authorization` header was
        # never read on exactly that path — `delegated_app` stayed None and
        # `/api/embed/agents` answered 403 to the one deployment we shipped it
        # for. Cross-origin hosts never saw it, because no cookie rides along.
        request.delegated_app = dtok.app
        request.runner_requirements = tuple(dtok.runner_requirements or ())

        # Identity stays with the session when there is one. The token was
        # minted FOR that user by `/api/embed/token`, so they agree in practice;
        # where they somehow did not, the person at the browser is the safer
        # answer, and it is the one every other view on this request already saw.
        if not already_signed_in:
            request.user = dtok.user
            request.auth_method = dtok.assurance or "delegated"
            request.auth_credential = _credential("delegated", dtok.pk, dtok.app.name)

        # Safe with or without a session, and required with one: the frame
        # authenticates by header and holds no CSRF cookie for canopy, so its
        # writes (declaring page actions, posting a result) would 403 otherwise.
        # The exemption is granted only against a VALID delegated token, and a
        # cross-site attacker cannot set an `Authorization` header on a request
        # the browser will send without a preflight canopy would refuse.
        request._dont_enforce_csrf_checks = True


def _authenticate_mcp_call(request: HttpRequest) -> bool:
    """An MCP tool call dispatched in-process (`apps/mcp/api_tools.py`).

    The MCP server already authenticated the caller; it hands the result over
    in the ASGI scope, which only that in-process transport builds — no network
    request can put a key there. The request then runs exactly as the caller's
    own token would against REST: their user, their `auth_method`, no CSRF (it
    is stateless), and it counts as a machine (`is_machine`), so the gates that
    refuse tokens refuse it too.
    """
    scope = getattr(request, "scope", None)
    principal = scope.get("canopy.mcp_principal") if isinstance(scope, dict) else None
    if not principal:
        return False
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=principal.get("user_id"), is_active=True).first()
    if user is not None:
        request.user = user
        request.auth_method = principal.get("auth_method") or "pat"
    cred = principal.get("credential")
    if cred:
        request.auth_credential = dict(cred)
    request.via_mcp = True
    request.mcp_tool = principal.get("mcp_tool") or ""
    request._dont_enforce_csrf_checks = True
    return True


#: Credentials that may NOT mint another credential (a PAT, a session). Each is
#: deliberately narrower than a PAT — an OAuth access token lives an hour and
#: dies with its grant ("Disconnect"), a delegated token is a site's, bounded to
#: `site ∩ user` and revoked with the site — so trading one for a never-expiring
#: PAT or a week-long cookie would launder away exactly the limit that made it
#: safe to hand out.
NON_MINTING_CREDENTIALS = frozenset({"oauth", "delegated", "contact"})


def minting_refusal(request: HttpRequest) -> str | None:
    """Why this request may not mint a credential, or None when it may.

    Reads the credential the middleware recorded (`auth_credential`), never a
    header, so it agrees with whatever actually authenticated the request. A
    browser session records none and may mint; so may a plain PAT, which is
    already the most a token can be."""
    kind = (getattr(request, "auth_credential", None) or {}).get("type")
    if kind in NON_MINTING_CREDENTIALS:
        return (
            f"A credential cannot be minted with a {kind} token: it would outlive the "
            "limits that token carries. Sign in to canopy in a browser, or use a "
            "personal access token."
        )
    return None


def _credential(kind: str, pk, label: str) -> dict:
    """`request.auth_credential`: which credential authenticated the request, for
    provenance (apps/common/request_context.py). Never a secret — a row id and
    its human label."""
    return {"type": kind, "id": pk, "label": (label or "")[:200]}
