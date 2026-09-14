"""Personal Access Tokens (PATs).

A PAT is a bearer token bound to a single user. The raw token is shown once at
creation; only the sha256 hash is stored. Revocable per-token; auditable via
`last_used_at`. Tokens expire after `settings.PAT_DEFAULT_TTL_DAYS` (180) unless
minted with an explicit `ttl_days` — where 0 means "never expires", matching the
GitHub PAT model and the labs `mcp_create_token --ttl-days 0` convention.

Replaces the previous shared-secret flow (`/api/auth/e2e-login/` +
`WORKBENCH_WRITE_TOKEN` allowlist) with per-user, per-purpose tokens.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.workspaces.models import WorkspaceMembership


class PersonalToken(models.Model):
    """Bearer token issued to a Django user.

    Authentication: `BearerTokenAuthMiddleware` reads the
    `Authorization: Bearer <raw>` header, sha256-hashes the raw value,
    looks up an unrevoked match, and stamps `request.user = token.user`.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personal_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    label = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When this token stops authenticating. NULL means it never expires — "
            "which covers both tokens minted before expiry existed (grandfathered "
            "by migration 0005, which deliberately has no data step) and tokens "
            "minted with ttl_days=0. Enforced in lookup(), not by callers."
        ),
    )

    class Meta:
        db_table = "personal_tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self):
        return f"Token {self.label!r} for {self.user_id}"

    @property
    def is_active(self) -> bool:
        """Display-only truth. MUST agree with lookup()'s filter — lookup is the
        security boundary; this is what admin and the API render. A divergence
        between the two would let a surface report healthy while auth rejects it.
        """
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    @classmethod
    def create_for_user(
        cls, *, user, label: str, ttl_days: int | None = None
    ) -> tuple[str, PersonalToken]:
        """Mint a token. The raw value is returned ONCE — it's never stored.

        `ttl_days` follows the same rule on every surface (API and CLI):
        None uses settings.PAT_DEFAULT_TTL_DAYS, 0 means never expires, and any
        positive integer is that many days. There is deliberately no upper bound:
        with 0 available, a cap would only mislead a caller asking for a long TTL
        into silently receiving a shorter one.

        The caller is responsible for delivering the raw value to the
        token owner (UI display, env-var dump, etc.).
        """
        if ttl_days is None:
            ttl_days = getattr(settings, "PAT_DEFAULT_TTL_DAYS", 180)
        ttl_days = int(ttl_days)
        if ttl_days < 0:
            raise ValueError("ttl_days cannot be negative (0 means never expires)")
        expires_at = (
            None if ttl_days == 0 else timezone.now() + timedelta(days=ttl_days)
        )
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        token = cls.objects.create(
            user=user, token_hash=token_hash, label=label, expires_at=expires_at
        )
        return raw, token

    @classmethod
    def lookup(cls, raw: str) -> PersonalToken | None:
        """Find a live (unrevoked, unexpired) token by its raw value.

        THE enforcement point for both bearer auth (`BearerTokenAuthMiddleware`)
        and MCP (`CanopyPATVerifier`) — both resolve tokens through here, so
        expiry lands on both surfaces at once and cannot drift between them.
        A NULL expires_at never expires (see the field's help_text).
        """
        if not raw:
            return None
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        return (
            cls.objects.select_related("user")
            .filter(token_hash=token_hash, revoked_at__isnull=True)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
            .first()
        )


_FRAME_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?$")


def is_valid_frame_origin(value) -> bool:
    """A single `frame-ancestors` source: scheme + host + optional port, nothing else.

    Deliberately NOT a general CSP-source parser. Everything a CSP source can
    otherwise be is something we must refuse here:

    * `*` / `https:` / `https://*.example.com` — wildcards re-open framing to
      more than one named site, and the whole point of the directive is that
      the set is enumerated.
    * a path (`https://host/app`) — `frame-ancestors` matches on origin, so a
      path is silently ignored, which means writing one would give a false
      sense of a narrower grant than was actually made.
    * whitespace or `;` — would let one row inject a second directive into the
      header and rewrite the policy.

    `http://` is allowed because local development frames from `http://localhost`.
    It is a weaker origin, but refusing it would push people to disable the
    header in dev, which is worse than allowing it in a value an admin typed.
    """
    return isinstance(value, str) and bool(_FRAME_ORIGIN.match(value.strip()))


