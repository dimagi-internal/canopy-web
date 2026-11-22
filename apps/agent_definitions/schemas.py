from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel


class StoredAgentIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=80)
    #: What the agent is for, in a sentence or two: its persona and README start from it.
    mandate: str = Field(min_length=1, max_length=2000)
    stakeholders: str = Field(default="", max_length=2000)
    workspace: str = Field(min_length=1, max_length=80)


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


class DefinitionChangeIn(StrictModel):
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
    published_by: str | None = None
    #: The approval request an agent's own draft filed, while it waits.
    approval_id: int | None = None


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
