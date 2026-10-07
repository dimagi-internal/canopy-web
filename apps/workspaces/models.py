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
    # Workspaces form a TREE: an org (`dimagi`) sits above its divisions
    # (`connect`, `strategy`, …). What the tree grants is deliberately narrow —
    # an OWNER of an ancestor is an owner of every descendant, and no other
    # ACCESS flows down (the one config that does is the shared vault — see
    # `shared_vault_source`; it grants no person anything). A parent's editors and viewers get no access to a child: the
    # org workspace takes access requests from the whole email domain, so inheriting
    # editor would hand every employee every division's agents, which is the
    # exact isolation a division workspace exists to provide. Resolution lives
    # in `services.membership` (the sole authorizer), not here.
    #
    # PROTECT: deleting a parent must not silently orphan (or cascade-delete)
    # its divisions; the delete endpoint names the children in the way.
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )
    # The tenant's SHARED 1Password vault, and a service-account token scoped to
    # it. Sibling of Agent.op_vault / op_sa_token_enc one level up: an agent's
    # own secrets live in Agent-<Slug>, but the credentials every agent in this
    # tenant needs — the shared gog OAuth clients, a github token — live here.
    #
    # Per TENANT rather than one fleet-wide constant because tenants do not
    # share secrets: different tenants can hold different values under the same
    # names, and the box must be TOLD which vault it is reading rather than
    # deriving one (Jonathan, 2026-09-07). The name was hardcoded as
    # "Canopy-Shared" in bootstrap_agents.sh, which is correct for exactly one
    # tenant and silently wrong for the second.
    #
    # The token is SEPARATE from any agent key on purpose. Handing the box a key
    # that reads both this vault and every Agent-* vault would undo the reason
    # the per-agent split exists (Agent.op_vault, 2026-09-06): a shared-vault-only
    # key bounds a compromise to credentials that are shared by definition.
    shared_op_vault = models.CharField(
        max_length=200, blank=True, default="",
        help_text="1Password vault holding secrets every agent in this workspace "
                  "needs, e.g. Canopy-Shared. Blank falls back to the box default.",
    )
    shared_op_sa_token_enc = models.TextField(blank=True, default="")
    # Who may REQUEST an invitation (owner decision, 2026-10-04): a person whose
    # login email is at one of these domains sees this workspace in
    # `GET /api/workspaces/requestable` and may `POST .../access-requests`.
    # Nothing here grants membership — an admin or owner approves the request
    # (or, while `auto_approve_role` is set, it is approved at that role).
    # Server-only: never client input (see WorkspaceCreateIn). Was
    # `self_join_domains` ("may join") until workspaces/0012.
    access_request_domains = models.JSONField(
        default=list,
        blank=True,
        help_text="Email domains (lowercased, no leading '@') whose users may REQUEST "
        "an invitation to this workspace. A request grants nothing until approved.",
    )
    # Approve every access request the moment it is made, at THIS role. Blank =
    # off: a person approves each one. A per-workspace setting rather than code
    # so turning it off or lowering it is a settings change (a workspace owner,
    # `PUT /api/workspaces/{slug}/access-settings`). Capped at editor — an
    # automatic grant never makes an admin or owner. Off everywhere by
    # default; `dimagi` was set to editor by workspaces/0012 while it
    # bootstraps (Jonathan, 2026-10-04). An auto-approved request is still a
    # normal request record, and every admin and owner is still told.
    AUTO_APPROVE_CHOICES = [("", "Off"), ("viewer", "Viewer"), ("editor", "Editor")]
    auto_approve_role = models.CharField(
        max_length=16, blank=True, default="", choices=AUTO_APPROVE_CHOICES,
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
        self._check_no_cycle()
        super().save(*args, **kwargs)

    def ancestor_slugs(self) -> list[str]:
        """Slugs from the direct parent up to the root, nearest first.

        Bounded by MAX_DEPTH so a cycle written around `save()` (a raw UPDATE)
        degrades to a truncated walk instead of an infinite loop inside an
        authorization check."""
        out: list[str] = []
        pid = self.parent_id
        seen = {self.slug}
        while pid and pid not in seen and len(out) < MAX_DEPTH:
            out.append(pid)
            seen.add(pid)
            pid = Workspace.objects.filter(slug=pid).values_list("parent_id", flat=True).first()
        return out

    def shared_vault_source(self) -> "Workspace | None":
        """The workspace whose shared vault this one uses: itself when it has
        one set, else its nearest ancestor that does, else None.

        The ONE thing besides ownership that flows down the tree. A division is
        part of its org's tenant, and what the shared vault holds (the gog OAuth
        clients) is shared by definition, so inheriting it grants no person
        access to anything — unlike editor/viewer, which the tree deliberately
        withholds. Without it every new division needs the org's key pasted in
        again, and rotating that key means N edits (Jonathan, 2026-09-29: point
        every division at the same Canopy-Shared vault Dimagi and Connect use).
        A division with its own vault or key set keeps it — nearest wins, and
        vault + key always come from the SAME workspace, never mixed."""
        if self.shared_op_vault or self.shared_op_sa_token_enc:
            return self
        for slug in self.ancestor_slugs():
            ws = Workspace.objects.filter(slug=slug).first()
            if ws is not None and (ws.shared_op_vault or ws.shared_op_sa_token_enc):
                return ws
        return None

    def _check_no_cycle(self) -> None:
        from django.core.exceptions import ValidationError

        if not self.parent_id:
            return
        if self.parent_id == self.slug:
            raise ValidationError({"parent": "a workspace cannot be its own parent"})
        pid, steps = self.parent_id, 0
        while pid and steps <= MAX_DEPTH:
            if pid == self.slug:
                raise ValidationError({"parent": "that parent would create a cycle"})
            pid = Workspace.objects.filter(slug=pid).values_list("parent_id", flat=True).first()
            steps += 1
        if pid:
            raise ValidationError({"parent": f"workspace tree deeper than {MAX_DEPTH}"})


#: How deep the workspace tree may be walked. An org → division → team tree is
#: three; this is headroom, and a hard stop against a corrupted cycle.
MAX_DEPTH = 8


class WorkspaceMembership(models.Model):
    OWNER, ADMIN, EDITOR, VIEWER = "owner", "admin", "editor", "viewer"
    ROLE_CHOICES = [(OWNER, "Owner"), (ADMIN, "Admin"), (EDITOR, "Editor"), (VIEWER, "Viewer")]
    # Total order — the single place role ORDERING lives. What each role may
    # DO is not here: that is `apps/workspaces/permissions.py`, which maps
    # every capability to the lowest role that holds it, and is the only thing
    # outside this app allowed to reason about roles at all
    # (`tests/test_roles_named_only_in_workspaces.py`).
    #
    # ADMIN (2026-10-02) sits between editor and owner: it runs the workspace
    # day to day — reads every log, manages invites and members below itself,
    # and the integrations (mailboxes, connected sites, Slack sync) — without
    # the owner's keys: the shared vault, deleting or moving the workspace, the
    # Slack app itself, and agents' credentials.
    ROLE_RANK = {VIEWER: 0, EDITOR: 1, ADMIN: 2, OWNER: 3}

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
    # Provenance for a row an AppCredential's provisioning grant created, rather
    # than an organic human join/invite. Null for every other row. NOTHING WRITES
    # THIS ANY MORE: the grant went with `/api/auth/token-exchange` (2026-09-22),
    # and the column is kept because it records how existing rows came to be —
    # dropping it would erase that, which is the one thing it is for.
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
        # The role the inviting admin chose; accepting grants exactly it (never
        # lower than a role already held). Viewer unless they chose otherwise.
        default=WorkspaceMembership.VIEWER,
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
    # When canopy last emailed this invite's link (services.email_invite). Null
    # = never: email was off, the send failed, or it predates invite email.
    last_emailed_at = models.DateTimeField(null=True, blank=True)

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


class WorkspaceAccessRequest(models.Model):
    """Someone asking to be invited into a workspace.

    The only way in besides an invite: a person whose login email is at one of
    the workspace's `access_request_domains` asks; every admin and owner is
    notified; an admin or owner approves at a role (no higher than their own
    may grant) or denies. Approval is what creates the membership
    (`services.approve_access_request`). With the workspace's
    `auto_approve_role` set, the request is approved at creation at that role,
    `auto=True`, `decided_by=None`. See docs/architecture/access.md.
    """

    PENDING, APPROVED, DENIED = "pending", "approved", "denied"
    STATUS_CHOICES = [(PENDING, "Pending"), (APPROVED, "Approved"), (DENIED, "Denied")]

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="access_requests"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_access_requests",
    )
    note = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    # The role an approval granted. Blank while pending or denied.
    role = models.CharField(
        max_length=16, choices=WorkspaceMembership.ROLE_CHOICES, blank=True, default="",
    )
    auto = models.BooleanField(default=False)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    # Optional words from whoever denied it, passed on to the requester.
    decision_reason = models.TextField(blank=True, default="")
    # What happened when the admins were told — {"emailed": [...], "failed":
    # [...], "pushed": n, "not_configured": bool}. Kept so a failed notification
    # is visible on the request itself, not only in a log line.
    notify_result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "status", "-created_at"], name="ws_access_req_status_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                condition=models.Q(status="pending"),
                name="uniq_ws_access_request_open",
            ),
        ]

    def __str__(self) -> str:
        return f"Access request {self.user_id} -> {self.workspace_id} ({self.status})"


