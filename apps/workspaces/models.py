"""Workspace tenancy models — the unit of multi-tenancy.

Ported from ace-web `apps/workspaces`, made domain-agnostic: the ace-specific
`drive_root_folder_id` is dropped (a generic workspace owns no Drive folder). A
Workspace owns members (roles) and pending email invites; agents + runs are
scoped to exactly one workspace (a later increment adds that FK).

This is the tenancy concept — distinct from the retired co-authoring app that
used to be `apps/workspace` (singular).

FRAMEWORK tier: may FK to the auth User + framework models; must not import any
product app. See ARCHITECTURE.md.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

#: The charset a workspace slug may use. Identical to ace-web's `SLUG_RE`, so
#: the two sibling deployments agree on what a tenant may be called.
#:
#: This is a TENANCY invariant, not a cosmetic one. A slug is an addressing
#: token: it appears in URLs, in Channels group names, and inside the presence
#: page key `<app>:<workspace>:<resource>`, which is parsed with a bounded
#: `split(":", 2)`. The resource segment legitimately contains colons
#: (`opp:bednet/run-001`, `session:<id>`), so a slug containing one is
#: irreducibly ambiguous with a shorter workspace plus a colon-bearing
#: resource — `canopy:acme:eu:activity` reads equally well as workspace
#: `acme:eu` and as workspace `acme` + resource `eu:activity`. Nothing in the
#: presence layer can disambiguate that after the fact, so such a slug must
#: never come into existence. See apps/realtime/presence_keys.py.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"

validate_slug = RegexValidator(
    # `\Z`, not `$` (and not `SLUG_PATTERN` verbatim): Python's `re` special-
    # cases `$` to also match immediately before a single trailing `\n`, so
    # `RegexValidator`'s `.search()`-based check would let `"acme\n"` through
    # even though the pattern "looks" fully anchored — `\Z` matches only the
    # true end of the string, with no such exception. This is deliberately a
    # SEPARATE pattern from the exported `SLUG_PATTERN` (which schemas.py
    # feeds straight into Pydantic's `Field(pattern=...)`): Pydantic v2
    # compiles `pattern=` with the Rust `regex` crate, which does not
    # recognize `\Z` (only lowercase `\z`) and would fail to compile — and
    # doesn't need the fix anyway, since Rust's `$` has no trailing-newline
    # quirk to begin with. See tests/test_trailing_newline_slug.py.
    regex=r"^[a-z0-9][a-z0-9-]*\Z",
    message=(
        "Slug must start with a lowercase letter or digit and contain only "
        "lowercase letters, digits and hyphens."
    ),
    code="invalid_slug",
)


def generate_invite_token() -> str:
    """A 48-char URL-safe random invite token."""
    return secrets.token_urlsafe(36)[:48]


class Workspace(models.Model):
    slug = models.CharField(primary_key=True, max_length=64, validators=[validate_slug])
    display_name = models.CharField(max_length=200)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="workspaces_created",
    )
    settings = models.JSONField(default=dict, blank=True)
    auto_join_domains = models.JSONField(
        default=list,
        blank=True,
        help_text="Email domains (lowercased, no leading '@') whose users are "
        "auto-added as editor on first login.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.slug})"

    def save(self, *args, **kwargs):
        """Enforce the slug charset on the SAVE path, not just in the schema.

        `WorkspaceCreateIn` guards the API, but a shell, a management command
        or an ad-hoc script goes straight to `Workspace.objects.create()` — and
        an out-of-charset slug minted that way is exactly as dangerous as one
        minted over HTTP (see SLUG_PATTERN). Validation is scoped to the slug
        so this stays a tenancy guard rather than a surprise `full_clean()` on
        every field of an otherwise-valid save.
        """
        self.full_clean(
            exclude=[f.name for f in self._meta.fields if f.name != "slug"],
            validate_unique=False,
            validate_constraints=False,
        )
        super().save(*args, **kwargs)


class WorkspaceMembership(models.Model):
    OWNER, EDITOR, VIEWER = "owner", "editor", "viewer"
    ROLE_CHOICES = [(OWNER, "Owner"), (EDITOR, "Editor"), (VIEWER, "Viewer")]
    # Total order used to decide "higher" vs "lower" role — the single place
    # role ORDERING lives (consumed by `accept_invite`'s upgrade-only
    # semantic, and by `services.set_member_role` as the valid-role check
    # via `role not in ROLE_RANK`). `is_last_owner`/`set_member_role`'s own
    # role comparisons are plain `==`/`!=` — ROLE_RANK is only for "which of
    # two roles outranks the other", not membership/equality checks.
    ROLE_RANK = {VIEWER: 0, EDITOR: 1, OWNER: 2}

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Provenance for a row created by an AppCredential's token-exchange
    # provisioning grant (apps.tokens.models.AppCredential.provision_workspace)
    # rather than an organic human join/invite. Null for every other row.
    # A string ref ("tokens.AppCredential") avoids a hard Python import from
    # workspaces -> tokens (tokens already imports workspaces for
    # WorkspaceMembership's role constants, so a direct import back would be
    # circular). Lets a leaked-credential incident be found and bulk-reverted
    # by app rather than being indistinguishable from an organic join — see
    # docs/archive/plans/2026-07-26-tenant-scoped-provisioning.md (F1).
    # Which embedding app created this membership (null = a human joined, was
    # invited, or auto-joined). PROTECT, not SET_NULL: this is an audit field,
    # and deleting the credential would otherwise silently erase provenance on
    # exactly the rows an incident responder needs — the ones a leaked
    # credential created. Retire a credential by setting `revoked_at` (which
    # keeps the row and the trail); deleting one is blocked while it still has
    # provisioned memberships to answer for.
    provisioned_by_app = models.ForeignKey(
        "tokens.AppCredential",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="provisioned_memberships",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="uniq_ws_member"),
        ]
        indexes = [models.Index(fields=["user", "workspace"])]

    def __str__(self) -> str:
        return f"{self.user.email} = {self.role} on {self.workspace.slug}"


class WorkspaceInvite(models.Model):
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="invites"
    )
    email = models.CharField(max_length=200)
    role = models.CharField(
        max_length=16,
        choices=WorkspaceMembership.ROLE_CHOICES,
        default=WorkspaceMembership.EDITOR,
    )
    token = models.CharField(max_length=64, unique=True, default=generate_invite_token)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="invites_sent",
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["email", "-created_at"]),
            models.Index(fields=["workspace", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="uniq_ws_invite_live_per_email",
            ),
        ]

    def __str__(self) -> str:
        return f"Invite {self.email} to {self.workspace.slug} as {self.role}"

    def is_pending(self) -> bool:
        if self.accepted_at is not None or self.revoked_at is not None:
            return False
        return self.expires_at > timezone.now()
