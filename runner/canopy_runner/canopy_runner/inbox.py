"""Deterministic inbox trigger: read an agent's gmail and enqueue one email-origin
turn per NEW thread state. Runs in the runner loop — NO Ada, NO LLM judgment in the
hot path; "new email on a thread → a turn" is a fixed rule.

`gog gmail search --json` returns {threads: [{id (=thread_id), from, subject, date,
messageCount, labels}]}. Idempotency is keyed on (thread, messageCount) so each new
reply fires exactly one turn and re-polling the SAME state never double-fires. The
enqueued turn carries the thread_id, so execute_turn resolves it to the existing
session (continuity) or a fresh one.
"""
from __future__ import annotations

import json
import subprocess

# UNREAD only — the "new email" signal. Critically NOT "all recent threads":
# every matched thread becomes a turn → a claude session, so an over-broad query
# is a cost bomb. Idempotency (thread+messageCount) means an unread thread fires
# exactly once until its state changes.
DEFAULT_QUERY = "in:inbox is:unread newer_than:14d"


class InboxError(Exception):
    pass


def search_threads(mailbox: str, gog_client: str, query: str = DEFAULT_QUERY,
                   max_threads: int = 15, *, runner=subprocess.run) -> list[dict]:
    try:
        r = runner(
            ["gog", "gmail", "search", "--account", mailbox, "--client", gog_client,
             query, "--max", str(max_threads), "--json"],
            capture_output=True, text=True, timeout=45,
        )
    except FileNotFoundError as exc:
        raise InboxError("gog not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise InboxError("gog gmail search timed out") from exc
    if r.returncode != 0:
        raise InboxError(r.stderr.strip() or "gog gmail search failed")
    try:
        return json.loads(r.stdout or "{}").get("threads", []) or []
    except ValueError as exc:
        raise InboxError(f"non-JSON from gog gmail search: {(r.stdout or '')[:150]!r}") from exc


def newest_sender(mailbox: str, gog_client: str, thread_id: str, *,
                  runner=subprocess.run) -> str | None:
    """Return the From value of a thread's NEWEST message, lowercased — or None if it
    can't be determined (gog missing/failed/timed-out, or an unparseable thread). None
    is the fail-open signal: the caller enqueues rather than risk dropping a real reply."""
    try:
        r = runner(
            ["gog", "gmail", "thread", "get", thread_id, "--account", mailbox,
             "--client", gog_client, "--json"],
            capture_output=True, text=True, timeout=45,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    msgs = data.get("messages") or (data.get("thread") or {}).get("messages") or []
    if not msgs:
        return None
    headers = (msgs[-1].get("payload") or {}).get("headers") or []
    for h in headers:
        if h.get("name", "").lower() == "from":
            return (h.get("value") or "").lower()
    return None


def check_inbox(client, agent: str, *, mailbox: str, gog_client: str,
                query: str = DEFAULT_QUERY, max_threads: int = 15, runner=subprocess.run,
                sender_of=None) -> dict:
    """Enqueue an email-origin turn for each new thread state. Returns
    {"new": [thread_ids that became a NEW turn], "seen": [ids already tracked],
    "skipped": [ids whose newest message is the agent's own reply]} — the split matters
    for logging: re-polling the same unread mail is idempotent server-side, so it must
    read as "nothing new", not as fresh work.

    The `skipped` guard closes a real bug: idempotency is keyed on (thread, messageCount),
    but the agent's OWN reply bumps the count to a value the watcher never registered
    (the thread was read by the time it replied). If anything later re-marks that thread
    unread WITHOUT a new inbound message (a human nudge, a Gmail label reshuffle), the
    watcher would see a fresh (thread, count) and fire a turn whose "trigger" is the
    agent's own last reply. So: if the newest message in a thread is from the agent
    itself, it has already had the last word — skip it. `sender_of(thread_id) -> str|None`
    is injectable for tests; it defaults to a live `newest_sender` lookup and fails open
    (None -> enqueue) so an unreadable thread never silently drops a real reply."""
    if sender_of is None:
        def sender_of(tid: str) -> str | None:
            return newest_sender(mailbox, gog_client, tid, runner=runner)
    threads = search_threads(mailbox, gog_client, query, max_threads, runner=runner)
    new: list[str] = []
    seen: list[str] = []
    skipped: list[str] = []
    box = mailbox.lower()
    for t in threads:
        tid = t.get("id")
        if not tid:
            continue
        latest = sender_of(tid)
        if latest and box in latest:
            skipped.append(tid)
            continue
        count = t.get("messageCount", 1)
        frm, subj = t.get("from", ""), t.get("subject", "")
        # Clean command only — the agent's namespaced /<slug>:turn command does everything (reads the
        # thread, triages under guardrails, marks it read). The runner hands the exact
        # thread it already resolved so the agent doesn't re-scan the inbox.
        res = client.enqueue_turn(
            agent, "email", f"email-{agent}-{tid}-{count}",
            origin_ref={"thread_id": tid, "from": frm, "subject": subj},
            prompt=f"/{agent}:turn --thread {tid}",
        )
        (new if (res or {}).get("_created") else seen).append(tid)
    return {"new": new, "seen": seen, "skipped": skipped}
