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


class MailDomainOut(Schema):
    id: int
    name: str
    status: str
    receiving: bool
    mx_provider: str
    suspended_reason: str
    daily_send_cap: int
    records: list[DnsRecordOut]
    routes: list[MailRouteOut]
    can_manage: bool


class MailDomainListOut(Schema):
    enabled: bool
    items: list[MailDomainOut]


class MailDomainIn(Schema):
    name: str


class MailDomainPatchIn(Schema):
    daily_send_cap: int | None = None
    unsuspend: bool = False


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
    subject: str
    text: str
    html: str
    delivery_status: str
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
    messages: list[MailMessageOut] = []


class MailThreadListOut(Schema):
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


class MailThreadStateIn(Schema):
    unread: bool | None = None
    archived: bool | None = None


class MailResolveOut(Schema):
    backend: str
    workspace: str
    agent: str
