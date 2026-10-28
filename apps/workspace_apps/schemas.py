from __future__ import annotations

from datetime import datetime
from typing import Any

from ninja import Schema


class StoreAppOut(Schema):
    key: str
    name: str
    description: str
    version: str
    publisher: str
    changelog: str
    tagline: str = ""
    #: The first screenshot's file, for the card (`/workspace-app-store/{ws}/screenshot`).
    cover: str = ""


class StoreHighlightOut(Schema):
    title: str
    body: str


class StoreScreenshotOut(Schema):
    file: str
    caption: str


class StoreListingOut(Schema):
    tagline: str = ""
    overview: str = ""
    audience: str = ""
    data: str = ""
    highlights: list[StoreHighlightOut] = []
    screenshots: list[StoreScreenshotOut] = []


class StoreRoleOut(Schema):
    name: str
    description: str
    holders: str


class StorePageOut(Schema):
    title: str
    audience: str


class StoreRecordOut(Schema):
    label: str
    fields: int
    restricted: list[str]
    scoped: bool


class StoreVersionOut(Schema):
    version: str
    released_at: str
    changelog: str


class StoreEmailOut(Schema):
    name: str
    to: str
    optional: bool
    delayable: bool


class StoreAppDetailOut(Schema):
    key: str
    name: str
    description: str
    version: str
    publisher: str
    listing: StoreListingOut
    roles: list[StoreRoleOut]
    pages: list[StorePageOut]
    records: list[StoreRecordOut]
    settings: int
    retention: list[str]
    public_pages: list[str]
    emails: list[StoreEmailOut]
    versions: list[StoreVersionOut]
    installed: bool


class InstallationSummaryOut(Schema):
    key: str
    name: str
    description: str
    workspace: str
    template: str
    template_version: str
    live_version: int
    enabled: bool
    public: bool
    has_public_pages: bool
    upgrade_available: str | None = None
    has_draft: bool
    installed_at: datetime
    updated_at: datetime


class PageOut(Schema):
    slug: str
    title: str
    audience: str
    role: str
    nav: bool
    code: str
    sources: list[dict[str, Any]]
    slots: list[str] = []
    fills: dict[str, list[dict[str, Any]]] = {}
    forked: bool = False
    origin: str = "template"


class DraftInfoOut(Schema):
    id: int
    number: int
    origin: str
    template_version: str
    space: str


class InstallationOut(InstallationSummaryOut):
    my_roles: list[str]
    may_maintain: bool
    may_manage: bool
    pages: list[PageOut]
    #: The collections' fields (labels, types, options) and the settings' values —
    #: what a page needs to draw fields it did not know about (an added question).
    data_model: dict[str, Any]
    settings: dict[str, Any]
    #: Set when this response describes a draft's test copy (`?draft=`).
    draft: DraftInfoOut | None = None
    #: The whole composed manifest, for someone who maintains the app; None otherwise.
    manifest: dict[str, Any] | None = None


class PublicAppOut(Schema):
    key: str
    name: str
    workspace: str
    workspace_name: str
    pages: list[PageOut]
    data_model: dict[str, Any]
    settings: dict[str, Any]


class InstallIn(Schema):
    template: str = ""
    manifest: dict[str, Any] | None = None


class InstallationPatch(Schema):
    enabled: bool | None = None
    public: bool | None = None


class ActionIn(Schema):
    #: The record an update or delete acts on.
    record: int | None = None
    input: dict[str, Any] = {}
    #: Values the action uses but never stores (the wording of one email).
    params: dict[str, Any] = {}


class BulkActionIn(Schema):
    records: list[int]
    input: dict[str, Any] = {}
    params: dict[str, Any] = {}


class LinkOut(Schema):
    url: str
    token: str
    days: int


class LinkPageOut(Schema):
    key: str
    name: str
    workspace: str
    workspace_name: str
    link: str
    label: str
    record: dict[str, Any]
    page: PageOut
    actions: dict[str, Any]
    data_model: dict[str, Any]
    settings: dict[str, Any]
    draft: bool


class HelperOut(Schema):
    """An agent the caller could ask to work on the app's draft."""
    slug: str
    name: str
    #: The workspace the agent lives in — where the chat with it is held.
    home: str
    login: str
    #: Its login is a member of the app's workspace, so it can reach the app.
    reachable: bool
    #: Already working on the open draft.
    helping: bool


class HelperIn(Schema):
    agent: str


class SetupStepOut(Schema):
    id: str
    title: str
    body: str
    go: str | None = None
    manual: bool
    done: bool


class SetupOut(Schema):
    steps: list[SetupStepOut]
    hidden: bool


class SetupIn(Schema):
    #: Tick (or untick) a step done by hand.
    step: str | None = None
    done: bool = True
    #: Put the whole list away, or bring it back.
    hidden: bool | None = None


class GrantIn(Schema):
    email: str
    role: str


class GrantOut(Schema):
    id: int
    email: str
    role: str
    granted_at: datetime


class VersionOut(Schema):
    number: int
    status: str
    origin: str
    template_version: str
    note: str
    changes: int
    created_by: str | None = None
    created_at: datetime
    published_by: str | None = None
    published_at: datetime | None = None


class DraftIn(Schema):
    #: Move the draft onto the newest template version (an update).
    upgrade: bool = False
    note: str = ""


class DraftOut(Schema):
    id: int
    number: int
    origin: str
    template_version: str
    note: str
    space: str
    created_by: str | None = None
    created_at: datetime
    #: `services.review`: changes, risks, quarantine, template_change, forked_pages, checks.
    review: dict[str, Any]


class RollbackIn(Schema):
    number: int


class FileOut(Schema):
    id: int
    name: str
    size: int
    content_type: str


class GuideOut(Schema):
    contract: str
    manifest: dict[str, Any]
    draft: int | None = None
