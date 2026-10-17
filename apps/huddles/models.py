"""Work groups — the one stored thing in apps/huddles (spec
docs/superpowers/specs/2026-10-16-work-group-huddles-design.md, layer 3).

A HUDDLE stays derived (services.py): turns, close-outs and board tasks. What a
work group adds is a standing arrangement no turn can carry — a lead, a Slack
channel, the agents that work it and a cadence — and the record of each time it
came due (`HuddleGroupRun`): announced in the channel, confirmed or skipped by
the lead, read, handed to the leader agent, finished.

The rule every run keeps: the channel is read only because the LEAD pressed
Confirm on the run's announcement, in Slack (groups.confirm). The schedule only
announces; no API, web or agent path starts a read.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class HuddleGroup(models.Model):
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="huddle_groups")
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=200)
    #: The work group's purpose, in one paragraph — the brief a huddle runs on.
    topic = models.TextField(blank=True, default="")
    #: Required: a member of `workspace` AND of the channel, checked on save and
    #: again at every announcement. Nullable only so a departed account does not
    #: cascade the group away; a group without a lead never announces.
    lead = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                             related_name="huddle_groups_led")
    lead_slack_id = models.CharField(max_length=32, blank=True, default="")
    installation = models.ForeignKey("slack.SlackInstallation", on_delete=models.CASCADE,
                                     related_name="huddle_groups")
    channel_id = models.CharField(max_length=32)
    channel_name = models.CharField(max_length=128, blank=True, default="")
    #: Agent slugs (≥1). One → single mode; more → the leader runs a huddle.
    agents = models.JSONField(default=list)
    #: Any agent in the workspace tree whose login may RUN every other agent in
    #: `agents` (an owner/admin edge, apps/agents/topology.can_run). Its schedule
    #: announces, the channel is read for it, and its turn runs the group.
    leader_agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE,
                                     related_name="huddle_groups_led")
    #: The leader's schedule for this group: firing it ANNOUNCES a run
    #: (harness.schedule_turns.fire_schedule), it never enqueues a turn itself.
    schedule = models.OneToOneField("harness.AgentSchedule", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="huddle_group")
    #: Single mode: the project the one agent works. Multi mode: null.
    project = models.ForeignKey("agents.AgentProject", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="huddle_groups")
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="huddle_groups_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="uniq_huddle_group_slug"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"group:{self.workspace_id}/{self.slug}"

    @property
    def single(self) -> bool:
        return len(self.agents or []) <= 1


class HuddleGroupRun(models.Model):
    AWAITING_CONFIRM, READING, RUNNING, DONE = "awaiting_confirm", "reading", "running", "done"
    SKIPPED, EXPIRED, FAILED = "skipped", "expired", "failed"
    STATUS_CHOICES = [(s, s) for s in (AWAITING_CONFIRM, READING, RUNNING, DONE, SKIPPED, EXPIRED, FAILED)]
    OPEN = (AWAITING_CONFIRM, READING, RUNNING)

    group = models.ForeignKey(HuddleGroup, on_delete=models.CASCADE, related_name="runs")
    #: The schedule slot it answers (a Run now uses the moment it was pressed).
    slot = models.DateTimeField()
    manual = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=AWAITING_CONFIRM)
    #: Why it ended skipped/expired/failed, in a sentence.
    note = models.CharField(max_length=500, blank=True, default="")
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
    #: The previous read this one continued from (null = the channel's first read).
    read_since = models.DateTimeField(null=True, blank=True)
    #: The channel as read (Markdown, quoted material) — what the leader's turn works from.
    material = models.TextField(blank=True, default="")
    turn = models.ForeignKey("harness.Turn", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    huddle_id = models.CharField(max_length=128, blank=True, default="")
    result_url = models.CharField(max_length=500, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["group", "slot"], name="uniq_huddle_group_run_slot"),
        ]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"group-run:{self.pk} ({self.status})"
