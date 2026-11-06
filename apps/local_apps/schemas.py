from __future__ import annotations

import uuid
from typing import Any

from ninja import Schema


class LocalAppComputerOut(Schema):
    id: str
    name: str


class LocalAppVersionOut(Schema):
    version: str
    released_at: str | None = None
    changelog: str = ""
    revoked: bool = False


class LocalAppListingOut(Schema):
    key: str
    name: str
    description: str
    version: str
    tagline: str = ""
    installed: bool = False


class LocalAppStoreOut(Schema):
    apps: list[LocalAppListingOut]


class LocalAppDetailOut(Schema):
    key: str
    name: str
    description: str
    version: str
    publisher: str
    #: False when it was published with no approval. The page must not claim a review then.
    reviewed: bool = False
    notice: str
    disclosure: dict[str, list[str]]
    settings: dict[str, Any]
    #: What the person accepts by installing, send it back with the install.
    digest: str
    #: Agents the person may use, across their workspaces, for a setting that picks one.
    agents: list[dict[str, str]] = []
    computers: list[LocalAppComputerOut] = []
    versions: list[LocalAppVersionOut] = []


class LocalAppInstallIn(Schema):
    runner_id: uuid.UUID
    settings: dict[str, Any] = {}
    accept_digest: str


class LocalAppRunOut(Schema):
    id: str
    version: str
    started_at: str
    finished_at: str | None = None
    state: str = ""
    counts: dict[str, Any] = {}
    stop_reason: str = ""
    stop_requested: bool = False
    running: bool = False


class LocalAppInstallationOut(Schema):
    id: int
    key: str
    name: str
    #: Where its sessions land: the chosen agent's workspace.
    workspace: str | None = None
    computer: LocalAppComputerOut
    settings: dict[str, Any]
    settings_spec: dict[str, Any]
    agents: list[dict[str, str]] = []
    version: str | None = None
    needs_acceptance: bool = False
    changes: list[str] = []
    digest: str = ""
    refusal: str | None = None
    last_run: LocalAppRunOut | None = None


class LocalAppConfigureIn(Schema):
    settings: dict[str, Any]


class LocalAppAcceptIn(Schema):
    digest: str


class LocalAppSubmitIn(Schema):
    package_b64: str
    note: str = ""
    #: The workspace it's submitted from; else your pinned or only workspace.
    workspace: str | None = None


class LocalAppSubmissionOut(Schema):
    id: int
    key: str
    version: str
    sha256: str
    status: str
    approval_id: int | None = None
    review_url: str


class LocalAppReviewOut(LocalAppSubmissionOut):
    name: str
    size: int
    note: str = ""
    submitted_by: str = ""
    submitted_at: str
    manifest: dict[str, Any]
    facts: dict[str, Any]
    prior_version: str | None = None
    trust_changes: list[str] = []
    diff: list[dict[str, Any]] = []
    revoked_reason: str = ""
    can_revoke: bool = False


class LocalAppRevokeIn(Schema):
    reason: str


class RunnerLocalAppOut(Schema):
    key: str
    name: str
    version: str | None = None
    sha256: str | None = None
    size: int | None = None
    notice: str = ""
    disclosure: dict[str, list[str]] = {}
    entry: list[str] = []
    max_minutes: int = 0
    settings: dict[str, Any] = {}
    needs_acceptance: bool = False
    changes: list[str] = []
    refusal: str | None = None
    running: bool = False
    #: The agent it sends to, by name, so Start can say so.
    agent: str | None = None
    checks: list[dict[str, Any]] = []


class RunnerLocalAppsOut(Schema):
    apps: list[RunnerLocalAppOut]


class LocalAppStartRunIn(Schema):
    version: str


class LocalAppStartRunOut(Schema):
    run_id: str


class LocalAppBeatIn(Schema):
    state: str = ""
    counts: dict[str, Any] = {}


class LocalAppBeatOut(Schema):
    stop: bool
    reason: str = ""


class LocalAppFinishIn(Schema):
    reason: str = ""
    counts: dict[str, Any] = {}


class LocalAppCanopyCallIn(Schema):
    op: str
    args: dict[str, Any] = {}


class LocalAppCanopyCallOut(Schema):
    status: int
    body: dict[str, Any]
