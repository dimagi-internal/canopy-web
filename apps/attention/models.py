from django.conf import settings
from django.db import models


class Hidden(models.Model):
    """Something a person has taken off their own list, without deciding it.

    Not a decline, not a close, not a "done": the task, chat or request is
    unchanged and nobody is told. It only leaves THIS person's list, until
    something new happens on it (its `last_activity_at` passes `hidden_at`) or,
    when `until` is set, until then — whichever comes first.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hidden_attention")
    key = models.CharField(max_length=200)
    hidden_at = models.DateTimeField()
    until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="attention_hidden_user_key")]


class Alert(models.Model):
    """One persisted thing that needs a person: the unified alert row.

    A source (`apps/<app>/alerts.py`) owns the subject and says when it is true;
    the framework owns this row's lifecycle (open / dismissed / resolved). One row
    per `(kind, subject_key)` — a recurrence re-opens it rather than adding another.

    `workspace` is NOT NULL on purpose: it is the subject's workspace and the
    tenancy boundary for who may see the alert. An alert with no workspace would
    mean "visible to everyone", which is never the intent.
    """

    BLOCKING = "blocking"
    ACTION = "action"
    SETUP = "setup"
    SEVERITY_CHOICES = [(BLOCKING, "Blocking"), (ACTION, "Action"), (SETUP, "Setup")]
    SEVERITY_RANK = {BLOCKING: 0, ACTION: 1, SETUP: 2}

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    STATUS_CHOICES = [(OPEN, "Open"), (RESOLVED, "Resolved"), (DISMISSED, "Dismissed")]

    REASON_SOURCE = "source"
    REASON_ACTION = "action"
    REASON_STALE = "stale"
    RESOLVED_REASON_CHOICES = [(REASON_SOURCE, "Source"), (REASON_ACTION, "Action"), (REASON_STALE, "Stale")]

    kind = models.CharField(max_length=60)
    subject_key = models.CharField(max_length=300)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="alerts")
    scopes = models.JSONField(default=list, blank=True)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES)
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True, default="")
    url = models.CharField(max_length=500, blank=True, default="")
    fingerprint = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN)
    resolved_reason = models.CharField(max_length=10, choices=RESOLVED_REASON_CHOICES, blank=True, default="")
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    occurrences = models.PositiveIntegerField(default=1)
    notified_at = models.DateTimeField(null=True, blank=True)
    # WHO HAS IT: an agent, a person, or nobody yet (both NULL — the old meaning, "it
    # needs a person", and everyone who holds a closing action is told when it
    # is blocking). An agent-held alert is shown on canopy-web and is SILENT: no
    # one is emailed or pushed while an agent works it. Handing it to a person
    # tells that one person, once (`alerts._assign`), and is an `assigned` event.
    #
    # Why (Jonathan, 2026-10-23): a turn failure used to be an alert only once it
    # was a person's, and before that it lived in a separate FailureInvestigation
    # with its own lifecycle; the only verb the debugger had was "resolve", so
    # even "needs Jonathan" resolved it, which cleared the alert and re-armed the
    # next escalation — investigation 10 ran two debug turns two minutes apart
    # (2026-10-22 15:02 / 15:04), each one emailing him. One alert, assigned to
    # whoever is working it, is the lifecycle he asked for: "in a clean alert ->
    # ada fix -> resolved, I don't want to be notified at all".
    handler_agent = models.ForeignKey(
        "agents.Agent", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    handler_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "subject_key"], name="attention_alert_kind_subject")]
        indexes = [models.Index(fields=["status", "workspace"], name="attention_alert_status_ws")]


class AlertEvent(models.Model):
    """One row of an alert's audit trail — written in the same transaction as the change.

    Its own table, not `apps/events`: that log coalesces rows, prunes after 30
    days and is admin-only to read. This one is never purged by retention
    (retention only acts on subjects it lists explicitly, and this is not one).
    """

    RAISED = "raised"
    CHANGED = "changed"
    ACTED = "acted"
    NOTIFIED = "notified"
    DISMISSED = "dismissed"
    UNDISMISSED = "undismissed"
    RESOLVED = "resolved"
    REOPENED = "reopened"
    #: The handler changed (`Alert.handler_agent` / `handler_user`). Payload:
    #: `{"agent": slug|None, "user": id|None, "ask": str}`.
    ASSIGNED = "assigned"
    KIND_CHOICES = [(k, k) for k in (RAISED, CHANGED, ACTED, NOTIFIED, DISMISSED, UNDISMISSED, RESOLVED, REOPENED,
                                     ASSIGNED)]

    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField()
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action_id = models.CharField(max_length=100, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at"]
