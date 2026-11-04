"""Agent schedules.

Framework tier. Split from the former models.py; `apps.harness.models` re-exports.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from .turn import Turn


def _default_notify() -> list:
    """Callable default — a mutable literal would be shared across rows."""
    return ["inbox"]


class AgentSchedule(models.Model):
    """An agent's TRIGGER: something that starts a turn on this agent, with the
    authority of the person who set it up. Two kinds (`kind`):

    * `cron` — a recurring (or one-off) turn: "Echo's weekly manager report,
      Fridays 9am ET". Everything below about slots, supersede and nags is this kind.
    * `inbound_mail` — aligned mail from `sender_address` (subject matching
      `subject_pattern`) to this agent's mailbox fires it: AWS CloudWatch alarm mail
      to Hal. See `apps/harness/triggers.py` and `channels/email.arrive`.

    The model keeps its historical name (schedules were the first trigger kind);
    the concept, the UI and `/api/agents/{slug}/triggers/` say trigger. A trigger's
    turn runs with no more authority than its creator has on the agent, and one
    whose creator lost that right stops firing, loudly (`triggers.refusal`,
    `alerts.trigger`). docs/superpowers/specs/2026-10-27-agent-triggers-design.md.

    Config lives here (server-side, so it is visible and editable in the Agent
    UI); canopy-web fires it itself (schedule_clock.py, a loop in the web
    process), and a laptop runner may too (it syncs these rows, evaluates the
    cron locally, and POSTs back a slot) — both collapse on the slot key. Either
    way the server materializes a normal harness Turn via services.enqueue_turn — the scheduler is a producer
    of turns, not a second execution engine.

    The Turn IS the occurrence (origin=cron, origin_ref={schedule_id, slot},
    idempotency_key="sched:<id>:<slot>"); there is deliberately no occurrence
    table. See docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md.

    An unattended (grace-released) occurrence raises a real Item (kind=review)
    whose `implement` re-runs the schedule's prompt — see
    services._raise_schedule_nag. There is no projection: the nag is a row.

    No workspace FK: a schedule is agent-owned and derives its tenant via
    agent.workspace, exactly as Turn does.
    """

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="schedules")
    # WHO set this up / last changed it. Distinct from `agent` (which agent it fires)
    # and from tenancy (agent.workspace): a schedule is a person's standing request,
    # so the calendar can show "my schedules" and a MISSED occurrence can notify the
    # human who created it. Nullable — SET_NULL so a departed user doesn't cascade the
    # schedule away, and null for pre-attribution rows.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="schedules_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="schedules_updated",
    )
    CRON, INBOUND_MAIL = "cron", "inbound_mail"
    KIND_CHOICES = [(CRON, "Schedule"), (INBOUND_MAIL, "Inbound mail")]
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=CRON, db_default=CRON)
    name = models.CharField(max_length=200, help_text='e.g. "Weekly manager report"')
    prompt = models.TextField(
        blank=True,
        help_text="What the turn is seeded with, e.g. /echo:manager-report. Optional for "
        "inbound_mail: when set it leads the mail turn's own prompt.")
    # A PLAIN JOB: run this named command of the repo's `.canopy/jobs.yaml` on a cloud
    # box, with no model, instead of seeding a prompt. Both or neither. The turn it
    # fires carries both and an empty prompt, and goes only to a box that can run it.
    job = models.CharField(max_length=64, blank=True, default="", db_default="")
    job_repo = models.CharField(max_length=100, blank=True, default="", db_default="")
    cron = models.CharField(max_length=120, blank=True,
                            help_text="5-field cron expression, e.g. '0 9 * * 5' (cron kind only)")
    # inbound_mail only: the sender this trigger answers to. Mail from it fires the
    # trigger only when THIS message is aligned (DMARC, or DKIM signed by the From:
    # domain) on our own receiver's verdict — an automated address such as
    # no-reply@sns.amazonaws.com is every AWS customer's, so the address alone
    # proves nothing; the binding is the agent's decision about some of its mail.
    sender_address = models.CharField(max_length=254, blank=True, default="", db_default="")
    # A Python regex (`re.search`, case-sensitive) narrowing the sender's mail to
    # what is actually this trigger's. Blank matches any subject.
    subject_pattern = models.CharField(max_length=300, blank=True, default="", db_default="")
    # "" = the agent's routing decides (bounded by the creator); manual | auto. Auto
    # holds only while the creator is an agent admin (turn_mode.trigger_posture).
    turn_mode = models.CharField(max_length=8, blank=True, default="", db_default="")
    # inbound_mail only: after a turn on a mail thread finishes, a further message on
    # it waits this long before it starts another turn, and that turn carries every
    # message since (apps/harness/mail_coalesce.py). 0 = no wait. CloudWatch re-fires
    # every evaluation period; without it one incident was six full turns.
    mail_quiet_minutes = models.PositiveIntegerField(
        default=30, db_default=30,
        help_text="inbound_mail: minutes a thread stays quiet after a turn on it finishes "
        "before further mail on it starts another turn (0 = immediately).")
    # A ONE-OFF: set, and the schedule fires exactly once, at this instant, then
    # disables itself. `cron` is still stored — derived from this in `timezone`
    # (schedule_services.one_off_cron) — so a runner that predates the field keeps
    # firing it correctly; `fire_after` is pinned just before this instant so the
    # derived cron's yearly repeats can never come due. Null = recurring.
    run_once_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Fire once at this instant, then disable. Null for a recurring schedule.",
    )
    timezone = models.CharField(max_length=64, default="UTC", help_text="IANA tz, e.g. America/New_York")
    enabled = models.BooleanField(default=True, help_text="Pause without deleting.")
    routing = models.CharField(max_length=15, choices=Turn.ROUTING_CHOICES, default=Turn.PREFER_LOCAL)
    grace_minutes = models.PositiveIntegerField(
        default=120,
        help_text="How long an unattended fired turn may run before it is released as "
        "MISSED. Bounds an abandoned session that would otherwise occupy a runner "
        "indefinitely.",
    )
    always_run = models.BooleanField(
        default=False,
        help_text="Run a slot however late it is claimed. Off: a slot not claimed "
        "within LATE_SLOT_WINDOW_MINUTES of its time is skipped as MISSED, so a "
        "laptop reopened after a day away does not fire every schedule at once.",
    )
    notify = models.JSONField(
        default=_default_notify, blank=True,
        help_text='Channel ids resolved through the notify registry, e.g. ["inbox"].',
    )
    # The agent CLI its turns run on (`clis.refusal` checks it is one the agent is
    # set up for); "" = the agent's default, as for any turn that names none.
    cli = models.CharField(max_length=32, blank=True, default="", db_default="")
    last_slot = models.DateTimeField(
        null=True, blank=True,
        help_text="Newest slot fired. The supersede + no-backfill anchor.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["agent", "enabled"])]
        constraints = [
            models.UniqueConstraint(fields=["agent", "name"], name="uniq_agent_schedule_name"),
            # A cron trigger has a cron and no sender; a mail trigger the reverse.
            models.CheckConstraint(
                condition=(
                    (models.Q(kind="cron") & ~models.Q(cron="") & models.Q(sender_address=""))
                    | (models.Q(kind="inbound_mail") & models.Q(cron="")
                       & ~models.Q(sender_address=""))
                ),
                name="agent_trigger_kind_shape",
            ),
            # Both or neither, and only a cron trigger runs one.
            models.CheckConstraint(
                condition=(
                    models.Q(job="", job_repo="")
                    | (~models.Q(job="") & ~models.Q(job_repo="") & models.Q(kind="cron"))
                ),
                name="agent_trigger_job_shape",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{'sched' if self.kind == self.CRON else 'trigger'}:{self.agent.slug}:{self.name}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug
