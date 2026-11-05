"""A meeting, and everything anyone has said about it.

A meeting's text arrives from wherever it was made, in any combination: a Google Meet
transcript with Gemini's notes, a Superwhisper transcript with someone else's summary.
So a meeting holds any number of artifacts and doesn't care which capture produced them.
Live capture is one more source, and nothing here depends on it.

Private to its owner until they attach it to a project; attaching is what shares it, with
whoever can read that project. A project is how meetings connect over time, because a
series doesn't reliably keep one calendar id. The calendar ids here are hints.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Meeting(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The tenant boundary. The attached project's workspace once it has one.
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="meetings")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="meetings")
    #: SET_NULL: losing a project leaves the meeting with its owner, private again.
    project = models.ForeignKey(
        "agents.AgentProject", null=True, blank=True, on_delete=models.SET_NULL, related_name="meetings"
    )
    #: The agent its owner asked for help. Its login may read this meeting even while it is
    #: private. Only the owner's asking sets it: a teammate's agent reads through the project.
    agent = models.ForeignKey(
        "agents.Agent", null=True, blank=True, on_delete=models.SET_NULL, related_name="meetings"
    )
    title = models.CharField(max_length=300, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    #: [{"name": "...", "email": "..."}], as far as anyone knows.
    attendees = models.JSONField(default=list, blank=True)
    #: Their names and addresses as plain words, for search: a word mustn't match the JSON's own keys.
    attendee_words = models.TextField(blank=True, default="")
    calendar_event_id = models.CharField(max_length=255, blank=True, default="")
    #: Google's recurringEventId. A hint: series get rescheduled into new ids.
    calendar_series_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-created_at"]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="meeting_owner_recent"),
            models.Index(fields=["project", "-started_at"], name="meeting_project_recent"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.title or str(self.pk)


class MeetingArtifact(models.Model):
    TRANSCRIPT, SUMMARY, NOTES, RECAP = "transcript", "summary", "notes", "recap"
    KIND_CHOICES = [
        (TRANSCRIPT, "Transcript"),
        (SUMMARY, "Summary"),
        (NOTES, "Notes"),
        (RECAP, "Agent's follow-up"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="artifacts")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    #: Who made it: "google_meet", "gemini", "granola", "superwhisper", "email", "pasted", ...
    source = models.CharField(max_length=40, blank=True, default="")
    #: Where it came from, when that is a place: a Doc link. Pointing at a Doc before anyone
    #: has read it gives an artifact with this and no text yet.
    source_url = models.URLField(max_length=1000, blank=True, default="")
    text = models.TextField(blank=True, default="")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["source_url"], name="meeting_artifact_url")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.kind} of {self.meeting_id}"