class AppCredential(models.Model):
    """A registered embedding application (e.g. ace-web). Its ONLY power is the
    token-exchange endpoint: it can mint short-lived DelegatedTokens for humans
    in its allowlisted email domains. It is NOT a user token — BearerTokenAuth
    never resolves it, so it cannot call normal APIs.

    `provision_workspace` / `provision_role` grant this credential's exchange
    calls the additional power to add a JIT-created (or existing) user to ONE
    tenant workspace, at `provision_role`, the first time they exchange. Null
    `provision_workspace` = no provisioning power (the historical behavior).
    The workspace is fixed on this server-side row — it is never client
    input, so an app can only ever provision into the tenant it was granted.
    `provision_role` must be one of `PROVISION_ROLE_CHOICES` (viewer/editor) —
    `owner` is deliberately excluded from the choices, since an app must never
    be able to mint an administrator of a tenant (owners can invite/remove
    members and change roles). This is an ALLOWLIST, not an owner-only
    denylist: an unrecognized value ("Owner", "admin", a typo) is rejected
    the same as `owner` — both here (`create_credential`) and by a DB-level
    CheckConstraint (`provision_role__in=[...]`), so a shell caller bypassing
    `create_credential` (e.g. `AppCredential.objects.create(...)`) is blocked
    too, and nothing but a known-valid role can ever reach
    `WorkspaceMembership.role` (which `ROLE_RANK[...]` would otherwise
    KeyError on downstream, e.g. in `accept_invite`).

    `provision_role` is a durable ceiling: `ensure_member` is create-only, so
    once this grant creates the membership, nothing (including a later
    explicit self-join via `POST /api/workspaces/{slug}/join`) can raise or
    lower the role it set. There is no more domain-wide auto-join to race
    against — see docs/archive/plans/
    2026-07-26-tenant-scoped-provisioning.md (design + F3 fix, from when
    there was).
    """

    PROVISION_ROLE_CHOICES = [
        (WorkspaceMembership.VIEWER, "Viewer"),
        (WorkspaceMembership.EDITOR, "Editor"),
    ]

    name = models.CharField(max_length=100, unique=True)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    #: The workspace whose owners administer this app.
    #:
    #: Nullable ONLY for rows that predate the product surface — a credential
    #: registered before there was a page to register it on has no owning
    #: tenant to infer, and inventing one would hand somebody an app they never
    #: created. Such a row keeps working exactly as before and is simply not
    #: editable in the UI.
    #:
    #: The usual hazard with a nullable tenant FK is a predicate that reads
    #: "no tenant ⇒ allow" (see ARCHITECTURE.md on `Agent.workspace`). It cannot
    #: arise here: every reader filters `workspace_id__in=<slugs I own>`, and a
    #: SQL `IN` never matches NULL, so an unowned row is excluded by
    #: construction rather than by remembering to exclude it.
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="embedded_apps",
        help_text="Workspace whose owners administer this app. Blank for rows "
        "registered before the Connected apps page existed.",
    )
    #: Email domains this app may assert a user in, at `token-exchange`.
    #:
    #: `blank=True` because EMPTY IS A REAL CONFIGURATION, not an unfinished
    #: one: it means the credential vouches for nobody, and that is exactly
    #: right for an app that never exchanges — canopy's own self-embed mints
    #: through `POST /api/embed/token`, which is session-authenticated and asks
    #: no domain question. Without `blank=True` the admin made the field
    #: required, so registering the self-app forced an operator to grant a
    #: delegation domain it does not use — turning a credential that can only
    #: frame a shell into one that can impersonate every user in that domain.
    allowed_delegation_domains = models.JSONField(default=list, blank=True)
    #: Origins permitted to frame this app's embed shell, as a
    #: `frame-ancestors` list (`https://host[:port]`, no path, no wildcard).
    #:
    #: A JSONField rather than a related table (the v2 spec left this open):
    #: it is the same kind of thing as `allowed_delegation_domains` directly
    #: above — a short, admin-managed allowlist of opaque strings that nothing
    #: joins against — and splitting one of the pair into a table would make
    #: two shapes for one idea. Revisit if an app ever needs enough origins to
    #: want paging or per-origin metadata.
    #:
    #: Empty means the embed shell is NOT SERVED (404), not "any origin".
    #: `frame_origins()` is the only reader, and it sanitises, because an
    #: XFO-exempt page with a permissive or malformed `frame-ancestors` is
    #: frameable by anyone.
    allowed_frame_origins = models.JSONField(default=list, blank=True)
    #: PEM public keys this app signs its visitor assertions with.
    #:
    #: A LIST because rotation has to be possible without a flag day: publish
    #: the new key alongside the old, switch the signer, then drop the old one.
    #: A single column would make every rotation an outage.
    #:
    #: Public keys only, and `assertions.ALLOWED_ALGORITHMS` is asymmetric-only
    #: for the reason that matters: with a symmetric algorithm the verification
    #: key IS the signing key, so a column called "public" would hold the
    #: ability to forge.
    #:
    #: Empty means this app cannot make signed assertions — it is not a
    #: degraded mode, it is the absence of the capability, and
    #: `assertions.verify` refuses rather than falling back to anything weaker.
    public_keys = models.JSONField(default=list, blank=True)
    #: Show this app's widget on canopy's OWN pages.
    #:
    #: Replaces an `EMBED_SELF_APP` setting that named one credential by name.
    #: That made canopy a special case twice over: the page needed a separate
    #: section for it, and the name had to match a deployment setting exactly
    #: or nothing mounted and nothing said why — a fail-closed silence with no
    #: surface to notice it on. As a column it is just another thing an app
    #: may do, and canopy becomes a connected site that happens to point at
    #: itself.
    #:
    #: NOT inferred from the origin list, though that was the obvious idea:
    #: canopy and connect-labs share a host on labs (canopy is served under
    #: `/canopy`, connect-labs at the root), so "lists canopy's origin" would
    #: be true of both and would mount the wrong agent panel on canopy's pages.
    #:
    #: At most one app may set it — enforced in `embed_apps`, not by a
    #: constraint, because the useful behaviour is an error naming the app
    #: that already has it rather than an IntegrityError.
    show_on_canopy_pages = models.BooleanField(default=False)
    provision_workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Tenant this credential may provision JIT/existing users into. "
        "Null = no provisioning power.",
    )
    provision_role = models.CharField(
        max_length=16,
        choices=PROVISION_ROLE_CHOICES,
        default=WorkspaceMembership.EDITOR,
        help_text="Role granted on first provisioning. Never 'owner'.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "app_credentials"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    provision_role__in=[WorkspaceMembership.VIEWER, WorkspaceMembership.EDITOR]
                ),
                name="app_credential_provision_role_valid",
            ),
        ]

    @classmethod
    def create_credential(cls, *, name, domains, created_by,
                          provision_workspace=None, provision_role=WorkspaceMembership.EDITOR):
        valid_roles = dict(cls.PROVISION_ROLE_CHOICES)
        if provision_role not in valid_roles:
            raise ValueError(
                f"AppCredential.provision_role must be one of {sorted(valid_roles)} "
                f"— got {provision_role!r}. An app credential must never mint a "
                "workspace owner or an unrecognized role."
            )
        raw = secrets.token_urlsafe(32)
        cred = cls.objects.create(
            name=name,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            allowed_delegation_domains=list(domains),
            created_by=created_by,
            provision_workspace=provision_workspace,
            provision_role=provision_role,
        )
        return raw, cred

    @classmethod
    def lookup(cls, raw):
        if not raw:
            return None
        return cls.objects.filter(
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            revoked_at__isnull=True,
        ).first()

    def frame_origins(self) -> list[str]:
        """The origins that may frame this app's embed shell — validated here,
        not trusted from the column.

        The write path validates too (`grant_app_frame_origin`), so this is
        defence in depth: a row inserted by a shell, a fixture or a future
        migration must not be able to produce a permissive or malformed
        directive. `*` is the case that matters — it would re-open framing to
        every site, which is exactly what `X-Frame-Options: DENY` was doing for
        us before the exemption.

        Order is preserved so the header is stable (and diffable) rather than
        set-shuffled.
        """
        return [o for o in (self.allowed_frame_origins or []) if is_valid_frame_origin(o)]


