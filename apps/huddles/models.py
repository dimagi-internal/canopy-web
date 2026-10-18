"""Project check-ins — the one stored thing in apps/huddles (spec
docs/superpowers/specs/2026-10-17-project-check-ins-design.md).

A HUDDLE stays derived (services.py): turns, close-outs and board tasks. The
PROJECT is the organizing unit; a check-in is how it meets: a recurring meeting
on one project — a cadence, a lead, the agents that take part, optionally a
Slack channel — and `CheckinRun` is the record of each time it came due.

The rule every run keeps: a channel is read only because the LEAD pressed
Confirm on the run's announcement, in Slack (checkins.click). The schedule only
announces; no API, web or agent path starts a read. A check-in without a channel
reads nothing: its schedule queues the turn directly.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ProjectCheckin(models.Model):
    SOLO, HUDDLE = "solo", "huddle"

    #: The project this meeting is about. Its leading agent is `project.agent`.
    project = models.ForeignKey("agents.AgentProject", on_delete=models.CASCADE, related_name="checkins")
    #: "Weekly check-in" — unique per project; the URL names it.
    name = models.CharField(max_length=200)
    #: The brief the meeting works from; blank means the project's outcome.
    topic = models.TextField(blank=True, default="")
    #: Required: the person each occurrence answers to. With a channel, a member of
    #: the project's workspace AND of the channel, checked on save and at every
    #: announcement. Nullable only so a departed account does not cascade the
    #: check-in away; a check-in without a lead never runs.
    lead = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                             related_name="checkins_led")
    lead_slack_id = models.CharField(max_length=32, blank=True, default="")
    #: Optional. Without a channel there is no announcement and nothing is read.
    installation = models.ForeignKey("slack.SlackInstallation", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="checkins")
    channel_id = models.CharField(max_length=32, blank=True, default="")
    channel_name = models.CharField(max_length=128, blank=True, default="")
    #: Agent slugs (≥1), always including `project.agent`. One → solo; more → a huddle
    #: led by the project's agent, whose login must be able to RUN every other one.
    agents = models.JSONField(default=list)
    #: On `project.agent`: firing it ANNOUNCES a run (harness.schedule_turns), or —
    #: with no channel — queues the run's turn directly.
    schedule = models.OneToOneField("harness.AgentSchedule", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="checkin")
    enabled = models.BooleanField(default=True)
    #: True when saving this check-in is what put `channel_id` on the agent's reading
    #: list. An entry that was already there is reused and never removed by it.
    added_allowance = models.BooleanField(default=False)
    #: The work group this was migrated from (its slug), so /huddles/groups/<slug>
    #: still finds its way to the project. Blank for a check-in made as one.
    legacy_slug = models.CharField(max_length=64, blank=True, default="", db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="checkins_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="uniq_checkin_name_per_project"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"checkin:{self.project_id}/{self.name}"

    @property
    def agent(self):
        return self.project.agent

    @property
    def workspace_id(self) -> str:
        return self.project.agent.workspace_id

    @property
    def format(self) -> str:
        return self.SOLO if len(self.agents or []) <= 1 else self.HUDDLE

    @property
    def has_channel(self) -> bool:
        return bool(self.channel_id and self.installation_id)


class CheckinRun(models.Model):
    AWAITING_CONFIRM, READING, RUNNING, DONE = "awaiting_confirm", "reading", "running", "done"
    SKIPPED, EXPIRED, FAILED = "skipped", "expired", "failed"
    STATUS_CHOICES = [(s, s) for s in (AWAITING_CONFIRM, READING, RUNNING, DONE, SKIPPED, EXPIRED, FAILED)]
    OPEN = (AWAITING_CONFIRM, READING, RUNNING)

    #: SET_NULL: deleting a check-in keeps the record of its meetings on the project.
    checkin = models.ForeignKey(ProjectCheckin, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="runs")
    #: Denormalised, so the project timeline reads without the (maybe deleted) check-in.
    project = models.ForeignKey("agents.AgentProject", on_delete=models.CASCADE, related_name="checkin_runs")
    #: The check-in's name when the run was made (the timeline's label once it is gone).
    checkin_name = models.CharField(max_length=200, blank=True, default="")
    #: The schedule slot it answers (a Run now uses the moment it was pressed).
    slot = models.DateTimeField()
    manual = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=AWAITING_CONFIRM)
    #: Why it ended skipped/expired/failed, in a sentence.
    note = models.CharField(max_length=500, blank=True, default="")
    #: Who took part, as it was: `{agents: [...], lead_email}` — history holds when membership changes.
    participants = models.JSONField(default=dict, blank=True)
    #: The channel it read (blank: a check-in without one). Kept for the timeline.
    channel_id = models.CharField(max_length=32, blank=True, default="")
    channel_name = models.CharField(max_length=128, blank=True, default="")
    #: The announcement in the channel: its buttons, and its thread for every later line.
    announce_ts = models.CharField(max_length=32, blank=True, default="")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    confirmed_by_slack = models.CharField(max_length=32, blank=True, default="")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    ingest = models.ForeignKey("slack.SlackChannelIngest", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    read_messages = models.PositiveIntegerField(default=0)
    read_first_ts = models.CharField(max_length=32, blank=True, default="")
    read_last_ts = models.CharField(max_length=32, blank=True, default="")
    #: Where the read starts, fixed at announce (the check-in's last read, or one cadence back).
    read_since = models.DateTimeField(null=True, blank=True)
    #: The channel as read (Markdown, quoted material) — what the agent's turn works from.
    material = models.TextField(blank=True, default="")
    turn = models.ForeignKey("harness.Turn", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    huddle_id = models.CharField(max_length=128, blank=True, default="")
    result_url = models.CharField(max_length=500, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    #: How many tasks it proposed in the project, set at finish.
    tasks_proposed = models.PositiveIntegerField(default=0)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["checkin", "slot"], name="uniq_checkin_run_slot"),
        ]
        indexes = [models.Index(fields=["status", "created_at"], name="checkin_run_status_idx"),
                   models.Index(fields=["project", "created_at"], name="checkin_run_project_idx")]

    def __str__(self) -> str:  # pragma: no cover
        return f"checkin-run:{self.pk} ({self.status})"
