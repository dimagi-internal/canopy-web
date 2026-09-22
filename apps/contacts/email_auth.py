"""Reading a mail server's verdict on whether a `From:` address was forged.

WHAT THE THREE MECHANISMS ACTUALLY PROVE, because they are routinely conflated
and only one of them answers the question anyone cares about:

* **SPF** — DNS lists which IPs may send for a domain, checked against the
  ENVELOPE sender (`Return-Path`), *not* the `From:` a human reads. So `spf=pass`
  is entirely compatible with a message displaying someone else's address. It is
  the weakest useful grade for that reason.
* **DKIM** — a cryptographic signature over headers and body, key in DNS. Proves
  a domain vouched for the message and that it was not modified in transit.
* **DMARC** — requires SPF or DKIM to ALIGN with the visible `From:` domain.
  This is the one that says "the address the human sees is not forged", which
  is why it is the top grade here.

**ONLY THE RECEIVING SERVER'S OWN VERDICT COUNTS.** `Authentication-Results` is
an ordinary header: anyone can put `Authentication-Results: dmarc=pass` in a
message they send, and a naive parser that scans all of them will believe it.
The rule is that only the header added by the trusted boundary — our own
receiver — may be read, and RFC 8601 puts the most recently added header FIRST.
`grade_from_headers` therefore takes the receiver's `authserv-id` explicitly and
refuses anything that does not match it. Being handed a forged header must
produce `none`, not a pass; there is a test for exactly that.

**GRADES, NOT A BOOLEAN.** Partner organisations run mail of wildly varying
quality, and refusing to talk to a clinic because their host has no DMARC record
is not an option. So every message yields a tier, "unverified" is a workable
state, and policy decides what each tier may unlock. What must never happen is
an unauthenticated `From:` GRANTING anything.

**AND THE LIMIT WORTH REPEATING:** even `dmarc=pass` proves the *domain* sent
it. It says nothing about which human did. Person-level identity needs an
out-of-band step.
"""
from __future__ import annotations

import re

from .models import Contact

#: `method=result` pairs, e.g. `dkim=pass`, `spf=softfail`, `dmarc=pass`.
#: Deliberately tolerant of surrounding whitespace and of the `(comment)` forms
#: real servers emit — `spf=pass (google.com: domain of x designates ...)`.
_METHOD_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)

#: The `header.from=` / `smtp.mailfrom=` property, used to check that the domain
#: the verdict is ABOUT is the domain we think we are talking to.
_HEADER_FROM_RE = re.compile(r"header\.from\s*=\s*([^\s;,()]+)", re.IGNORECASE)


#: The DKIM signing domain: `header.d=example.org`, or the domain of the
#: agent/user identity `header.i=@example.org` (Gmail writes the latter).
_DKIM_D_RE = re.compile(r"\bdkim\s*=\s*pass\b[^;]*?\bheader\.d\s*=\s*([^\s;,()]+)", re.IGNORECASE)
_DKIM_I_RE = re.compile(r"\bdkim\s*=\s*pass\b[^;]*?\bheader\.i\s*=\s*([^\s;,()]+)", re.IGNORECASE)


def _dkim_signer(auth_results: str) -> str:
    m = _DKIM_D_RE.search(auth_results or "")
    if m:
        return m.group(1).strip(". ").lower()
    m = _DKIM_I_RE.search(auth_results or "")
    return m.group(1).rpartition("@")[2].strip(". ").lower() if m else ""


def _domain_of(address: str) -> str:
    return (address or "").strip().lower().rpartition("@")[2]


def grade_of(auth_results: str, *, from_address: str = "") -> str:
    """Grade a SINGLE `Authentication-Results` value that we already trust.

    Trust is the caller's job — use `grade_from_headers` unless you have
    already established that this header came from your own receiver.

    When `from_address` is given, a `dmarc=pass` is only honoured if the
    header's `header.from=` domain matches it. Without that check a message
    could carry a genuine DMARC pass for a domain unrelated to the address we
    are attributing the contact to, which is precisely the alignment property
    DMARC exists to provide and would be silently discarded.
    """
    if not auth_results:
        return Contact.AUTH_NONE

    verdicts = {m.group(1).lower(): m.group(2).lower() for m in _METHOD_RE.finditer(auth_results)}

    if verdicts.get("dmarc") == "pass":
        if from_address:
            m = _HEADER_FROM_RE.search(auth_results)
            # A pass with no stated `header.from` cannot be shown to be aligned
            # with the address we are attributing, so it does not earn the top
            # grade. Falls through to DKIM/SPF rather than being discarded.
            if m and _domain_of("@" + m.group(1).strip(". ")) == _domain_of(from_address):
                return Contact.AUTH_DMARC
            if not m:
                return Contact.AUTH_DKIM if verdicts.get("dkim") == "pass" else (
                    Contact.AUTH_SPF if verdicts.get("spf") == "pass" else Contact.AUTH_NONE
                )
        else:
            return Contact.AUTH_DMARC
    if verdicts.get("dkim") == "pass":
        # Signed BY the From: domain → as strong as a DMARC pass via DKIM. Only
        # checkable when we know whose From: it is; otherwise plain DKIM.
        if from_address and _dkim_signer(auth_results) == _domain_of(from_address):
            return Contact.AUTH_DKIM_ALIGNED
        return Contact.AUTH_DKIM
    if verdicts.get("spf") == "pass":
        return Contact.AUTH_SPF
    return Contact.AUTH_NONE


def grade_from_headers(
    headers: list[tuple[str, str]] | list[dict],
    *,
    authserv_id: str,
    from_address: str = "",
) -> tuple[str, str]:
    """Grade a message from its full header list. Returns `(grade, raw_header)`.

    `headers` accepts either `(name, value)` pairs or Gmail API dicts
    (`{"name": ..., "value": ...}`), because the runner ships whichever it has.

    `authserv_id` is OUR receiver's identity — `mx.google.com` for a Gmail
    mailbox. Headers whose authserv-id is anything else are IGNORED, including
    ones an attacker put in the message themselves. This is the security
    boundary of the whole module: without it, "is this sender authenticated" is
    answered by the sender.

    Returns the FIRST matching header, per RFC 8601: the most recently added
    appears first, and that is the one our own server wrote.
    """
    if not authserv_id:
        # No trusted receiver named means nothing can be trusted. Fail closed
        # rather than falling back to "read whatever is there".
        return Contact.AUTH_NONE, ""

    wanted = authserv_id.strip().lower()
    for h in headers or []:
        name, value = (h.get("name", ""), h.get("value", "")) if isinstance(h, dict) else h
        if (name or "").strip().lower() != "authentication-results":
            continue
        value = value or ""
        # The authserv-id is the first token, before the first `;`.
        serv = value.split(";", 1)[0].strip().lower()
        if serv != wanted:
            continue
        return grade_of(value, from_address=from_address), value
    return Contact.AUTH_NONE, ""
