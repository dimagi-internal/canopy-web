"""A Google Docs comment that tags the agent: the one Doc it may read and answer in.

Google sends every Doc, Sheet and Slides comment notification from one address,
`comments-noreply@docs.google.com`. The person who commented is in the display name and the
body, and nowhere Google vouches for: on a real notification (eva@ thread 1a20810ba581b0a9)
there is no commenter email in any header or in the body. Anyone with a Google
account can make a Doc, share it with an agent and @-mention it, under any name. So a Docs
comment is never run as the person it names.

What the message DOES prove (DMARC aligned for docs.google.com, on our own receiver's verdict) is
that Google sent it, about one file. That is enough for a narrow answer: the turn runs in canopy's
own `doc_comment` profile (`published_profiles.doc_comment_profile`), which may read that one Doc
and its comments and reply on its comment threads, as the agent's own mailbox, and nothing else:
no mail, no other file, no Salesforce, no canopy tool but `who_is_asking`. Whatever it reads it
can only write back where it read it, to the people who can already see that Doc.

The runner reads the ids off Google's own links in the notification (`origin_ref["doc_comment"]`,
`runner/canopy_runner/canopy_runner/inbox_rules.py::doc_comment_of`) and only for a notification
that tags the agent; this module decides whether a turn's ids are honoured. Nothing here grants
anything a turn did not have: `turn_context` only NARROWS a Docs-comment turn to this profile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: The one address Google sends comment notifications from.
DOCS_COMMENTS = "comments-noreply@docs.google.com"

#: `origin_ref` key the runner writes for a Docs-comment notification that tags the agent.
ORIGIN_KEY = "doc_comment"

#: A Drive file id, as it appears in `/d/<id>/`. Also what makes it safe to write into a bash
#: allowlist literally: no shell or glob character can be in one.
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
#: The `disco=` value of Google's link to the comment. A hint for the agent, never in a rule.
_COMMENT_HINT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_KINDS = ("document", "spreadsheets", "presentation")
_ADDRESS = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass(frozen=True)
class DocComment:
    """The file a Docs-comment turn may read and answer in, and the mailbox it answers as."""

    file_id: str
    kind: str
    mailbox: str
    comment_hint: str = ""
    commenter_name: str = ""

    @property
    def url(self) -> str:
        return f"https://docs.google.com/{self.kind}/d/{self.file_id}/edit"

    def envelope(self) -> dict:
        """What the caller envelope tells the agent. `commenter_name` is Google's display name, a label:
        nobody's identity was established from it."""
        return {"file_id": self.file_id, "kind": self.kind, "url": self.url, "mailbox": self.mailbox,
                "comment_hint": self.comment_hint, "commenter_name": self.commenter_name,
                "commenter_verified": False}


def of_turn(turn) -> DocComment | None:
    """The Doc a turn may answer in, or None when it is not a proven Docs-comment turn. All of:

      * an email turn whose initiator is the contact `comments-noreply@docs.google.com`, graded aligned
        (DMARC, or DKIM signed by docs.google.com) on THIS message, by our own receiver's verdict;
      * `origin_ref["doc_comment"]` names a well-formed file id;
      * it came through a Gmail mailbox, and the mailbox it would answer as is one of the agent's own
        Gmail addresses.
    """
    from apps.contacts.models import Contact  # noqa: PLC0415 — models import the agents app
    from apps.harness.models import Turn  # noqa: PLC0415 — as above

    if turn is None or turn.origin != Turn.ORIGIN_EMAIL or turn.initiator_kind != "contact":
        return None
    if (turn.initiator_assurance or "") not in Contact.EMAIL_ALIGNED:
        return None
    contact = getattr(turn, "initiator_contact", None)
    if contact is None or (contact.email or "").strip().lower() != DOCS_COMMENTS:
        return None
    ref = turn.origin_ref if isinstance(turn.origin_ref, dict) else {}
    raw = ref.get(ORIGIN_KEY)
    if not isinstance(raw, dict):
        return None
    file_id, kind = str(raw.get("file_id") or ""), str(raw.get("kind") or "document")
    if not _FILE_ID.match(file_id) or kind not in _KINDS:
        return None
    if ref.get("backend") != "gmail":
        return None  # the reply goes out with gog as the agent's Google account; a hosted mailbox has none
    mailbox = _own_mailbox(turn.acting_agent, str(ref.get("mailbox") or ""))
    if mailbox is None:
        return None
    hint = str(raw.get("comment_hint") or "")
    return DocComment(file_id=file_id, kind=kind, mailbox=mailbox,
                      comment_hint=hint if _COMMENT_HINT.match(hint) else "",
                      commenter_name=_label(raw.get("commenter_name")))


def _own_mailbox(agent, claimed: str) -> str | None:
    """`claimed` when it is one of the agent's own addresses, else the agent's primary one, else None.
    The mailbox becomes the `--account` of every allowed command, so it is never taken on trust."""
    if agent is None:
        return None
    from apps.agents import mailbox as mailbox_mod  # noqa: PLC0415 — the mail app registers its provider late

    own = [a["address"].strip().lower() for a in mailbox_mod.addresses(agent)
           if a.get("address") and a.get("backend", "gmail") == "gmail"]
    own = [a for a in own if _ADDRESS.match(a)]
    claimed = claimed.strip().lower()
    if claimed in own:
        return claimed
    return own[0] if own else None


def _label(value) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()
    return text[:120]
