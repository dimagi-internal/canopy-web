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
        self._authenticate(request)
        return self.get_response(request)

    @staticmethod
    def _authenticate(request: HttpRequest) -> None:
        header = request.META.get("HTTP_AUTHORIZATION", "")
        raw = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if not raw:
            return

        user = getattr(request, "user", None)
        already_signed_in = user is not None and getattr(user, "is_authenticated", False)

        from apps.tokens.models import ContactToken, DelegatedToken, PersonalToken

        if not already_signed_in:
            token = PersonalToken.lookup(raw)
            if token is not None:
                PersonalToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
                request.user = token.user
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
            request._dont_enforce_csrf_checks = True
            return

        dtok = DelegatedToken.lookup(raw)
        if dtok is None or not dtok.user.is_active:
            return

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

        # Identity stays with the session when there is one. The token was
        # minted FOR that user by `/api/embed/token`, so they agree in practice;
        # where they somehow did not, the person at the browser is the safer
        # answer, and it is the one every other view on this request already saw.
        if not already_signed_in:
            request.user = dtok.user

        # Safe with or without a session, and required with one: the frame
        # authenticates by header and holds no CSRF cookie for canopy, so its
        # writes (declaring page actions, posting a result) would 403 otherwise.
        # The exemption is granted only against a VALID delegated token, and a
        # cross-site attacker cannot set an `Authorization` header on a request
        # the browser will send without a preflight canopy would refuse.
        request._dont_enforce_csrf_checks = True
