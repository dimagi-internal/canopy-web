"""Workspace apps: templates in a store, and the instances workspaces customize.

Three layers, after Shopify's core / themes / apps and Salesforce's managed
packages (docs/architecture/workspace-apps.md):

* `AppTemplate` + `AppTemplateVersion` — what an app IS, published to the store:
  a manifest (`manifest.py`). canopy's own templates are synced from
  `bundled/<key>/`; a workspace's own app is a template the workspace owns.
  A released version is immutable.
* `AppInstallation` — one template in one workspace. It never COPIES the
  template: what runs is `template version + overlay` (`overlay.py`), the
  workspace's own keyed, additive changes. A template update is the same overlay
  over a newer version, so customizing never forks the app off its update path.
* `AppVersion` — every overlay the installation has had, append-only: drafts
  being tested, the published one, the ones it superseded. Publishing a draft and
  rolling back are both "make this version live".

An app is DATA, never server code, so installing or changing one — even one a
workspace wrote — runs nothing on the server. Records (`AppRecord`), their history
(`AppRecordEvent`) and files (`AppFile`) belong to the installation, and to a
`space`: "live", or one draft's private test copy.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

LIVE = "live"


class AppTemplate(models.Model):
    """An app in the store. `owner` is the workspace that publishes it, or none
    for canopy's own catalog (synced from the repo, readable by every member of
    every workspace — it holds no tenant data)."""

    #: canopy's own: the manifest key ("hiring"). A workspace's: "<workspace>/<key>".
    key = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey("workspaces.Workspace", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="app_templates")
    #: Shown in the store. An unlisted template installs only where it is owned.
    listed = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return self.key


class AppTemplateVersion(models.Model):
    template = models.ForeignKey(AppTemplate, on_delete=models.CASCADE, related_name="versions")
    version = models.CharField(max_length=40)
    #: Validated (`manifest.validate`). Never edited once released.
    manifest = models.JSONField()
    changelog = models.TextField(blank=True, default="")
    released_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-released_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["template", "version"], name="app_template_version_once")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.template_id}@{self.version}"


class AppInstallation(models.Model):
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="app_installations")
    key = models.CharField(max_length=40)
    template = models.ForeignKey(AppTemplate, on_delete=models.PROTECT, related_name="installations")
    #: The published version. Null only in the instant between creating the
    #: installation and its first version.
    live = models.ForeignKey("AppVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: The live version's composed manifest, cached: what every read and action uses.
    manifest = models.JSONField(default=dict)
    name = models.CharField(max_length=120)
    #: Off = the app's pages and actions answer 404 to everyone; its data stays.
    enabled = models.BooleanField(default=True)
    #: The app's PUBLIC pages and actions answer anonymous visitors. Off by default.
    public = models.BooleanField(default=False)
    #: Setup steps (manifest `setup`) a maintainer ticked off by hand, by id.
    setup_done = models.JSONField(default=list, blank=True)
    #: A maintainer put the setup list away before finishing it.
    setup_hidden = models.BooleanField(default=False)
    installed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    installed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "key"], name="workspace_app_key_per_workspace"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.key}"


class AppVersion(models.Model):
    DRAFT, PUBLISHED, SUPERSEDED, DISCARDED = "draft", "published", "superseded", "discarded"
    STATUSES = [(s, s) for s in (DRAFT, PUBLISHED, SUPERSEDED, DISCARDED)]
    #: Why the version exists: a customization, a template update, a rollback, the install.
    ORIGINS = [(s, s) for s in ("install", "edit", "upgrade", "rollback")]

    installation = models.ForeignKey(AppInstallation, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    template_version = models.ForeignKey(AppTemplateVersion, on_delete=models.PROTECT, related_name="+")
    #: The workspace's changes: a list of keyed operations (`overlay.py`).
    overlay = models.JSONField(default=list)
    status = models.CharField(max_length=12, choices=STATUSES, default=DRAFT)
    origin = models.CharField(max_length=12, choices=ORIGINS, default="edit")
    note = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    #: The last scenario run on this version: {ok, ran_at, results: [...]}.
    checks = models.JSONField(default=dict, blank=True)
    #: Agents a maintainer asked to work on THIS draft: they may read it, change
    #: it, test it and run its checks — never publish, discard or roll back. The
    #: draft closing ends it, so no grant outlives the work it was given for.
    helpers = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="+")

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["installation", "number"], name="app_version_number_once"),
            models.UniqueConstraint(fields=["installation"], condition=models.Q(status="draft"),
                                    name="app_version_one_draft"),
        ]

    @property
    def space(self) -> str:
        """Where this version's test copy keeps its records."""
        return LIVE if self.status != self.DRAFT else f"draft-{self.pk}"


