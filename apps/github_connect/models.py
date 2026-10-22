"""A person's GitHub connection: the grant canopy holds to create their agents' repos."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class GitHubConnection(models.Model):
    """One person's GitHub grant, held so canopy can create their agent's repo.

    WHY THIS IS NOT AN AgentCredential. The credentials design puts per-user
    secrets explicitly out of scope — an agent vault holds what the AGENT is,
    and "Jonathan's GitHub token" is not that. This is the `chrome-sales` case
    named there: a credential that acts on behalf of the dispatching human. So
    it gets its own per-user row and none of the vault's semantics. It also
    satisfies that design's rule for what canopy-web may hold at all — only
    secrets it mints itself, which a token from its own OAuth flow is.

    WHAT IS STORED. Both halves, both Fernet-encrypted at rest: the
    `refresh_token` (the durable half, 6 months) and the current access token
    with its expiry (8 hours). User access tokens live 8 hours and the refresh
    token 6 months (GitHub's "Expire user authorization tokens" setting, which
    is on — with it off GitHub issues no refresh token at all and we would be
    holding a credential that never expires).

    The access token used to be deliberately NOT stored, and every use
    refreshed. That was a race, not a simplification: GitHub rotates the
    refresh token on every refresh, so two concurrent uses (two web workers,
    say a history sync and a diff) both spent the SAME refresh token — the
    loser's exchange was rejected, `refresh_failed_at` was stamped, and the
    owner's grant was dead for every feature until they pressed Connect again.
    Caching the access token makes a refresh rare (once per ~8 hours), and the
    refresh that does happen runs under a row lock (`github_app.access_token_for`).
    Holding it costs little: a stolen row plus the encryption key already
    yielded the refresh token, which is strictly more than an 8-hour token.

    THE REFRESH TOKEN ROTATES. GitHub returns a NEW refresh token on every
    refresh and invalidates the old one, so exactly one process may refresh a
    given row — canopy-web, serialized on this row. That is why a runner can
    never hold this credential: two refreshers race and lock each other out. A
    runner that needs GitHub gets an installation token instead (see
    `docs/superpowers/specs/2026-09-12-github-backed-agent-creation-design.md`).

    SCOPE IS THE INSTALLATION'S, NOT THIS ROW'S. This row records that a person
    authorized canopy; which repositories that reaches is a separate, explicit
    choice they make on GitHub's own installation screen. The two are different
    grants and a connection can exist with zero repository access — see
    `github_app.install_url`.

    There is deliberately no `Administration: write` in the app's permission
    set, so canopy cannot create or delete a repository. That is what lets ONE
    narrow grant serve both pushing an agent's initial scaffold and every later
    agent commit: a user access token cannot be down-scoped, so any permission
    the app holds is a permission every runner token holds, and repo-delete in
    the hands of an autonomous agent is not a risk worth a saved click.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="github_connection",
    )
    # Who GitHub says this is. Display only — never used for authorization
    # (the Django user is the identity; this is what we show them so they can
    # tell they connected the account they meant to).
    github_login = models.CharField(max_length=100)
    github_user_id = models.BigIntegerField()
    # Fernet ciphertext (apps.common.encryption). Blank only in the window
    # between a failed refresh and a reconnect.
    refresh_token_enc = models.TextField(blank=True, default="")
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)
    # The current user access token (Fernet ciphertext) and when GitHub said it
    # expires. Reused until shortly before that, so concurrent callers do not
    # each spend the rotating refresh token. Blank = none cached; refresh.
    access_token_enc = models.TextField(blank=True, default="")
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    # Set when a refresh is rejected, so `/settings` can say "reconnect GitHub"
    # instead of surfacing an opaque 401 in the middle of creating an agent.
    # Cleared on every successful refresh.
    refresh_failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "GitHub connection"
        # Moved here from apps/tokens with its table, so no data moved
        # (migrations: tokens 0034 + github_connect 0001, state only).
        db_table = "tokens_githubconnection"

    def __str__(self) -> str:
        return f"{self.user_id} -> @{self.github_login}"

    @property
    def needs_reconnect(self) -> bool:
        """True when the stored grant cannot mint an access token any more.

        Three ways to get here, and they are one state to the user: the refresh
        token is gone, it expired, or GitHub rejected it (revoked by the user,
        or the app's permissions changed and the installation has not
        re-approved). All of them mean the same next action — press Connect.
        """
        from django.utils import timezone

        if not self.refresh_token_enc:
            return True
        if self.refresh_failed_at is not None:
            return True
        if self.refresh_token_expires_at and self.refresh_token_expires_at <= timezone.now():
            return True
        return False
