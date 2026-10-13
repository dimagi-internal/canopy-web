"""Raw MIME → the fields canopy stores. stdlib only."""
from __future__ import annotations

import codecs
import hashlib
import html as htmlmod
import re
import secrets
from dataclasses import dataclass, field
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses


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
    reply_to: list[str] = field(default_factory=list)


_IDS = re.compile(r"<[^<>\s]+>")
_TAGS = re.compile(r"<[^>]+>")


_CRLF = re.compile(r"[\r\n\v\f\u2028\u2029\x85]+")


def _nonul(v: str) -> str:
    """Postgres text cannot hold NUL: one in a stored field loses the whole message."""
    return v.replace("\x00", "")


def _clean(v: str) -> str:
    # Lone surrogates (from raw 8-bit headers) would break later DB/JSON writes.
    v = _nonul(v.encode("utf-8", "replace").decode("utf-8"))
    return _CRLF.sub(" ", v).strip()


def _raw_all(msg, name: str) -> list[str]:
    """Raw (undecoded, unparsed) values of every header called `name`; never raises."""
    try:
        want = name.lower()
        return [str(v) for k, v in msg.raw_items() if k.lower() == want]
    except Exception:
        return []


def _raw(msg, name: str) -> str:
    vals = _raw_all(msg, name)
    return vals[0] if vals else ""


def _decode_words(v: str) -> str:
    try:
        v = str(make_header(decode_header(v)))
    except Exception:
        pass
    return _clean(v)


def _hdr(msg, name: str) -> str:
    """Decoded single-line header value; never raises, never contains CR/LF."""
    return _decode_words(_raw(msg, name))


_ENCWORD = re.compile(r"=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=")


def _addr_pairs(msg, name: str) -> list[tuple[str, str]]:
    values = [_clean(v) for v in _raw_all(msg, name)]
    try:
        return _addr_pairs_parked(values)
    except Exception:
        pass
    try:
        return [(_clean(n), _clean(a).lower()) for n, a in getaddresses(values) if _clean(a)]
    except Exception:
        return []


def _addr_pairs_parked(values: list[str]) -> list[tuple[str, str]]:
    # Encoded-words may decode to specials (":", ",", CR/LF); park them behind
    # inert, unpredictable tokens while parsing addresses, then restore and decode.
    nonce = secrets.token_hex(8)
    words: list[str] = []
    tok = re.compile(f"ENCWORD{nonce}N(\\d+)X")

    def park(m):
        words.append(m.group(0))
        return f"ENCWORD{nonce}N{len(words) - 1}X"

    def restore(v: str) -> str:
        def one(m):
            i = int(m.group(1))
            return words[i] if i < len(words) else m.group(0)
        return tok.sub(one, v)

    pairs = getaddresses([_ENCWORD.sub(park, v) for v in values])
    return [(_decode_words(restore(n)), _clean(a).lower()) for n, a in pairs if _clean(a)]


def _addrs(msg, name: str) -> list[str]:
    return [a for _, a in _addr_pairs(msg, name)]


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>", "\n", h)
    return re.sub(r"[ \t]+", " ", htmlmod.unescape(_TAGS.sub("", h))).strip()


def _content(part) -> str:
    """Decoded text of a part, NUL-free; tolerate unknown/bad charsets instead of raising."""
    try:
        return _nonul(part.get_content())
    except (LookupError, UnicodeError, ValueError, AssertionError):
        charset = part.get_content_charset() or "utf-8"
        try:
            codecs.lookup(charset)
        except (LookupError, ValueError):
            charset = "utf-8"
        return _nonul((part.get_payload(decode=True) or b"").decode(charset, errors="replace"))


def _safe_items(msg) -> list[tuple[str, str]]:
    try:
        return list(msg.raw_items())
    except Exception:
        return []


def _safe_filename(part) -> str:
    try:
        return _clean(part.get_filename() or "") or "attachment"
    except Exception:
        return "attachment"


def _is_automated(msg) -> bool:
    auto = _hdr(msg, "Auto-Submitted").lower()
    prec = _hdr(msg, "Precedence").lower()
    return bool((auto and auto != "no") or prec in ("bulk", "list", "junk")
                or _raw(msg, "List-Unsubscribe").strip())


def parse(raw: bytes) -> Parsed:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    frm = _addr_pairs(msg, "From")
    name, addr = frm[0] if frm else ("", "")
    message_id = _hdr(msg, "Message-ID")
    if not _IDS.fullmatch(message_id or ""):
        message_id = f"<{hashlib.sha256(raw).hexdigest()[:32]}@canopy>"
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    html = _content(html_part) if html_part is not None else ""
    text = _content(text_part) if text_part is not None else _html_to_text(html)
    attachments = [
        Attachment(_safe_filename(part), _clean(part.get_content_type()),
                   part.get_payload(decode=True) or b"")
        for part in msg.iter_attachments()
    ]
    return Parsed(
        message_id=message_id,
        in_reply_to=" ".join(_IDS.findall(_hdr(msg, "In-Reply-To"))),
        references=_IDS.findall(_hdr(msg, "References")),
        from_addr=addr, from_name=name,
        to=_addrs(msg, "To"), cc=_addrs(msg, "Cc"),
        subject=_hdr(msg, "Subject"), text=text, html=html,
        attachments=attachments,
        headers=[(_clean(str(k)), _clean(str(v))) for k, v in _safe_items(msg)],
        automated=_is_automated(msg),
        reply_to=_addrs(msg, "Reply-To"),
    )