class AppRoleGrant(models.Model):
    """An app role given to one workspace member by name, beside the roles the
    manifest derives from workspace capabilities (`access.roles_for`). How a
    viewer becomes an interviewer, or the head of HR becomes the app's maintainer,
    without becoming a workspace editor or admin."""

    installation = models.ForeignKey(AppInstallation, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=40)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["installation", "user", "role"], name="workspace_app_grant_once"),
        ]


class AppRecord(models.Model):
    """One record in one of an installation's collections. `data` holds only
    the fields the collection declares, validated on every write."""

    installation = models.ForeignKey(AppInstallation, on_delete=models.CASCADE, related_name="records")
    space = models.CharField(max_length=40, default=LIVE)
    collection = models.CharField(max_length=40)
    data = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    #: Who wrote it, for a public action with no account: the address they gave.
    created_by_label = models.CharField(max_length=254, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["installation", "space", "collection", "-created_at"])]


class AppRecordEvent(models.Model):
    """Every action that wrote a record, append-only: the record's history, the
    app's audit log, and what public actions are rate-limited by."""

    installation = models.ForeignKey(AppInstallation, on_delete=models.CASCADE, related_name="events")
    space = models.CharField(max_length=40, default=LIVE)
    #: Null once the record is deleted — the event of deleting it remains.
    record = models.ForeignKey(AppRecord, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    record_ref = models.BigIntegerField()
    collection = models.CharField(max_length=40)
    action = models.CharField(max_length=40)
    op = models.CharField(max_length=10)
    #: {field: [before, after]} for an update; the written fields for a create.
    changes = models.JSONField(default=dict)
    #: What the action's effects did: [{"type": "email", "to": …, "status": …}].
    effects = models.JSONField(default=list, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    actor_label = models.CharField(max_length=254, blank=True, default="")
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["installation", "action", "client_ip", "-at"])]


class AppFile(models.Model):
    """A file a `file` field points at (a résumé). The bytes live in canopy's
    attachment bucket (`apps/canopy_sessions/attachment_storage.py`); `local`
    holds them only on a box with no bucket and DEBUG on (a dev laptop)."""

    installation = models.ForeignKey(AppInstallation, on_delete=models.CASCADE, related_name="files")
    space = models.CharField(max_length=40, default=LIVE)
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=500, blank=True, default="")
    local = models.BinaryField(null=True, blank=True)
    #: Set when a record first points at it; an unclaimed upload is swept.
    claimed = models.BooleanField(default=False)
    uploaded_by_label = models.CharField(max_length=254, blank=True, default="")
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AppScheduledEmail(models.Model):
    """An action's email held back until `send_at` (a rejection sent the next
    morning, not the minute someone clicked). Rendered when the action ran; sent
    by the clock only if its record still exists and still matches `when`, so an
    un-rejected candidate never gets the letter."""

    SCHEDULED, SENT, CANCELLED, FAILED = "scheduled", "sent", "cancelled", "failed"
    STATUSES = [(s, s) for s in (SCHEDULED, SENT, CANCELLED, FAILED)]

    installation = models.ForeignKey(AppInstallation, on_delete=models.CASCADE, related_name="scheduled_emails")
    #: Deleting the record (retention, a removed candidate) takes its letter with it.
    record = models.ForeignKey(AppRecord, on_delete=models.CASCADE, related_name="scheduled_emails")
    action = models.CharField(max_length=40)
    to = models.TextField()
    subject = models.CharField(max_length=200)
    body = models.TextField()
    when = models.JSONField(default=dict, blank=True)
    send_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=STATUSES, default=SCHEDULED)
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "send_at"])]
