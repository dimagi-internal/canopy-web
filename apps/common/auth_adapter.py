"""Allauth adapters: close local signup, and gate social login on the
email-domain allowlist (or a pending workspace invite)."""
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth import get_user_model
from django.shortcuts import render

from apps.workspaces.services import email_admitted_outside_domain

from .auth_domains import allowed_email_domains, email_in_allowlist


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        email = (sociallogin.account.extra_data.get("email") or "").strip().lower()
        admitted_outside_domain = False
        if not email_in_allowlist(email):
            # An email outside the global domain allowlist is normally a
            # hard reject. Two structural facts used to make the domain
            # check ALONE sufficient proof of control: nobody gets a Google
            # identity asserting an `@dimagi.com` address unless Dimagi
            # issued it, and the allowlisted domain was itself the reason
            # a raw (unverified) email claim could be trusted at all.
            # Bypassing the domain check for a workspace invite loses that
            # bound, so BOTH must be re-established explicitly, independent
            # of one another:
            #
            #  1. the email must be PROVIDER-VERIFIED — Google's own
            #     `email_addresses[].verified`, never the raw self-asserted
            #     `extra_data["email"]` claim above. Otherwise anyone could
            #     assert an address they don't control and ride someone
            #     else's invite/membership straight past the gate.
            #  2. the (now verified) email must have real workspace
            #     standing: `email_admitted_outside_domain` is true iff it
            #     already belongs to a user holding a WorkspaceMembership
            #     (e.g. a previously-accepted invitee — otherwise they'd be
            #     403'd on every login after the one where they accept), OR
            #     there is a currently-live invite for it. Neither check
            #     creates or touches a WorkspaceMembership by itself — the
            #     ONLY path that does is `accept_invite`, called explicitly
            #     by the user after signing in. So admitting the login here
            #     can only ever let an already-authorized-or-invited human
            #     reach the app; it can never itself grant new access.
            admitted_outside_domain = (
                bool(email)
                and email in self._verified_emails(sociallogin)
                and email_admitted_outside_domain(email)
            )
            if not admitted_outside_domain:
                allowed = ", ".join(allowed_email_domains())
                response = render(
                    request,
                    "auth/domain_rejected.html",
                    {"email": email, "allowed_domain": allowed},
                    status=403,
                )
                raise ImmediateHttpResponse(response)

        if admitted_outside_domain:
            # Never run the JIT-identity merge on this branch: it connects
            # to ANY single existing local `User` sharing this email,
            # including a non-allowlisted machine/service account (e.g. one
            # minted by `manage.py create_token --create-user --email
            # ...`). That connect is only safe when the domain allowlist
            # itself already vouched for the email (see the docstring
            # below) — it did not here, so an invite-admitted login must
            # never inherit an existing account's memberships or PAT-minting
            # rights.
            return

        self._connect_jit_identity(request, sociallogin, email)

    @staticmethod
    def _verified_emails(sociallogin) -> set[str]:
        """The set of emails the PROVIDER (not the caller) vouches for —
        never trust `sociallogin.account.extra_data`'s raw claim in place of
        this."""
        return {e.email.strip().lower() for e in sociallogin.email_addresses if e.verified}

    @staticmethod
    def _connect_jit_identity(request, sociallogin, email):
        """Merge a JIT-provisioned delegated-identity user (bare `User` + a
        verified allauth `EmailAddress`, minted by
        `apps.tokens.exchange_api.token_exchange`) with a later real Google
        login for the same human, instead of letting allauth's own
        duplicate-email path fork or block a second account.

        Safe specifically because, by this point: (1) the domain allowlist
        check above has already passed — this is a Dimagi Google account, not
        an arbitrary self-asserted email — and this method is never reached
        on the invite-admitted branch (see `pre_social_login`), which is the
        one case where that would no longer hold; (2) we only trust an email
        the *provider* (Google) marked verified on `sociallogin.email_addresses`,
        never a bare claim. `is_existing` guards against re-connecting an
        already-linked SocialAccount.
        """
        if sociallogin.is_existing:
            return
        if email not in CustomSocialAccountAdapter._verified_emails(sociallogin):
            return
        User = get_user_model()
        matches = list(User.objects.filter(email__iexact=email)[:2])
        if len(matches) == 1:
            sociallogin.connect(request, matches[0])


class CustomAccountAdapter(DefaultAccountAdapter):
    """Close local (username/password) signup entirely.

    Allauth's URLconf is mounted whole, which ships an OPEN
    `/accounts/signup/` form by default. That route bypasses
    CustomSocialAccountAdapter completely, so a stranger could register any
    address — including one at an allowlisted domain — with a self-chosen
    password and be auto-joined to the workspaces that trust that domain,
    never touching Google. Every legitimate identity here comes from the
    social provider (or is provisioned server-side by a management command
    with an unusable password), so nothing is lost by closing it.

    NOTE: `ACCOUNT_LOGIN_METHODS` / `ACCOUNT_SIGNUP_FIELDS` in settings are
    allauth>=65 names and are INERT on the pinned 0.63.x — they look like
    they restrict signup and do not. This adapter is the control that
    actually works; do not remove it in favour of those settings without
    first confirming the installed allauth version reads them.
    """

    def is_open_for_signup(self, request):
        return False
