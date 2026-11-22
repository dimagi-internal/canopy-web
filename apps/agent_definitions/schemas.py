from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import ConfigDict, Field

from apps.common.schemas import StrictModel


class _FileContentIn(StrictModel):
    # A file's text is its bytes: surrounding whitespace (a final newline, say) is content,
    # never trimmed the way a form field is.
    model_config = ConfigDict(str_strip_whitespace=False)


class DefinitionNewFileIn(_FileContentIn):
    path: str = Field(min_length=1, max_length=200)
    #: The file as text; or `content_b64`, its bytes as base64. Exactly one.
    content: str | None = None
    content_b64: str | None = None
    executable: bool = False


class StoredAgentIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80)
    #: What the agent is for, in a sentence or two: its persona and README start from it.
    mandate: str = Field(min_length=1, max_length=2000)
    stakeholders: str = Field(default="", max_length=2000)
    workspace: str = Field(min_length=1, max_length=80)
    #: Files laid over the factory's scaffold (one of these replaces the scaffold's at its
    #: path). The files canopy writes itself (`.claude-plugin/`, `.claude/settings.json`,
    #: `bin/<slug>-email`, `.gitignore`) are ignored if sent.
    files: list[DefinitionNewFileIn] = Field(default_factory=list, max_length=400)
    #: A `git bundle` of the source repository's branch (`git bundle create - main`), base64:
    #: its history is kept under `refs/heads/imported` (every blob secret-scanned) and
    #: revision 1 is a commit on its tip, as `convert` does.
    bundle_b64: str | None = None
    #: Leave out each file the file policy refuses (named in `dropped`) instead of refusing
    #: the create.
    drop_refused: bool = False
    #: Revision 1's message.
    message: str = Field(default="", max_length=4000)


class DefinitionFileIn(StrictModel):
    path: str = Field(min_length=1, max_length=200)
    #: The file's bytes, base64.
    content_b64: str
    executable: bool = False


class DefinitionConvertIn(StrictModel):
    files: list[DefinitionFileIn] = Field(max_length=400)
    message: str = Field(default="", max_length=4000)
    #: A `git bundle` of the repository's branch (`git bundle create - main`), base64: its
    #: history is kept under `refs/heads/imported` and revision 1 is a commit on its tip.
    bundle_b64: str | None = None


class DefinitionExportIn(StrictModel):
    #: The EMPTY GitHub repository to move to, `owner/name`.
    repo: str = Field(min_length=3, max_length=200)
    #: The branch `main` is pushed to there, and that the agent then runs.
    ref: str = Field(default="main", min_length=1, max_length=120)
    #: A GitHub token of the person exporting that may push to `repo`: used for this one
    #: push, never kept. Required: canopy-web cannot open the agent's delegated GitHub
    #: token, which is sealed to the machines that run it (without one, 422 `no_token`).
    github_token: str = Field(default="", max_length=400)


class DefinitionDetachIn(StrictModel):
    #: The GitHub repository the person pushed the definition to themselves.
    repo_url: str = Field(min_length=3, max_length=300)
    ref: str = Field(default="main", min_length=1, max_length=120)


class DefinitionMovedOut(StrictModel):
    #: `owner/name`, and the agent's new `repo_url` / `repo_ref`.
    repo: str
    repo_url: str
    repo_ref: str
    #: The commit (and revision) that was live, now the tip of `repo_ref` on GitHub.
    commit: str
    revision: int
    #: The `refs/tags/r<N>` pushed with it (empty for a detach).
    tags: list[str] = Field(default_factory=list)
    #: The agent that did it as the person, under an act-as-you permission; else null.
    acted_by: DefinitionActedByOut | None = None


class DefinitionChangeIn(_FileContentIn):
    path: str = Field(min_length=1, max_length=200)
    #: The new text, or null to delete the file.
    content: str | None = None
    #: Base64 bytes instead of `content`, for a file that is not easy to send as a JSON string.
    content_b64: str | None = None
    #: Omitted keeps the file's current bit (false for a new file).
    executable: bool | None = None


class DefinitionDraftIn(StrictModel):
    #: The live revision these changes are made on.
    base: int = Field(ge=1)
    changes: list[DefinitionChangeIn] = Field(min_length=1, max_length=200)
    message: str = Field(default="", max_length=4000)


class DefinitionRollbackIn(StrictModel):
    revision: int = Field(ge=1)
    message: str = Field(default="", max_length=4000)


class DefinitionAuthorOut(StrictModel):
    email: str = ""
    #: True when an agent (its own login, or a turn) wrote the revision.
    by_agent: bool = False
    turn_id: str | None = None


class DefinitionActedByOut(StrictModel):
    """An agent that did a person's act AS them, under an act-as-you permission the person
    approved with their passkey."""

    #: The agent's slug.
    agent: str
    #: The person it acted as (their email).
    person: str
    #: The act-as-you permission it held ("" once that record is gone).
    act_as_grant: str = ""


class DefinitionRevisionSummaryOut(StrictModel):
    number: int
    status: Literal["draft", "published", "superseded", "discarded"]
    root_sha256: str
    #: The revision's commit in the agent's repository (`DefinitionOut.git_url`).
    commit: str = ""
    message: str
    parent: int | None = None
    author: DefinitionAuthorOut
    files: int
    size: int
    created_at: dt.datetime
    published_at: dt.datetime | None = None
    #: The person who published it (their email), or "<slug> (agent)" when another agent that
    #: administers this one published it as itself (`published_by_agent`).
    published_by: str | None = None
    #: The admin agent that published this revision as itself, or null.
    published_by_agent: str | None = None
    #: The approval request an agent's own draft filed, while it waits.
    approval_id: int | None = None
    #: When an agent created or published this revision AS its person (`published_by`, or
    #: the author of a revision created live), the agent and its permission; else null.
    acted_by: DefinitionActedByOut | None = None


class DefinitionPolicyOut(StrictModel):
    max_files: int
    max_file_bytes: int
    max_tree_bytes: int
    editable: list[str]
    generated: list[str]


class DefinitionOut(StrictModel):
    source: Literal["repo", "stored"]
    #: Where to `git clone` the definition (with a canopy token), when canopy hosts its
    #: repository: a stored agent, or one exported to GitHub (then a read-only archive).
    git_url: str | None = None
    #: `owner/name` the definition moved to, once exported; nothing here can change then.
    exported_to: str = ""
    live: DefinitionRevisionSummaryOut | None = None
    draft: DefinitionRevisionSummaryOut | None = None
    policy: DefinitionPolicyOut
    #: May the caller draft, publish (and roll back), or discard here.
    can_draft: bool = False
    can_publish: bool = False


class DefinitionTreeEntryOut(StrictModel):
    sha256: str
    size: int
    executable: bool


class DefinitionRevisionOut(DefinitionRevisionSummaryOut):
    tree: dict[str, DefinitionTreeEntryOut]


class DefinitionFileDiffOut(StrictModel):
    path: str
    status: Literal["added", "deleted", "modified", "mode"]
    diff: str
    truncated: bool = False


class DefinitionDiffOut(StrictModel):
    revision: int
    against: int
    files: list[DefinitionFileDiffOut]


class StoredAgentOut(StrictModel):
    slug: str
    name: str
    workspace: str
    source: Literal["stored"] = "stored"
    revision: int
    root_sha256: str
    #: The agent's owner (their email).
    owner: str = ""
    #: The agent that created it as its owner, under an act-as-you permission; else null.
    acted_by: DefinitionActedByOut | None = None
    #: The files sent that the file policy refused and `drop_refused` left out.
    dropped: list[str] = Field(default_factory=list)
