# apps/mail/schemas.py
from __future__ import annotations

from ninja import Schema


class DnsRecordOut(Schema):
    purpose: str
    type: str
    name: str
    value: str
    priority: int | None = None
    ok: bool | None = None
    found: list[str] = []


class MailRouteOut(Schema):
    id: int
    local_part: str
    address: str
    kind: str
    agent_slug: str = ""
    forward_to_user_id: int | None = None
    forward_to_email: str = ""
    enabled: bool


class MailDomainShareOut(Schema):
    workspace: str
    daily_send_cap: int


class MailDomainOut(Schema):
    id: int
    name: str
    status: str
    receiving: bool
    mx_provider: str
    suspended_reason: str
    verification_error: str = ""
    """Why SES stopped verifying the domain (its DKIM or MAIL FROM check FAILED after 72 h);
    "" while verifying or verified. The status stays `pending` meanwhile."""
    verification_restarted_at: str | None = None
    restart_note: str = ""
    """On a Re-check's answer only: whether it restarted SES verification, and why not."""
    daily_send_cap: int
    records: list[DnsRecordOut]
    routes: list[MailRouteOut]
    can_manage: bool
    owned: bool = True
    owner_workspace: str = ""
    can_share: bool = False
    shares: list[MailDomainShareOut] = []
    sign_in: bool = False
    """People with a verified Google account at this domain may sign in and ask to join
    the workspace (Google Workspace logins on a domain whose mail canopy hosts)."""
    can_set_sign_in: bool = False
    mx_points_at_google: bool = False
    """Its MX goes to Google: mail for it would reach Google, not canopy — a warning."""


class MailDomainListOut(Schema):
    enabled: bool
    can_manage: bool = False
    items: list[MailDomainOut]


class MailDomainIn(Schema):
    name: str


class MailDomainPatchIn(Schema):
    daily_send_cap: int | None = None
    unsuspend: bool = False
    sign_in: bool | None = None
    """Owner only, verified domains only: let Google accounts @this domain sign in and
    ask to join (they are approved at the workspace's auto-approve role, if any)."""


class MailDomainShareIn(Schema):
    workspace: str
    daily_send_cap: int | None = None


class MailRouteIn(Schema):
    local_part: str
    kind: str
    agent_slug: str = ""
    forward_to_user_id: int | None = None
    enabled: bool = True


class MailAttachmentOut(Schema):
    filename: str
    content_type: str
    size: int
    url: str


class MailMessageOut(Schema):
    id: int
    direction: str
    message_id: str
    from_addr: str
    from_name: str
    to: list[str]
    cc: list[str]
    reply_to: list[str] = []
    subject: str
    text: str
    html: str
    delivery_status: str
    verdicts: dict[str, str] = {}
    created_at: str
    attachments: list[MailAttachmentOut] = []


class MailThreadOut(Schema):
    thread_id: str
    address: str
    agent: str
    subject: str
    message_count: int
    unread: bool
    archived: bool
    last_message_at: str
    counterpart: str = ""
    default_reply_to: list[str] = []
    messages: list[MailMessageOut] = []


class MailThreadListOut(Schema):
    agent_address: str = ""
    items: list[MailThreadOut]


class MailReplyIn(Schema):
    text: str
    html: str = ""
    to: list[str] = []
    cc: list[str] = []


class MailSendIn(Schema):
    agent: str
    to: list[str]
    cc: list[str] = []
    subject: str
    text: str
    html: str = ""
    from_address: str | None = None
    """Which of the agent's addresses to send from (one of its live routes here). Omitted:
    the route for the agent's own address, else its first."""


class MailThreadStateIn(Schema):
    unread: bool | None = None
    archived: bool | None = None


class MailResolveOut(Schema):
    backend: str
    workspace: str
    agent: str
    address: str = ""


class AgentAddressOut(Schema):
    address: str
    backend: str
    """"ses" (a canopy-hosted route) or "gmail" (a Google mailbox)."""
    primary: bool = False
    """The agent's own address (`Agent.email`)."""


class AgentMailboxOut(Schema):
    """Where an agent's address lives. `kind`: hosted (canopy-hosted, nothing to sign in),
    google (signed in with Google), none. For hosted: `live` = mail to it is the agent's turn
    now; `route` = mine | taken | none; `verified` = the domain's DNS checks out."""

    kind: str
    address: str = ""
    domain: str = ""
    verified: bool = False
    live: bool = False
    route: str = ""
    addresses: list[AgentAddressOut] = []
    """Every address the agent has: its own (primary), its canopy-hosted routes and its
    Gmail push mailboxes, lowercase, each once."""
    hosted_domains: list[str] = []
    """The canopy-hosted domains this agent's workspace may give it an address on."""
    can_route: bool = False
    """Whether the caller may create the mailbox route (workspace admin or owner)."""


class AgentMailboxIn(Schema):
    address: str