class AppCredentialAgent(models.Model):
    """One agent an embedding app is allowed to offer.

    The widget's picker asks a THREE-way question — agent x host x user — and
    nothing modelled it. `AppCredential` covers app x tenant (its domains and
    provisioning grant); `Agent.workspace` covers agent x tenant. The edge
    between an app and an agent did not exist, so a host chose its agent in its
    own settings (ace-web's `CANOPY_AGENT_SLUG`, default "ace") and canopy had
    no record of, or say in, the choice — which is also why canopy could not
    answer "which agents do I have here".

    Explicit rows rather than a list of slugs on `AppCredential`, for the reason
    `RunnerAssignment` replaced self-declared `capabilities.agents`: a real FK
    cannot name an agent that no longer exists, and it is queryable from both
    ends. And server-side only, following `provision_workspace` — the app is
    resolved from the bearer token, never from request data, so one host cannot
    borrow another's allowlist.

    Not exclusive: the same agent may be offered by several apps.

    Keyed on `Agent` because that is what exists today. When an agent gains
    per-tenant/per-user INSTANCES this becomes the instance FK, and the
    intersection in `embed_api` keeps its shape — it just matches on any of an
    agent's tenants instead of its one.
    """

    app = models.ForeignKey(AppCredential, on_delete=models.CASCADE, related_name="allowed_agents")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="embedding_apps")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        db_table = "app_credential_agents"
        ordering = ["app_id", "agent_id"]
        constraints = [
            models.UniqueConstraint(fields=["app", "agent"], name="one_row_per_app_agent")
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.app.name}:{self.agent_id}"


