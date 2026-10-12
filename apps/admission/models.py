"""Admission rules: the one place canopy decides "this turn will not run".

Some turns can never complete. An email from a confined caller, in manual mode,
claimed by a cloud runner is the worked example (canopy-web#1400): the ask
procedure drafts a reply and waits for a human yes in a session nobody can
reach, so the turn spends an LLM session and ends with nothing sent and nobody
told. An `AdmissionRule` names a combination like that and rejects it before
any session starts: the turn is closed CANCELLED, the sender gets a fixed-text
automatic reply on their thread, and the agent's owner gets a separate notice
saying which rule fired and where to change it.

Rules are WORKSPACE data, edited by the workspace's admins at
`/w/:workspace/settings/admission-rules` — nothing canopy-wide is hard-coded.
The defaults (`defaults.py`) are seeded into every workspace as ordinary rows.

Every condition but `workspace` is a filter; blank/null means "any". Where and
how they are evaluated is `rules.py`.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class AdmissionRule(models.Model):
    REJECT, ALLOW = "reject", "allow"
    ACTION_CHOICES = [
        (REJECT, "Reject: close the turn before it runs"),
        (ALLOW, "Allow: exempt matching turns from every reject rule"),
    ]

    # The Turn.origin vocabulary. Not imported from harness so a migration
    # stays readable; `tests/test_admission.py` pins it to Turn.ORIGIN_CHOICES.
    ORIGIN_CHOICES = [
        ("api", "API"), ("ace_web", "ace-web"), ("canopy_web_chat", "canopy-web chat"),
        ("canopy_scheduler", "canopy scheduler"), ("email", "Email"), ("slack", "Slack"),
    ]

    MEMBER, CONTACT, AGENT, SYSTEM = "member", "contact", "agent", "system"
    PRINCIPAL_CHOICES = [
        (MEMBER, "Member: a canopy user"),
        (CONTACT, "Contact: an outside person with no account"),
        (AGENT, "Agent: another agent dispatched it"),
        (SYSTEM, "System: a schedule, a drill, or nobody established"),
    ]

    CONFINED, FULL, NONE = "confined", "full", "none"
    ACCESS_CHOICES = [
        (CONFINED, "Confined: a caller held to one capability"),
        (FULL, "Full: the agent's whole profile"),
        (NONE, "None: offered nothing, so refused"),
    ]

    #: The sender's standing with the workspace. Only `blocked` is a condition.
    BLOCKED = "blocked"
    SENDER_CHOICES = [(BLOCKED, "Blocked: a contact the workspace blocked")]

    MANUAL, AUTO = "manual", "auto"
    TURN_MODE_CHOICES = [(MANUAL, "Manual"), (AUTO, "Auto")]

    # Runner.KIND_CHOICES. `emdash` is a laptop running emdash; `cloud` a cloud box.
    RUNNER_KIND_CHOICES = [("emdash", "Laptop (emdash)"), ("cloud", "Cloud"), ("remote", "Remote")]

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="admission_rules",
    )
    #: Null = every agent in the workspace.
    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, null=True, blank=True,
        related_name="admission_rules",
    )
    origin = models.CharField(max_length=32, choices=ORIGIN_CHOICES, blank=True, default="")
    principal = models.CharField(max_length=8, choices=PRINCIPAL_CHOICES, blank=True, default="")
    access = models.CharField(max_length=8, choices=ACCESS_CHOICES, blank=True, default="")
    #: A capability name from the agent's interface (`ask`), blank = any.
    capability = models.CharField(max_length=64, blank=True, default="")
    turn_mode = models.CharField(max_length=8, choices=TURN_MODE_CHOICES, blank=True, default="")
    #: `blocked` matches a sender the workspace blocked (`Contact.is_blocked`).
    sender = models.CharField(max_length=8, choices=SENDER_CHOICES, blank=True, default="")
    runner_kind = models.CharField(max_length=10, choices=RUNNER_KIND_CHOICES, blank=True, default="")

    action = models.CharField(max_length=8, choices=ACTION_CHOICES, default=REJECT)
    reason = models.TextField(help_text="Why matching turns are rejected; shown to the owner.")
    notify_sender = models.BooleanField(default=False)
    notify_owner = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    #: Which seeded default this row started as (`defaults.py`); blank for a rule
    #: someone added. Identity for the seed only — editing the row is fine.
    seed_key = models.CharField(max_length=64, blank=True, default="")

    fired_count = models.PositiveIntegerField(default=0)
    last_fired_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["workspace_id", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "seed_key"], condition=~models.Q(seed_key=""),
                name="one_seeded_admission_rule_per_workspace",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"admission:{self.workspace_id}:{self.pk}:{self.action}"

    #: The conditions, in the order a summary reads them.
    CONDITIONS = ("origin", "principal", "sender", "access", "capability", "turn_mode", "runner_kind")
    #: Conditions only known once a runner claims the turn.
    CLAIM_TIME = ("turn_mode", "runner_kind")

    @property
    def is_refusal(self) -> bool:
        """A rule about turns canopy refuses on its own — a blocked sender, or
        a person the agent offers nothing. It cannot let them run: it decides
        only what such a refusal records and who is told (`evaluate.at_refusal`)."""
        return self.sender == self.BLOCKED or self.access == self.NONE

    @property
    def specificity(self) -> int:
        return sum(bool(getattr(self, f)) for f in self.CONDITIONS) + bool(self.agent_id)

    def clean(self) -> None:
        if self.agent_id and self.workspace_id and self.agent.workspace_id != self.workspace_id:
            raise ValidationError({"agent": "that agent lives in another workspace"})
        if self.is_refusal and self.action == self.ALLOW:
            raise ValidationError(
                "a blocked sender or a person offered nothing is always refused; "
                "unblock the sender or change the agent's interface instead")
        if self.is_refusal and (self.turn_mode or self.runner_kind):
            raise ValidationError(
                "a refusal happens before any runner or mode is chosen, so it cannot "
                "be narrowed by turn mode or runner")
        if not self.specificity:
            # A rule with no conditions rejects (or exempts) every turn in the
            # workspace. Disabling the agent is the honest way to do that.
            raise ValidationError("a rule needs at least one condition")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class AdmissionNotice(models.Model):
    """One notice a rejection owed: to the sender (on their thread) or the owner.

    Recorded whether or not it went out, so "was Vikram told?" has an answer,
    and so the sender's at-most-one-per-thread-per-day limit has something to
    count.
    """

    SENDER, OWNER = "sender", "owner"
    KIND_CHOICES = [(SENDER, "Sender"), (OWNER, "Owner")]

    SENT = "sent"                      # delivered
    HELD = "held"                      # delivery is switched off on this deployment
    SKIPPED = "skipped"                # deliberately not sent (loop safety, throttle)
    NOT_CONFIGURED = "not_configured"  # the transport is off (SES not enabled)
    FAILED = "failed"                  # tried, and nothing more will try
    #: canopy-web could not send it, so the agent's runner owes it: the runner
    #: that reads the mailbox claims it on its next poll and sends it through
    #: its own gog client (`runner_notices.py`, canopy-web#1416).
    PENDING = "pending"
    STATUS_CHOICES = [(s, s) for s in (SENT, HELD, SKIPPED, NOT_CONFIGURED, FAILED, PENDING)]

    turn = models.ForeignKey("harness.Turn", on_delete=models.CASCADE, related_name="admission_notices")
    rule = models.ForeignKey(AdmissionRule, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="notices")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    #: Gmail thread id, for a sender notice on an email turn.
    thread_id = models.CharField(max_length=64, blank=True, default="")
    recipient = models.CharField(max_length=320, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # What a runner needs to send a PENDING sender notice exactly as canopy-web
    # would have: the same fixed text, from the same mailbox, on the same message.
    #: The mailbox the reply must come from (the agent's address).
    mailbox = models.CharField(max_length=320, blank=True, default="")
    #: Gmail's id of the message being answered ("" = the thread's newest).
    message_id = models.CharField(max_length=64, blank=True, default="")
    subject = models.CharField(max_length=998, blank=True, default="")
    body = models.TextField(blank=True, default="")
    #: The runner holding a PENDING notice, and since when. A claim is a lease
    #: (`runner_notices.LEASE`), so a runner that dies mid-send does not keep it.
    claimed_by = models.ForeignKey("harness.Runner", on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="+")
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["agent", "kind", "thread_id", "created_at"]),
                   models.Index(fields=["status", "kind"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"notice:{self.kind}:{self.turn_id}:{self.status}"
