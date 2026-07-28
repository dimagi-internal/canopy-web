"""Pydantic schemas for the /api/workspaces surface."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import EmailStr, Field

from apps.common.schemas import StrictModel

from .models import SLUG_PATTERN

Role = Literal["owner", "editor", "viewer"]


class WorkspaceCreateIn(StrictModel):
    # Charset, not just length: the slug is an addressing token that ends up
    # inside the presence page key `<app>:<workspace>:<resource>` (parsed with
    # a bounded `split(":", 2)` over a resource segment that legitimately
    # contains colons). A colon-bearing slug is therefore irreducibly
    # ambiguous — `canopy:acme:eu:activity` reads as workspace `acme:eu` or as
    # workspace `acme` + resource `eu:activity` — which is a cross-tenant
    # roster leak the presence layer cannot undo. Single source of truth for
    # the pattern is `Workspace.SLUG_PATTERN`, which also validates the model
    # save path (a shell or management command never sees this schema).
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    # Deliberately no `auto_join_domains` here: it is never client input.
    # `auto_join_domains` grants standing DOMAIN-WIDE (every user of that
    # domain auto-joins as editor via `auto_join_workspaces`), so letting a
    # caller set it on their own workspace would let an attacker declare an
    # arbitrary allowlisted domain (e.g. "dimagi.com") and silently recruit
    # every teammate of that domain into their workspace. Only
    # `ensure_default_workspace()` may set it, straight from
    # `AUTH_ALLOWED_EMAIL_DOMAIN` server-side. `StrictModel`'s `extra="forbid"`
    # means a request that still sends this field is rejected (422), not
    # silently ignored — see the F1 security finding on the invite-aware
    # login gate.


class WorkspaceOut(StrictModel):
    slug: str
    display_name: str
    auto_join_domains: list[str]
    role: str  # the requesting user's role in this workspace
    created_at: dt.datetime


class MemberOut(StrictModel):
    user_id: int
    email: str
    role: str
    joined_at: dt.datetime


class MemberRoleUpdateIn(StrictModel):
    role: Role


class InviteCreateIn(StrictModel):
    # EmailStr rejects a non-email string at the schema boundary, before it
    # ever becomes a matchable admission key for `pending_invite_for_email` /
    # `email_admitted_outside_domain` — an owner shouldn't be able to store
    # e.g. a garbage or wildcard-shaped value there.
    email: EmailStr = Field(max_length=200)
    role: Role = "editor"


class InviteOut(StrictModel):
    id: int
    email: str
    role: str
    token: str
    expires_at: dt.datetime
    accepted_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None


InviteStatus = Literal["pending", "expired", "revoked", "accepted"]


class InvitePreviewOut(StrictModel):
    """Pre-auth invite preview — deliberately minimal disclosure. For any
    non-pending status, workspace_slug/workspace_display_name/role stay None:
    someone holding a dead token (forwarded, pasted into Slack, ...) learns
    only that it's dead, never which tenant it pointed at."""

    status: InviteStatus
    email_hint: str
    workspace_slug: str | None = None
    workspace_display_name: str | None = None
    role: str | None = None
