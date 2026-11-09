"""Session resolve and report schemas."""
from __future__ import annotations

import datetime as dt
import uuid

from ninja import Schema
from pydantic import Field, model_validator


class ResolveSessionIn(Schema):
    agent_slug: str = ""
    project: str = ""  # set instead of agent_slug for a repo session
    workspace: str = ""  # required with project: the turn's tenant (gates the owner)
    thread_key: str
    # The agent/project turn asking. With it, a session that is mid-turn is never
    # proposed for reuse. Optional so a runner predating it still works.
    turn_id: uuid.UUID | None = None


#: The retired spelling of `session_key`, still accepted on input.
#:
#: The field was renamed when the cloud runner started putting Claude session ids
#: in it (harness migration 0057). Runners update on deploy, but an agent's
#: `canopy agent turn` client and a runner mid-update still send the old name —
#: so each input takes either, and the new name wins when both arrive. Remove the
#: field and `adopt_legacy_session_key` once nothing in the fleet sends it.
LEGACY_SESSION_KEY = Field(
    default="", max_length=200, json_schema_extra={"deprecated": True},
    description="Deprecated: send `session_key`.",
)


def adopt_legacy_session_key(model):
    """An after-validator body: fold `emdash_task_id` into an empty `session_key`.
    After, not before, so it works the same on ninja `Schema` (which wraps the raw
    input before a before-validator sees it) and on plain pydantic models."""
    if model.emdash_task_id and not model.session_key:
        model.session_key = model.emdash_task_id
    return model


class ForkPlanOut(Schema):
    # The source's Claude session, and how many of its transcript records (as
    # canopy_transcript.read_records counts them) the fork starts from.
    session_key: str
    records: int


class ResolveSessionOut(Schema):
    reuse: bool
    new_thread: bool
    session_key: str
    # Deprecated duplicate of `session_key`, for runners that predate the rename.
    emdash_task_id: str
    agent_task_ext_id: str
    summary: str
    link_id: str | None
    # Why reuse was refused although this runner owns the session: it is mid-turn
    # (another turn executing in it, or the engine reports it working). "" otherwise.
    busy: str = ""
    # A forked chat with no Claude session yet: start it from a cut of another.
    fork: ForkPlanOut | None = None
    # For every turn of a forked chat: the chat whose directory a cloud runner runs
    # it in (the source's, so they share a worktree). "" otherwise.
    worktree_of: str = ""
    # The fork and the chats it was cut from, nearest first, from canopy's own
    # `parent_session` column. A runner runs in `worktree_of` only if it is here.
    fork_lineage: list[str] = []


class RecordSessionIn(Schema):
    agent_slug: str = ""
    project: str = ""  # set instead of agent_slug for a repo session
    workspace: str = ""  # required with project: the turn's tenant (gates the owner)
    thread_key: str
    session_key: str = ""
    session_id: str = ""
    agent_task_ext_id: str | None = None
    summary: str | None = None
    # A readable name for the session, used where the session would otherwise be
    # named after its key. A laptop's key is an emdash task name a person can
    # read; a cloud runner's is a Claude session UUID, so it sends one of these.
    title: str = Field(default="", max_length=200)
    # The turn this session is running, when the runner is recording it mid-turn.
    # Stamps that turn's session key NOW rather than at finish, so the agent's
    # close-out — which it posts before the turn ends — can find its turn.
    turn_id: uuid.UUID | None = None
    emdash_task_id: str = LEGACY_SESSION_KEY

    _legacy_key = model_validator(mode="after")(adopt_legacy_session_key)


class ReportedSessionIn(Schema):
    emdash_task: str  # the emdash task NAME
    # emdash's `tasks.id` — the task's IDENTITY, which the name is not (names are
    # reused over time). A change behind the same name forks a new session record
    # (canopy_sessions.transcript_ingest.fork_if_task_changed). "" = an old runner or a
    # session with no emdash task, which makes no claim.
    task_uid: str = ""
    project: str = ""
    status: str = ""
    # Emdash's own per-conversation liveness flag: "working" | "awaiting-input" | "",
    # or the runner's "interrupted" where emdash says working over a turn Escape ended.
    # Defaulted because "" is a real and common answer — a runner that predates this,
    # a cloud runner with no emdash, or an emdash whose schema drifted all send
    # nothing, and the server falls back to its activity-recency heuristic for them
    # (services.is_session_running). Only a runner that actually knows overrides it.
    agent_status: str = ""
    # The runner's dissent from its OWN `agent_status` above: emdash says this session
    # is not working, but it is still writing to its transcript (or to a subagent's).
    # Set only after the runner has watched writes land AFTER the flag went non-working
    # — see canopy_runner.sessions.annotate_engine_staleness for why that, and not
    # recency, is the discriminator. Defaulted False so an older runner, which cannot
    # dissent, keeps the pre-existing "trust the flag outright" behaviour exactly.
    agent_status_stale: bool = False
    last_interacted_at: dt.datetime | None = None
    recent_messages: list = []  # Phase B populates this; ignored/empty in Phase A
    # The dialog this session is blocked on, read from its transcript, or None
    # for "I looked and there is none". `None` is a REAL answer and is written
    # through: it is what retires a menu once the human answers at the laptop.
    #
    # Defaulted so a runner that predates this keeps working — but note what its
    # default MEANS. An old runner sends nothing, which lands as None and clears
    # the field, i.e. "no dialog". That is the safe direction: the failure is a
    # phone with no buttons (the terminal still answers), never a phone offering
    # buttons against a dialog that is gone.
    question: dict | None = None


class ReportSessionsIn(Schema):
    sessions: list[ReportedSessionIn] = []
    # emdash task names this runner has seen ARCHIVED. Defaulted so an older runner
    # (which does not send it) keeps working unchanged — it simply never closes a row.
    archived: list[str] = []
    # True when `sessions` is this runner's WHOLE open set (it was not cut off by
    # the runner's report limit). Only then is absence an observation: a session
    # missing from consecutive complete reports was closed. Defaulted False so an
    # older runner never has anything retired on absence.
    complete: bool = False


class EmdashSessionOut(Schema):
    id: uuid.UUID
    emdash_task: str
    #: `SessionView.project` carries `emdash_project`, so this stays meaningful
    #: for an agent-owned row whose stored column the XOR necessarily cleared.
    project: str
    #: Whose work this is. Without it, every consumer doing
    #: `.get("agent")` would read `agent: null` — indistinguishable from "known to
    #: belong to nobody" — leaving `project` as the only thing to guess from.
    agent: str | None
    status: str
    last_interacted_at: dt.datetime | None
    recent_messages: list
    workspace: str
    runner_name: str

    @staticmethod
    def resolve_workspace(obj) -> str:
        return obj.workspace_id

    @staticmethod
    def resolve_runner_name(obj) -> str:
        return obj.runner_name


class SessionReportOut(Schema):
    """Result of a runner's wholesale session report (POST /runners/{id}/sessions).

    Named distinctly from apps.agents.schemas.CountOut ({created, replaced, count}) —
    Django Ninja keys OpenAPI components by class title, so two Pydantic models both
    named CountOut collapse into one component and one silently wins, dropping fields
    from the other's advertised schema."""

    count: int
    # Reported tasks that were not filed: their project names an agent outside
    # this runner's tenant (`services.session_home`).
    skipped: int = 0
