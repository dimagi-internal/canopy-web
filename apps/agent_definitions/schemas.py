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
