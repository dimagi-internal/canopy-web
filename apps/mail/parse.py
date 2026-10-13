"""Raw MIME → the fields canopy stores. stdlib only."""
from __future__ import annotations

import hashlib
import html as htmlmod
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Parsed:
    message_id: str
    in_reply_to: str
    references: list[str]
    from_addr: str
    from_name: str
    to: list[str]
    cc: list[str]
    subject: str
    text: str
    html: str
    attachments: list[Attachment] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    automated: bool = False


_IDS = re.compile(r"<[^<>\s]+>")
_TAGS = re.compile(r"<[^>]+>")


def _addrs(values) -> list[str]:
    return [a.lower() for _, a in getaddresses([str(v) for v in values or []]) if a]


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>", "\n", h)
    return re.sub(r"[ \t]+", " ", htmlmod.unescape(_TAGS.sub("", h))).strip()


def _is_automated(msg) -> bool:
    auto = str(msg.get("Auto-Submitted", "")).strip().lower()
    prec = str(msg.get("Precedence", "")).strip().lower()
    return bool((auto and auto != "no") or prec in ("bulk", "list", "junk")
                or msg.get("List-Unsubscribe"))


def parse(raw: bytes) -> Parsed:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    name, addr = parseaddr(str(msg.get("From", "")))
    message_id = str(msg.get("Message-ID", "")).strip()
    if not _IDS.fullmatch(message_id or ""):
        message_id = f"<{hashlib.sha256(raw).hexdigest()[:32]}@canopy>"
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    html = html_part.get_content() if html_part is not None else ""
    text = text_part.get_content() if text_part is not None else _html_to_text(html)
    attachments = [
        Attachment(part.get_filename() or "attachment", part.get_content_type(),
                   part.get_payload(decode=True) or b"")
        for part in msg.iter_attachments()
    ]
    return Parsed(
        message_id=message_id,
        in_reply_to=" ".join(_IDS.findall(str(msg.get("In-Reply-To", "")))),
        references=_IDS.findall(str(msg.get("References", ""))),
        from_addr=addr.lower(), from_name=name,
        to=_addrs(msg.get_all("To")), cc=_addrs(msg.get_all("Cc")),
        subject=str(msg.get("Subject", "")), text=text, html=html,
        attachments=attachments,
        headers=[(k, str(v)) for k, v in msg.items()],
        automated=_is_automated(msg),
    )
