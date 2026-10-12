"""Send the automatic replies canopy-web could not send itself.

When an admission rule rejects an email turn, the sender is owed a fixed-text
reply on their thread from the agent's mailbox (canopy-web#1400). canopy-web
sends it when it can refresh the mailbox's token; most agents' tokens are on the
fleet's desktop `canopy` client, which only a box with gog can use. Those
notices wait as PENDING on canopy-web (`apps/admission/runner_notices.py`), and
this module — on the same tick that polls the mailbox — leases them, sends each
through gog, and reports what happened (canopy-web#1416).

The text is canopy-web's, never written here or by a model. What this module
adds is only what a box can see and canopy-web cannot:

* the message's own headers — a machine-written message (`Auto-Submitted`,
  `Precedence`, list mail, a no-reply sender) is never answered (`skipped`);
* the thread — if it already carries this mailbox's automatic reply (a send
  whose report was lost), it is not answered again;
* threading — `In-Reply-To`/`References` from the message's real Message-ID, and
  `--thread-id` so Gmail files it on the sender's thread.

Every reply carries `Auto-Submitted: auto-replied` (RFC 3834) and the thread is
marked read afterwards. Stdlib-only, like `inbox.py`, so the cloud runner
imports it off the clone.
"""
from __future__ import annotations

import json
import re
import subprocess
from email.message import EmailMessage
from email.utils import parseaddr

from . import inbox

#: The automatic reply's own marker, which `already_answered` looks for.
AUTO_SUBMITTED = "auto-replied"


def _headers(msg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in (msg.get("payload") or {}).get("headers") or []:
        out.setdefault((h.get("name") or "").lower(), h.get("value") or "")
    return out


def _gog(argv: list[str], *, runner=subprocess.run, stdin: str | None = None):
    try:
        r = runner(argv, capture_output=True, text=True, timeout=45, input=stdin)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, f"gog: {exc}"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:300] or f"gog exited {r.returncode}"
    return r, ""


def thread_messages(mailbox: str, client: str, thread_id: str, *, runner=subprocess.run):
    """(messages, error) — the thread's raw Gmail messages, oldest first."""
    r, err = _gog(["gog", "gmail", "thread", "get", thread_id, "--account", mailbox,
                   "--client", client, "--json"], runner=runner)
    if r is None:
        return None, err
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return None, "non-JSON from gog gmail thread get"
    return data.get("messages") or (data.get("thread") or {}).get("messages") or [], ""


def is_automated(headers: dict[str, str]) -> bool:
    """`inbox.is_automated`, plus list mail — the same set canopy-web refuses."""
    return inbox.is_automated(headers) or bool(headers.get("list-id"))


def already_answered(messages: list[dict], target_index: int, mailbox: str) -> bool:
    """Does the thread already carry this mailbox's automatic reply after the
    message being answered? Then a report was lost, not a reply."""
    box = mailbox.lower()
    for msg in messages[target_index + 1:]:
        h = _headers(msg)
        if box in (h.get("from") or "").lower() and \
                (h.get("auto-submitted") or "").strip().lower() == AUTO_SUBMITTED:
            return True
    return False


def compose(notice: dict, mailbox: str, headers: dict[str, str]) -> str:
    """The RFC822 reply, as text. canopy-web's subject and body, the message's
    threading headers, and the automatic-reply marker."""
    msg = EmailMessage()
    name = (notice.get("agent_name") or "").replace('"', "")
    msg["From"] = f'"{name}" <{mailbox}>' if name else mailbox
    msg["To"] = notice["to"]
    original = headers.get("subject") or notice.get("subject") or ""
    msg["Subject"] = original if re.match(r"(?i)^re:", original) else f"Re: {original}".strip()
    rfc_id = headers.get("message-id", "")
    if rfc_id:
        msg["In-Reply-To"] = rfc_id
        msg["References"] = f"{headers.get('references', '')} {rfc_id}".strip()
    msg["Auto-Submitted"] = AUTO_SUBMITTED
    msg.set_content(notice["body"])
    return msg.as_string()


def send_one(notice: dict, mailbox: str, client: str, *, runner=subprocess.run) -> tuple[str, str]:
    """Send one leased notice. Returns (outcome, detail): `sent`, `skipped`
    (never to be sent) or `failed` (canopy-web leaves it for a retry)."""
    want = (notice.get("mailbox") or "").strip().lower()
    if want and want != mailbox.strip().lower():
        return "failed", f"this box reads {mailbox}, the reply must come from {want}"
    thread_id = notice["thread_id"]
    messages, err = thread_messages(mailbox, client, thread_id, runner=runner)
    if messages is None:
        return "failed", f"could not read the thread: {err}"
    if not messages:
        return "failed", "the thread has no messages"
    index = len(messages) - 1
    if notice.get("message_id"):
        found = [i for i, m in enumerate(messages) if m.get("id") == notice["message_id"]]
        if not found:
            return "failed", "the message to answer is not on the thread"
        index = found[0]
    headers = _headers(messages[index])
    if is_automated(headers):
        return "skipped", "not replied to: the message carries automation headers"
    sender = parseaddr(headers.get("from") or "")[1].strip().lower()
    if sender and sender != (notice.get("to") or "").strip().lower():
        return "skipped", f"not replied to: the message is from {sender}, not {notice.get('to')}"
    if already_answered(messages, index, mailbox):
        return "sent", "the thread already carries this mailbox's automatic reply"
    raw = compose(notice, mailbox, headers)
    r, err = _gog(["gog", "gmail", "send", "--raw-file", "-", "--thread-id", thread_id,
                   "--account", mailbox, "--client", client, "--json", "--no-input"],
                  runner=runner, stdin=raw)
    if r is None:
        return "failed", f"gog gmail send: {err}"
    try:
        sent_id = (json.loads(r.stdout or "{}") or {}).get("id", "")
    except ValueError:
        sent_id = ""
    # Best-effort, like canopy-web's: the reply went, an unread thread is not
    # worth reporting it as anything else.
    _gog(["gog", "gmail", "thread", "modify", thread_id, "--remove=UNREAD", "--account",
          mailbox, "--client", client, "--no-input"], runner=runner)
    return "sent", f"replied on the thread from this box (gmail message {sent_id or '?'})"


def deliver(client, runner_id: str, boxes: dict[str, dict], *, runner=subprocess.run,
            log=None) -> list[tuple[int, str, str]]:
    """Lease, send and report the pending notices for `boxes`
    ({agent slug: {"account", "client"}}). Returns (id, outcome, detail) for
    each. One notice failing never stops the rest; nothing here raises past a
    failed lease request, which the caller logs."""
    readable = {slug: (b.get("account") or "").strip() for slug, b in boxes.items()
                if (b.get("account") or "").strip() and (b.get("client") or "").strip()}
    if not readable:
        return []
    out = []
    for notice in client.claim_admission_notices(runner_id, readable):
        box = boxes.get(notice.get("agent")) or {}
        try:
            outcome, detail = send_one(notice, box["account"], box["client"], runner=runner)
        except Exception as exc:  # noqa: BLE001 — one bad notice never stops the rest
            outcome, detail = "failed", f"runner error: {type(exc).__name__}: {exc}"
        try:
            client.report_admission_notice(runner_id, notice["id"], outcome, detail)
        except Exception as exc:  # noqa: BLE001 — the lease runs out and it is retried
            detail = f"{detail} (report failed: {exc})"
        if log:
            log(f"admission notice {notice['id']} ({notice.get('agent')}, thread "
                f"{notice.get('thread_id')}): {outcome} — {detail}")
        out.append((notice["id"], outcome, detail))
    return out
