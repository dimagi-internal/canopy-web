"""Local apps: programs from Canopy's store that run on a person's own computer.

The catalog is shared with workspace apps: an approved version is an
`AppTemplateVersion` of an `AppTemplate` with `runtime="local"`. What's here is
what's different because this is real code running as a person:

* `LocalAppSubmission` — one version's exact bytes, on its way through review.
  Released and revoked state live here, not on the template version, so a version
  can stop running everywhere while the record of what shipped stays.
* `LocalAppInstallation` — one app, for one person, on one of their computers. The
  row they made while signed in is the authority for everything the runner's own
  token later asks the server to do for this app.
* `LocalAppRun` — one Start, from its first heartbeat to its stop reason. It holds
  counts and ids, never anything said.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class LocalAppSubmission(models.Model):
    key = models.CharField(max_length=40)
    version = models.CharField(max_length=40)
    #: Never edited: a change is a new version.
    manifest = models.JSONField()
    sha256 = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    #: The bytes: in the attachment bucket, or `local` on a box with none (a dev laptop).
    storage_key = models.CharField(max_length=500, blank=True, default="")
    local = models.BinaryField(null=True, blank=True)
    #: `package.facts`, worked out once when it arrived.
    facts = models.JSONField(default=dict)
    #: The author's words to the reviewer, shown as theirs.
    note = models.TextField(blank=True, default="")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.PROTECT, related_name="+")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    submitted_at = models.DateTimeField(auto_now_add=True)
    approval = models.ForeignKey(
        "approvals.ApprovalRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Set by the approval's handler, and only there.
    release = models.OneToOneField(
        "workspace_apps.AppTemplateVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="local_submission",
    )
    released_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=500, blank=True, default="")
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-submitted_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["key", "version"], name="local_app_version_once")]

    @property
    def status(self) -> str:
        if self.revoked_at:
            return "revoked"
        if self.released_at:
            return "released"
        return self.approval.status if self.approval_id else "submitted"


class LocalAppInstallation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    runner = models.ForeignKey("harness.Runner", on_delete=models.CASCADE, related_name="+")
    #: Where its sessions land. Its agent must belong to this workspace.
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    template = models.ForeignKey("workspace_apps.AppTemplate", on_delete=models.PROTECT, related_name="+")
    settings = models.JSONField(default=dict, blank=True)
    #: The last version they read and agreed to, and that version's trust surface.
    accepted = models.ForeignKey(LocalAppSubmission, on_delete=models.PROTECT, related_name="+")
    accepted_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["template__name", "id"]
        constraints = [models.UniqueConstraint(fields=["user", "runner", "template"], name="local_app_install_once")]


class LocalAppRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(LocalAppInstallation, on_delete=models.CASCADE, related_name="runs")
    version = models.ForeignKey(LocalAppSubmission, on_delete=models.PROTECT, related_name="+")
    #: The source it listens to, so that revoking the source reaches this run too.
    source_version = models.ForeignKey(LocalAppSubmission, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    last_beat_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=40, blank=True, default="")
    counts = models.JSONField(default=dict, blank=True)
    stop_reason = models.CharField(max_length=80, blank=True, default="")
    #: A person pressed Stop on the website. A durable row, read at the next heartbeat.
    stop_requested = models.BooleanField(default=False)
    #: The sessions this run opened: the only ones it may send to or read turns of.
    session_ids = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["installation", "-started_at"])]
