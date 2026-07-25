"""Social account adapter enforcing an email-domain allowlist."""
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth import get_user_model
from django.shortcuts import render

from .auth_domains import allowed_email_domains, email_in_allowlist


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        email = (sociallogin.account.extra_data.get("email") or "").lower()
        if not email_in_allowlist(email):
            allowed = ", ".join(allowed_email_domains())
            response = render(
                request,
                "auth/domain_rejected.html",
                {"email": email, "allowed_domain": allowed},
                status=403,
            )
            raise ImmediateHttpResponse(response)

        self._connect_jit_identity(request, sociallogin, email)

    @staticmethod
    def _connect_jit_identity(request, sociallogin, email):
        """Merge a JIT-provisioned delegated-identity user (bare `User` + a
        verified allauth `EmailAddress`, minted by
        `apps.tokens.exchange_api.token_exchange`) with a later real Google
        login for the same human, instead of letting allauth's own
        duplicate-email path fork or block a second account.

        Safe specifically because, by this point: (1) the domain allowlist
        check above has already passed — this is a Dimagi Google account, not
        an arbitrary self-asserted email; (2) we only trust an email the
        *provider* (Google) marked verified on `sociallogin.email_addresses`,
        never a bare claim. `is_existing` guards against re-connecting an
        already-linked SocialAccount.
        """
        if sociallogin.is_existing:
            return
        verified_emails = {e.email.lower() for e in sociallogin.email_addresses if e.verified}
        if email not in verified_emails:
            return
        User = get_user_model()
        matches = list(User.objects.filter(email__iexact=email)[:2])
        if len(matches) == 1:
            sociallogin.connect(request, matches[0])