class DelegatedToken(models.Model):
    """Short-lived bearer minted by token exchange: <app> acting as <user>.
    DB-backed (not JWT) so it is revocable and the table is the audit trail."""

    app = models.ForeignKey(AppCredential, on_delete=models.CASCADE, related_name="delegated_tokens")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="delegated_tokens")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "delegated_tokens"
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, *, app, user, ttl_seconds):
        from django.utils import timezone
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            app=app, user=user,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timezone.timedelta(seconds=ttl_seconds),
        )
        return raw, token

    @classmethod
    def lookup(cls, raw):
        """Resolve a raw delegated token, or None.

        Checks the APP's revocation as well as the token's expiry. Without that
        second clause, disconnecting a site did not disconnect it: revoking an
        `AppCredential` stopped it minting anything new and 404'd its embed
        shell, but every token it had already minted kept authenticating for up
        to its full hour. An open widget carried on reading and writing as the
        user, and the one endpoint that did check (`/api/embed/agents`, via
        `_acting_app`) made the gap look closed.

        Revocation is reached for when a secret has leaked. A control that
        takes an hour to take effect is not the control the button promises.
        """
        from django.utils import timezone
        if not raw:
            return None
        return (
            cls.objects.select_related("user", "app")
            .filter(token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                    expires_at__gt=timezone.now(),
                    app__revoked_at__isnull=True)
            .first()
        )


