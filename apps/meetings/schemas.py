from __future__ import annotations

import datetime as dt

from ninja import Field, Schema


class MeetingAttendee(Schema):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=254)


class MeetingArtifactIn(Schema):
    #: transcript, summary, notes or recap.
    kind: str
    #: The text. Leave it out to point at `source_url` for an agent to read.
    text: str = ""
    #: Who made it: google_meet, gemini, granola, superwhisper, email, pasted...
    source: str = Field(default="", max_length=40)
    source_url: str = Field(default="", max_length=1000)


class MeetingIn(Schema):
    #: Where a private meeting lives. Taken from the project or agent when they're given.
    workspace: str = ""
    title: str = Field(default="", max_length=300)
    started_at: dt.datetime | None = None
    attendees: list[MeetingAttendee] = []
    #: A project's address ("connect/weekly-sync"). Attaching shares it with that project.
    project: str = ""
    #: The agent to help. Defaults to the project's lead agent.
    agent: str = ""
    calendar_event_id: str = Field(default="", max_length=255)
    calendar_series_id: str = Field(default="", max_length=255)
    artifact: MeetingArtifactIn | None = None
    #: Start the conversation with the agent now.
    follow_up: bool = False
    #: What the person wants help with.
    ask: str = Field(default="", max_length=4000)


class MeetingPatch(Schema):
    title: str | None = Field(default=None, max_length=300)
    started_at: dt.datetime | None = None
    attendees: list[MeetingAttendee] | None = None
    #: A project's address. Empty detaches it, which makes it private again.
    project: str | None = None
    calendar_event_id: str | None = Field(default=None, max_length=255)
    calendar_series_id: str | None = Field(default=None, max_length=255)


class MeetingFollowUpIn(Schema):
    agent: str = ""
    ask: str = Field(default="", max_length=4000)


class MeetingArtifactOut(Schema):
    id: int
    kind: str
    source: str
    source_url: str
    text: str
    added_by: str
    created_at: dt.datetime


class MeetingProjectOut(Schema):
    address: str
    handle: str
    workspace: str
    name: str


class MeetingRowOut(Schema):
    id: str
    workspace: str
    title: str
    started_at: dt.datetime | None
    created_at: dt.datetime
    owner: str
    is_mine: bool
    attendees: list[MeetingAttendee]
    project: MeetingProjectOut | None
    #: "<kind>:<source>" of what's saved on it.
    has: list[str]
    #: Around the first word found, when this came from a search.
    snippet: str = ""


class MeetingOut(MeetingRowOut):
    agent: str
    calendar_event_id: str
    calendar_series_id: str
    session_id: str | None
    session_url: str | None
    artifacts: list[MeetingArtifactOut]
    url: str
    #: The instructions it would follow up under.
    meeting_instructions: str
    may_change: bool


class MeetingListOut(Schema):
    meetings: list[MeetingRowOut]
    has_more: bool


class MeetingFollowUpOut(Schema):
    session_id: str
    session_url: str
    turn_id: str | None
