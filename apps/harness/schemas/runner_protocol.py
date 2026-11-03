"""Runner protocol schemas: drills, menus, closes and handoffs."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from ninja import Schema
from pydantic import Field


class RunnerDrillOut(Schema):
    id: int
    agent_slug: str
    outcome: str
    summary: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    turn_id: uuid.UUID | None

    @staticmethod
    def resolve_agent_slug(obj):
        return obj.agent.slug


class DrillIn(Schema):
    agents: list[str] | None = None


class DrillReportIn(Schema):
    outcome: Literal["pass", "fail"]
    summary: str = ""


class MenuAnswerOut(Schema):
    session_id: str
    session_key: str
    #: The emdash project owning `session_key` — task names are unique per
    #: project only, so the runner aims the keystroke by (project, task).
    project: str = ""
    answer_id: str
    option: int | None = None
    #: One list of chosen option numbers per declared question. A runner that
    #: predates this ignores the field and presses `option`, which is what it
    #: does today — so the poll tick never gets WORSE than the current
    #: behaviour on an ask this shape cannot express.
    selections: list[list[int]] | None = None
    texts: list[str | None] | None = None


class MenuAnswerSyncOut(Schema):
    answers: list[MenuAnswerOut] = []


class MenuAnswerResultOut(Schema):
    ok: bool


class AdmissionNoticeClaimIn(Schema):
    #: {agent slug: the mailbox address this runner reads for it}.
    mailboxes: dict[str, str] = {}


class AdmissionNoticeOut(Schema):
    id: int
    agent: str
    agent_name: str
    #: The address the reply must come from ("" = the agent's mailbox, as read).
    mailbox: str = ""
    thread_id: str
    #: Gmail's id of the message to answer ("" = the thread's newest).
    message_id: str = ""
    to: str
    subject: str = ""
    body: str


class AdmissionNoticeClaimOut(Schema):
    notices: list[AdmissionNoticeOut] = []


class AdmissionNoticeReportIn(Schema):
    outcome: Literal["sent", "skipped", "failed"]
    detail: str = ""


class MenuAnswerResultIn(Schema):
    session_id: uuid.UUID
    answer_id: str
    # What the runner did with it. Free-form on purpose: the vocabulary lives in
    # `canopy_runner.hooks` (answered / no_dialog / wrong_pane / …) and the server
    # only needs to know the answer is retired, not to re-litigate the outcome.
    outcome: str = ""


class SessionMenuIn(Schema):
    session_id: uuid.UUID
    # The canopy menu dict (`canopy_acp.menus`), or null to clear it. The same
    # shape a laptop puts in a reported session's `question`.
    menu: dict | None = None


class SessionMenuOut(Schema):
    ok: bool


class SessionSubagentsIn(Schema):
    session_id: uuid.UUID
    # One dict per subagent (`canopy_transcript.summarize_subagents`), the whole
    # set each time; [] when it has none.
    subagents: list[dict] = []


class SessionInterruptOut(Schema):
    session_id: str
    session_key: str
    #: The emdash project owning `session_key` (task names are unique per project).
    project: str = ""
    interrupt_id: str


class SessionInterruptSyncOut(Schema):
    interrupts: list[SessionInterruptOut] = []


class SessionInterruptResultIn(Schema):
    session_id: uuid.UUID
    interrupt_id: str
    #: What the runner did with it (interrupted / idle / failed / no_session …);
    #: free-form, the server only retires the request.
    outcome: str = ""


class CloseOut(Schema):
    session_id: str
    session_key: str
    #: The emdash project owning `session_key`. The runner REFUSES a delete it
    #: cannot place in exactly one project rather than delete by name alone.
    project: str = ""
    #: The failure the server already holds for this close ("" for none), so a
    #: runner retrying every tick reports only when its reason changes.
    close_error: str = ""


class CloseSyncOut(Schema):
    closes: list[CloseOut] = []


class KeptConversationsIn(Schema):
    #: The chat sessions whose Claude session this sensitive-data box keeps on disk.
    session_ids: list[str] = Field(default=[], max_length=500)


class KeptConversationsOut(Schema):
    #: Those of them still open, in which this runner has run a confined turn. The
    #: runner forgets every other (closed, deleted, or never its own).
    open: list[str] = []


class CloseResultIn(Schema):
    session_key: str
    #: The emdash project the task was looked for in; blank when the runner
    #: could not tell (a task with no project is never deleted by name alone).
    project: str = ""
    ok: bool
    #: Why it failed, in a short sentence a person can act on. Ignored when ok.
    error: str = ""


class CloseResultsIn(Schema):
    results: list[CloseResultIn] = []


class CloseResultsOut(Schema):
    #: Bindings whose failed-close record was written or cleared.
    updated: int


class HandoffDueOut(Schema):
    """One thing a runner owes as the SOURCE of a transfer (`canopy_sessions/handoff.py`)."""
    id: str
    session_id: str
    #: The task (emdash) or Claude session (cloud) whose worktree holds the work.
    session_key: str
    project: str = ""
    #: Pack the Claude transcript too (the target resumes it).
    transcript: bool = False
    #: Pack the worktree and upload it.
    capture: bool = False
    #: Close the task: the target restored the work, or the snapshot is held here.
    close: bool = False
    close_error: str = ""


class HandoffSyncOut(Schema):
    handoffs: list[HandoffDueOut] = []


class HandoffPartIn(Schema):
    part: int
    parts: int
    #: Base64 of this chunk of the tar.gz.
    data: str = Field(max_length=2_200_000)


class HandoffPartOut(Schema):
    received: int


class HandoffCapturedIn(Schema):
    ok: bool
    #: What was packed (`runner/canopy_runner/canopy_runner/handoff.py::capture`).
    manifest: dict | None = None
    error: str = Field(default="", max_length=1000)


class HandoffCloseIn(Schema):
    ok: bool
    error: str = Field(default="", max_length=1000)


class HandoffRestoredIn(Schema):
    #: `handoff.restore`'s report: ok, restored, branch, head, counts, mismatch, notes, error.
    report: dict


class HandoffAckOut(Schema):
    capture_status: str
    restore_status: str
    close_status: str