class SystemAccount(models.Model):
    """A non-human member of a workspace: an automated sender such as AWS
    CloudWatch, that makes agents do work the way a person with the same role
    would, and can never sign in.

    It IS a canopy `User` (`user`), so everything that keys on a user — the
    access rule, routing rules and per-person routes, the turn-content ACL,
    the roster — works for it unchanged. It holds an ordinary membership in
    `workspace` (editor by default, never above: a system administers nothing).
    What makes it a system account rather than a person:

    * **It cannot authenticate.** Its password is unusable, its `User.email`
      is a synthetic `.invalid` address no identity provider can assert, and
      every credential door refuses it (`apps/workspaces/system_accounts.py`
      lists them; `test_system_accounts.py` pins each one).
    * **It arrives only by a binding.** Mail becomes this account's turn only
      through a `SystemSender` of THIS workspace, on a message aligned on our
      own receiver's verdict (`harness.services._member_behind_email`). The
      sender address alone proves nothing across tenants:
      `no-reply@sns.amazonaws.com` is every AWS customer's.

    Disabling one (`disabled_at`) stops it resolving at once — its mail falls
    back to being an ordinary contact — while keeping its turns' attribution.
    Deleting the account deletes its user, its membership and its bindings.
    See docs/architecture/access.md, "System accounts".
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="system_account",
    )
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="system_accounts",
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    disabled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_ws_system_account_name"),
        ]

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None

    def __str__(self) -> str:
        return f"system:{self.name} on {self.workspace_id}"


class SystemSender(models.Model):
    """An inbound address that, in this account's workspace, IS the account.

    `subject_pattern` (a Python regex, `re.search`, case-sensitive) narrows a
    shared address to the mail that is actually yours — e.g. only alarms
    whose names start `labs-` — so another tenant's alarms, or your own
    unrelated ones, stay contacts. Blank matches any subject.
    """

    account = models.ForeignKey(SystemAccount, on_delete=models.CASCADE, related_name="senders")
    address = models.EmailField()
    subject_pattern = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["address", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["account", "address", "subject_pattern"],
                                    name="uniq_system_sender"),
        ]

    def __str__(self) -> str:
        return f"{self.address} -> {self.account.name}"