class GitHubConnection(models.Model):
    """One person's GitHub grant, held so canopy can create their agent's repo.

    WHY THIS IS NOT AN AgentCredential. The credentials design puts per-user
    secrets explicitly out of scope — an agent vault holds what the AGENT is,
    and "Jonathan's GitHub token" is not that. This is the `chrome-sales` case
    named there: a credential that acts on behalf of the dispatching human. So
    it gets its own per-user row and none of the vault's semantics. It also
    satisfies that design's rule for what canopy-web may hold at all — only
    secrets it mints itself, which a token from its own OAuth flow is.

    WHAT IS STORED, AND WHAT IS NOT. The `refresh_token` is the durable half
    and the only thing encrypted at rest; the access token is deliberately NOT
    stored. User access tokens live 8 hours and the refresh token 6 months
    (GitHub's "Expire user authorization tokens" setting, which is on — with it
    off GitHub issues no refresh token at all and we would be holding a
    credential that never expires). Treating the access token as disposable
    means a stolen database row is worth one refresh call to detect and revoke,
    not indefinite access.

    THE REFRESH TOKEN ROTATES. GitHub returns a NEW refresh token on every
    refresh and invalidates the old one, so exactly one process may refresh a
    given row — canopy-web. That is why a runner can never hold this
    credential: two refreshers race and lock each other out. A runner that
    needs GitHub gets an installation token instead (see
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
    # Set when a refresh is rejected, so `/settings` can say "reconnect GitHub"
    # instead of surfacing an opaque 401 in the middle of creating an agent.
    # Cleared on every successful refresh.
    refresh_failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "GitHub connection"

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


class EmbedAuditLog(models.Model):
    """Who acted as whom, through which embedding app, and from where.

    The credential surfaces wrote `logger.info` / `logger.warning` and nothing
    else. Application logs are the wrong home for this: they are not queryable
    per user or per app, they age out on a retention policy chosen for
    debugging, and on a container they are the first thing lost. The question an
    audit trail exists to answer — *did anything act as me, and what let it* —
    could not be asked at all.

    **Identifying strings are denormalised on purpose.** `user` and `app` are
    `SET_NULL`, so deleting either would otherwise erase the trail of what it
    did, which is the one thing an audit row must survive. `subject_email` and
    `app_name` are written at the time of the event and never updated: they
    record what was true then, not what is true now.

    Append-only by convention — nothing in canopy updates or deletes a row here.
    """

    # Identity flowed to someone.
    EXCHANGE = "exchange"          # a host asserted a user, and canopy believed it
    MINT = "mint"                  # canopy minted for its own signed-in user
    # The registration itself changed.
    CONNECT = "connect"
    UPDATE = "update"
    ROTATE = "rotate"
    DISCONNECT = "disconnect"
    EVENT_CHOICES = [
        (EXCHANGE, "Token exchange"),
        (MINT, "Session mint"),
        (CONNECT, "Site connected"),
        (UPDATE, "Site updated"),
        (ROTATE, "Secret rotated"),
        (DISCONNECT, "Site disconnected"),
    ]

    event = models.CharField(max_length=20, choices=EVENT_CHOICES, db_index=True)
    ok = models.BooleanField(default=True)
    #: Short machine-readable reason on a refusal (`bad_domain`, `revoked`, …).
    reason = models.CharField(max_length=64, blank=True, default="")

    app = models.ForeignKey(
        "AppCredential", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_rows",
    )
    #: The app's name AS IT WAS. Survives the app being deleted.
    app_name = models.CharField(max_length=100, blank=True, default="", db_index=True)

    #: Whose identity was handed over (exchange/mint), or whose registration
    #: changed. Null for a refusal that never resolved a user.
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="embed_audit_subject_rows",
    )
    subject_email = models.CharField(max_length=254, blank=True, default="", db_index=True)

    #: Who performed the action, when that is a different person from the
    #: subject — an owner connecting a site, say. Null when they are the same.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="embed_audit_actor_rows",
    )

    #: Client address as the app server saw it. Best-effort and spoofable
    #: upstream of a proxy — recorded because it is the only correlation
    #: available when a credential is suspected of leaking, not because it is
    #: evidence on its own.
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    detail = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "embed_audit_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["app_name", "-created_at"]),
            models.Index(fields=["subject_email", "-created_at"]),
            models.Index(fields=["event", "-created_at"]),
        ]

    def __str__(self):
        status = "ok" if self.ok else f"REFUSED({self.reason})"
        return f"[{status}] {self.event} {self.app_name} -> {self.subject_email}"


class ContactToken(models.Model):
    """A bearer for a CONTACT — someone with no canopy account at all.

    **Deliberately a separate model from `DelegatedToken`, not a nullable user
    on it.** A `DelegatedToken` resolves to a `User`, and every surface
    downstream applies that user's ACL. A contact has no ACL to apply, so a
    contact arriving through the same lookup would not be a smaller permission
    — it would be an unspecified one, and the code that received it was written
    for principals that have memberships.

    That is not hypothetical here. `Agent.workspace` was nullable once and six
    separate predicates independently grew a `workspace_id IS NULL` leg meaning
    *allow*, because a row with no tenant met code written for rows that have
    one (ARCHITECTURE.md). Two types cannot make that mistake: nothing that
    asks for a user can be handed a contact by accident.

    Same shape as `DelegatedToken` otherwise, and for the same reasons: opaque
    and hashed at rest so the table is not a credential store, short-lived, and
    resolvable only while both the app and the person behind it are in good
    standing.
    """

    app = models.ForeignKey(AppCredential, on_delete=models.CASCADE, related_name="contact_tokens")
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, related_name="tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "contact_tokens"
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, *, app, contact, ttl_seconds):
        from django.utils import timezone

        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            app=app, contact=contact,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timezone.timedelta(seconds=ttl_seconds),
        )
        return raw, token

    @classmethod
    def lookup(cls, raw):
        """Resolve a raw contact token, or None.

        Three independent reasons to refuse, all in the one query so no caller
        can implement two of them: the token expired, the site was
        disconnected, or this particular person was blocked. The third is the
        reason blocking exists — refusing one visitor without disconnecting a
        whole site — and putting it here means it takes effect on the next
        request rather than whenever a view remembers to check.
        """
        from django.utils import timezone

        if not raw:
            return None
        return (
            cls.objects.select_related("contact", "contact__workspace", "app")
            .filter(
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                expires_at__gt=timezone.now(),
                app__revoked_at__isnull=True,
                contact__blocked_at__isnull=True,
            )
            .first()
        )
