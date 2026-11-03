"""Schedule and trigger schemas."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from canopy_cron import validate_cron, validate_timezone
from ninja import Schema
from pydantic import Field, field_validator, model_validator

from .. import clis as cli_vocab


class ScheduleIn(Schema):
    """Create payload. Cron + tz validate here so a bad expression 422s as
    problem+json at edit time — a typo that silently never fires is the worst
    failure mode a scheduler has."""

    name: str
    prompt: str
    # WHEN — exactly one. `cron` is a recurring schedule; `run_once_at` is a
    # one-off that fires at that instant and then disables itself (its cron is
    # derived server-side).
    cron: str | None = None
    run_once_at: dt.datetime | None = None
    timezone: str = "UTC"
    enabled: bool = True
    routing: str = "prefer_local"
    grace_minutes: int = 120
    always_run: bool = False
    notify: list[str] = ["inbox"]
    # The agent CLI its turns run on (one the agent is set up for); "" = the
    # agent's default.
    cli: cli_vocab.CliOrDefault = ""

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        return validate_cron(v) if v is not None else v

    @model_validator(mode="after")
    def _exactly_one_when(self):
        if bool(self.cron) == bool(self.run_once_at):
            raise ValueError("give exactly one of cron (recurring) or run_once_at (one-off)")
        return self

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_timezone(v)

    @field_validator("name", "prompt")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("must not be blank")
        return v.strip()


class SchedulePatch(Schema):
    """Partial update. Every field optional; the same validators apply to any
    field actually supplied."""

    name: str | None = None
    prompt: str | None = None
    # Setting `cron` makes the schedule recurring; setting `run_once_at` makes it
    # a one-off (and re-arms one that already fired). Not both.
    cron: str | None = None
    run_once_at: dt.datetime | None = None
    timezone: str | None = None
    enabled: bool | None = None
    routing: str | None = None
    grace_minutes: int | None = None
    always_run: bool | None = None
    notify: list[str] | None = None
    cli: cli_vocab.CliOrDefault | None = None

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        return validate_cron(v) if v is not None else v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        return validate_timezone(v) if v is not None else v


class ScheduleOut(Schema):
    id: int
    agent_slug: str
    name: str
    prompt: str
    cron: str
    # Set for a one-off: it fires once, at this instant, then `enabled` goes false.
    # `cron` is then derived from it and is shown only for runner compatibility.
    run_once_at: dt.datetime | None = None
    timezone: str
    enabled: bool
    routing: str
    grace_minutes: int
    always_run: bool = False
    notify: list[str]
    # The agent CLI its turns run on; "" = the agent's default.
    cli: str = ""
    last_slot: dt.datetime | None = None
    # The anchor the runner MUST pass as due_slot(after=...). Server-computed as
    # `last_slot or created_at` so the runner cannot get the fallback wrong.
    # Without it a fresh schedule (last_slot=None) fires once for the slot BEFORE
    # it existed — a schedule created Wednesday would immediately owe last
    # Friday's report. See the runner-side section.
    fire_after: dt.datetime
    next_runs: list[dt.datetime] = []
    last_status: str = ""
    created_by_email: str | None = None  # who set it up (null for pre-attribution rows)
    created_at: dt.datetime
    updated_at: dt.datetime


# ---- triggers: every kind (docs/superpowers/specs/2026-10-27-agent-triggers-design.md)


class TriggerIn(Schema):
    """Create a trigger. `cron` (default): exactly one of `cron` / `run_once_at`, and
    a prompt. `inbound_mail`: `sender_address` (+ optional `subject_pattern`); its
    prompt is optional and, when set, leads the mail turn's own prompt."""

    kind: Literal["cron", "inbound_mail"] = "cron"
    name: str
    prompt: str = ""
    cron: str | None = None
    run_once_at: dt.datetime | None = None
    timezone: str = "UTC"
    sender_address: str = ""
    subject_pattern: str = ""
    # "" = the agent's routing decides (bounded by you); manual | auto (agent admin).
    turn_mode: Literal["", "manual", "auto"] = ""
    # inbound_mail: minutes a thread stays quiet after a turn on it before further
    # mail starts another (that turn carries every message since). 0 = immediately.
    mail_quiet_minutes: int = Field(30, ge=0, le=1440)
    enabled: bool = True
    routing: str = "prefer_local"
    grace_minutes: int = 120
    always_run: bool = False
    notify: list[str] = ["inbox"]
    cli: cli_vocab.CliOrDefault = ""

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        return validate_cron(v) if v else None

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_timezone(v)

    @field_validator("name")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("must not be blank")
        return v.strip()

    @model_validator(mode="after")
    def _shape(self):
        if self.kind == "cron":
            if bool(self.cron) == bool(self.run_once_at):
                raise ValueError("a cron trigger needs exactly one of cron (recurring) or "
                                 "run_once_at (one-off)")
            if not self.prompt.strip():
                raise ValueError("a cron trigger needs a prompt")
        elif not self.sender_address.strip():
            raise ValueError("an inbound_mail trigger needs a sender_address")
        return self


class TriggerPatch(SchedulePatch):
    """Partial update of any trigger. Its kind is fixed once made: naming a
    different one is refused (422) rather than ignored."""

    kind: Literal["cron", "inbound_mail"] | None = None
    sender_address: str | None = None
    subject_pattern: str | None = None
    turn_mode: Literal["", "manual", "auto"] | None = None
    mail_quiet_minutes: int | None = Field(None, ge=0, le=1440)


class TriggerOut(ScheduleOut):
    kind: str
    # inbound_mail: the bound sender and the subject regex it must match.
    sender_address: str = ""
    subject_pattern: str = ""
    turn_mode: str = ""
    mail_quiet_minutes: int = 30
    # Why this trigger may not fire now (its creator lost the right to it), or null.
    stopped: str | None = None
    updated_by_email: str | None = None


class ScheduledFireOut(Schema):
    schedule: ScheduleOut
    workspace_slug: str | None = None
    fires: list[dt.datetime]


class ScheduleWeekOut(Schema):
    start: dt.datetime
    items: list[ScheduledFireOut]


class SchedulePreviewIn(Schema):
    """Preview a cron the user is still typing — no row exists yet."""

    cron: str
    timezone: str = "UTC"

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return validate_cron(v)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_timezone(v)


class SchedulePreviewOut(Schema):
    next_runs: list[dt.datetime]


class ScheduleAnnouncedOut(Schema):
    """A project check-in's schedule fired (apps/huddles): canopy made a run. With
    a channel it announced it there and queued no turn — the lead confirms there
    first; without one the run's turn is queued (`status` "running")."""

    announced: bool = True
    run_id: int | None = None
    status: str = ""


class ScheduleFireIn(Schema):
    """The runner's report that a slot came due. The server re-derives nothing —
    but the slot is only honored as an idempotency anchor, never as a claim of
    authority: tenant scoping gates the route."""

    slot: dt.datetime


# ---------------------------------------------------------------------------
# Runner streams — the poll-fallback sync + live-event fan-out (SP3 Task 4)
# ---------------------------------------------------------------------------
