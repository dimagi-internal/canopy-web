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

import base64
import binascii
import json
import re
import subprocess
import time
from typing import NamedTuple

# UNREAD only — the "new email" signal. Critically NOT "all recent threads":
# every matched thread becomes a turn → a claude session, so an over-broad query
# is a cost bomb. Idempotency (thread+messageCount) means an unread thread fires
# exactly once until its state changes.
DEFAULT_QUERY = "in:inbox is:unread newer_than:14d"

#: A CloudWatch/SNS alarm notification subject: `ALARM: "<name>" in <region>`, and its
#: matching `OK: "<name>" in <region>`. The quoted alarm name is what pairs the two.
_ALARM_SUBJECT = re.compile(r'^\s*(ALARM|OK):\s*"([^"]+)"')

#: An alarm announcing its OWN CREATION, in the body of an otherwise ordinary `OK:`.
#:
#: CloudWatch emails every alarm carrying `OKActions` the moment it is created, and the
#: subject is indistinguishable from a real recovery — same `OK: "<name>" in <region>`
#: shape, same SNS sender. Only the body separates them, and it is unambiguous: a real
#: recovery always names a concrete prior state (`ALARM -> OK`, `INSUFFICIENT_DATA -> OK`),
#: while a creation has no prior state at all.
#:
#: So every monitoring PR that adds an alerting alarm used to dispatch a full agent
#: session on a non-event. Measured on hal twice in two days —
#: `labs-jj-web-cpu-high-actionable` (2026-09-06, connect-labs#1463) and
#: `labs-jj-web-worker-kill-rate-actionable` (2026-09-07, connect-labs#1537, emailed
#: 8 minutes after that PR merged). See #688.
_ALARM_CREATION = re.compile(r"^\s*-?\s*State Change:\s*N/A\s*->\s*OK\s*$",
                             re.MULTILINE)


def alarm_key(thread: dict) -> tuple[str, str] | None:
    """``(state, alarm_name)`` for a CloudWatch alarm notification, else ``None``.

    Deliberately narrow on BOTH axes, because everything downstream of it suppresses a
    turn and a false positive here is a silently-dropped message:

    * the sender must be SNS — a human writing ``OK: "the deploy" in staging`` is not an
      alarm and must never be coalesced;
    * the subject must match the CloudWatch shape exactly.

    Anything unrecognised returns ``None``, which every caller treats as "enqueue
    normally". That is the fail-open direction.
    """
    if "sns.amazonaws.com" not in (thread.get("from") or "").lower():
        return None
    m = _ALARM_SUBJECT.match(thread.get("subject") or "")
    return (m.group(1).upper(), m.group(2)) if m else None


def _alarm_incident_is_owned(box: str, key: tuple[str, str], alarming: set[str],
                             now: float) -> bool:
    """Does a turn already own this alarm notification's incident?

    ``key`` is an `alarm_key()` result, so this is only ever asked about a genuine SNS
    CloudWatch notification — everything else short-circuits before here and enqueues
    normally. Returning True suppresses a turn, so every branch is written to fail toward
    enqueueing when it does not KNOW.
    """
    state, name = key
    last = _alarm_enqueued.get((box, name))
    if state == "OK":
        # #670's rule (its `ALARM:` is unread in this same batch) OR the same alarm fired
        # recently enough that this is plainly its recovery.
        return name in alarming or (last is not None and now - last < OK_PAIRING_WINDOW_S)
    # A repeat `ALARM:` inside the window is the same incident re-notifying. The FIRST one
    # always enqueues — there is no entry to compare against until we have enqueued once.
    return last is not None and now - last < ALARM_REPEAT_WINDOW_S


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


class ThreadFacts(NamedTuple):
    """What one `gog gmail thread get` tells us about a thread's NEWEST message.

    Both fields fail OPEN — `newest_from=None` and `is_alarm_creation=False` are the
    "we don't know" values, and every caller treats them as "enqueue normally".
    """

    #: The newest message's `From`, lowercased, or None if undeterminable.
    newest_from: str | None = None
    #: True only when the body positively identifies an alarm announcing its own
    #: creation (see `_ALARM_CREATION`). Never a guess.
    is_alarm_creation: bool = False


def _decoded_body(msg: dict) -> str:
    """The newest message's text body, or "" when it can't be decoded.

    `gog gmail thread get --json` hands back the raw Gmail payload, so the body is
    base64url in `payload.body.data` for a `text/plain` mail and in a part for a
    multipart one. Anything unexpected returns "" — which reads as "not a creation
    notice" and enqueues, the fail-open direction.
    """
    payload = msg.get("payload") or {}
    chunks = [payload] + list(payload.get("parts") or [])
    out = []
    for p in chunks:
        if (p.get("mimeType") or "").startswith("text/") or p is payload:
            data = (p.get("body") or {}).get("data")
            if not data:
                continue
            try:
                out.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
            except (binascii.Error, ValueError):
                continue
    return "\n".join(out)


def thread_facts(mailbox: str, gog_client: str, thread_id: str, *,
                 runner=subprocess.run) -> ThreadFacts:
    """ONE `gog gmail thread get`, every fact the enqueue decision needs.

    This subprocess is the expensive call in this module, and it already returns the
    whole message — headers AND body. It used to be spent on the `From` header alone
    and the rest discarded, which is why the alarm-creation check in #688 was believed
    to cost a body read it does not: the read is already paid for, only the parse is
    new. Keep it that way — anything else the hot path needs from a thread belongs
    here, not in a second fetch.
    """
    try:
        r = runner(
            ["gog", "gmail", "thread", "get", thread_id, "--account", mailbox,
             "--client", gog_client, "--json"],
            capture_output=True, text=True, timeout=45,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ThreadFacts()
    if r.returncode != 0:
        return ThreadFacts()
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return ThreadFacts()
    msgs = data.get("messages") or (data.get("thread") or {}).get("messages") or []
    if not msgs:
        return ThreadFacts()
    newest = msgs[-1]
    sender = None
    for h in (newest.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == "from":
            sender = (h.get("value") or "").lower()
            break
    return ThreadFacts(newest_from=sender,
                       is_alarm_creation=bool(_ALARM_CREATION.search(_decoded_body(newest))))


def newest_sender(mailbox: str, gog_client: str, thread_id: str, *,
                  runner=subprocess.run) -> str | None:
    """Return the From value of a thread's NEWEST message, lowercased — or None if it
    can't be determined (gog missing/failed/timed-out, or an unparseable thread). None
    is the fail-open signal: the caller enqueues rather than risk dropping a real reply."""
    return thread_facts(mailbox, gog_client, thread_id, runner=runner).newest_from


#: Thread states this runner has already turned into a turn, as
#: ``{(mailbox, thread_id): messageCount}``. Process-local and deliberately not
#: persisted: the SERVER is the authority on idempotency (the enqueue is keyed on
#: ``email-<agent>-<thread>-<count>``), so losing this on restart costs one
#: redundant round of work, never a duplicate turn.
#:
#: It exists purely to stop paying for what we already know. `newest_sender()` is
#: a `gog gmail thread get` SUBPROCESS, and it used to run for every unread thread
#: on every poll — before the idempotency check — then POST an enqueue the server
#: deduped anyway. With four unread threads sitting in two mailboxes that was
#: eight subprocess round-trips every five minutes, forever, all concluding
#: "already tracked".
_seen_state: dict[tuple[str, str], int] = {}

#: A re-fired `ALARM:` for the same alarm name inside this window is the SAME incident.
#:
#: CloudWatch re-evaluates a metric alarm roughly every 60s over a SLIDING window, so a
#: condition sitting near its threshold flips ALARM->OK->ALARM every few minutes for as
#: long as it lasts. Each flip is a fresh notification, and because Gmail threads by
#: subject they all land on ONE thread with a bumped `messageCount` — which is a new
#: idempotency key, so each one enqueued another turn.
#:
#: Measured on hal, 2026-09-07: `labs-jj-web-cpu-high-actionable` transitioned to ALARM at
#: 12:23:56, 12:29:21 and 12:34:21 UTC for a single incident, dispatching THREE turns onto
#: one thread. The third had nothing left to find — the first had already diagnosed it,
#: shipped the fix and closed the storm out.
#:
#: 15 minutes covers the observed re-fire spacing (~5 min) with margin. It is deliberately
#: NOT open-ended: a genuinely sustained incident re-notifies after the window, which is
#: the right behaviour — one look per quarter hour, not one per flip.
ALARM_REPEAT_WINDOW_S = 15 * 60

#: How long after an `ALARM:` its matching `OK:` is still that incident's recovery.
#:
#: Much longer than the repeat window, because the two are not the same judgment. An `OK:`
#: is never work ON ITS OWN — its `ALARM:` owns the incident, and the agent-side rule is
#: explicit that the `OK:` turn's whole job is to discover it should stand down. So the
#: only question is "did this alarm recently fire", and a generous answer is the safe one.
#:
#: #670 answered it with "is the `ALARM:` in THIS batch", which holds only while both are
#: unread in the same poll. On 2026-09-07 the `OK:` landed 24 minutes after its `ALARM:`,
#: by which time the `ALARM:` thread had been read and closed out — so the batch was empty
#: of it, the `OK:` enqueued a turn, and that session spent 65 events reading the turn
#: procedure before stalling with nothing to do.
OK_PAIRING_WINDOW_S = 2 * 60 * 60

#: ``{(mailbox, alarm_name): wall-clock time we last ENQUEUED a turn for it}``. Written
#: only on a real enqueue, never when coalescing — so a flapping alarm is quiet for one
#: window and then genuinely re-notifies, rather than being suppressed forever by its own
#: repeats. Process-local for the same reason as `_seen_state`: losing it on restart costs
#: one redundant turn, never a dropped incident. That is the fail-open direction.
_alarm_enqueued: dict[tuple[str, str], float] = {}


def check_inbox(client, agent: str, *, mailbox: str, gog_client: str,
                query: str = DEFAULT_QUERY, max_threads: int = 15, runner=subprocess.run,
                sender_of=None, facts_of=None, discovered_by: str = "poll",
                clock=time.time) -> dict:
    """Enqueue an email-origin turn for each new thread state. Returns
    {"new": [thread_ids that became a NEW turn], "seen": [ids already tracked],
    "skipped": [ids whose newest message is the agent's own reply],
    "coalesced": [SNS alarm ids folded into an existing incident's turn — an `OK:`
    recovery, or an `ALARM:` re-firing inside ALARM_REPEAT_WINDOW_S],
    "created": [SNS `OK:` ids that are an alarm announcing its OWN creation — a
    non-event, in its own bucket because there is no incident to fold it into]}
    — the split matters
    for logging: re-polling the same unread mail is idempotent server-side, so it must
    read as "nothing new", not as fresh work.

    The `skipped` guard closes a real bug: idempotency is keyed on (thread, messageCount),
    but the agent's OWN reply bumps the count to a value the watcher never registered
    (the thread was read by the time it replied). If anything later re-marks that thread
    unread WITHOUT a new inbound message (a human nudge, a Gmail label reshuffle), the
    watcher would see a fresh (thread, count) and fire a turn whose "trigger" is the
    agent's own last reply. So: if the newest message in a thread is from the agent
    itself, it has already had the last word — skip it. `facts_of(thread_id) ->
    ThreadFacts` is injectable for tests; it defaults to a live `thread_facts` lookup
    and fails open (unknown -> enqueue) so an unreadable thread never silently drops a
    real reply. `sender_of(thread_id) -> str|None` is the narrower legacy seam, kept
    because existing callers and tests inject it; supplying it opts out of every fact
    that needs the body, so a `sender_of`-injected call behaves exactly as before."""
    if facts_of is None:
        if sender_of is not None:
            def facts_of(tid: str) -> ThreadFacts:
                return ThreadFacts(newest_from=sender_of(tid))
        else:
            def facts_of(tid: str) -> ThreadFacts:
                return thread_facts(mailbox, gog_client, tid, runner=runner)
    threads = search_threads(mailbox, gog_client, query, max_threads, runner=runner)
    new: list[str] = []
    seen: list[str] = []
    skipped: list[str] = []
    coalesced: list[str] = []
    created: list[str] = []
    box = mailbox.lower()
    # ONE INCIDENT, ONE TURN. CloudWatch emits `ALARM:` and `OK:` as two Gmail threads
    # (the subjects differ), so one alarm transition used to enqueue two turns — and the
    # `OK:` half can never produce a finding: its `ALARM:` already owns the incident, so
    # the second session's whole job is to discover it should stand down.
    #
    # Measured on hal, 2026-08-31 -> 09-05: 10 sessions for 5 incidents, the `OK:` halves
    # burning 3,186 of 7,232 transcript events to conclude "not mine" (one of them 1,362
    # events, having taken over from a stalled owner and died on a usage limit).
    #
    # So an `OK:` whose alarm ALSO has an `ALARM:` thread in this batch is coalesced into
    # that turn. Deterministic and free — both threads are already in hand, so this costs
    # no extra subprocess and no LLM judgment, preserving this module's "fixed rule, no
    # judgment in the hot path" contract.
    alarming = {k[1] for k in (alarm_key(t) for t in threads) if k and k[0] == "ALARM"}
    for t in threads:
        tid = t.get("id")
        if not tid:
            continue
        count = t.get("messageCount", 1)
        # Already turned this exact thread state into a turn. Skip BEFORE the
        # `gog gmail thread get` subprocess and before the enqueue — both would
        # be no-ops, and the subprocess is the expensive one.
        if _seen_state.get((box, tid)) == count:
            seen.append(tid)
            continue
        # Coalesce BEFORE `sender_of`, which is a `gog gmail thread get` subprocess —
        # the same cost-ordering reason the idempotency check sits above.
        #
        # Two ways one incident still reached us as several turns after #670, both
        # measured on hal 2026-09-07 and both fixed by remembering WHEN we last enqueued
        # for an alarm name rather than only looking inside the current batch:
        #
        #   * a re-fired `ALARM:` — the flip lands on the SAME Gmail thread with a bumped
        #     messageCount, which is a new idempotency key, so it enqueued again;
        #   * an `OK:` whose `ALARM:` was in an EARLIER batch — `alarming` is per-poll, so
        #     a recovery arriving after its `ALARM:` was read fell straight through it.
        key = alarm_key(t)
        if key and _alarm_incident_is_owned(box, key, alarming, clock()):
            coalesced.append(tid)
            # Remember it so the next poll doesn't re-evaluate the same state. NOTE:
            # `_seen_state` is process-local (see its docstring), so a runner restart
            # while this `OK:` is still unread can fire one turn for it. That is exactly
            # today's behaviour — strictly never worse — and closing it properly belongs
            # on the agent side: the `ALARM:` owner groups the storm by alarm name and
            # marks the whole storm read.
            _seen_state[(box, tid)] = count
            continue
        facts = facts_of(tid)
        # An alarm announcing its OWN creation. This is the one check that needs the
        # body, and it is deliberately placed AFTER the two cheap guards above and on
        # the SAME fetch as the sender — so it costs no subprocess this path was not
        # already paying for (see `thread_facts`). Narrow on purpose: only an `OK:`
        # from SNS (`alarm_key`) whose body says `N/A -> OK`. A real recovery names a
        # concrete prior state and still fires. #688.
        if key and key[0] == "OK" and facts.is_alarm_creation:
            created.append(tid)
            _seen_state[(box, tid)] = count
            continue
        latest = facts.newest_from
        if latest and box in latest:
            skipped.append(tid)
            # Remember it, so a thread the agent already answered does not cost a
            # `thread get` on every poll for the next fourteen days.
            _seen_state[(box, tid)] = count
            continue
        frm, subj = t.get("from", ""), t.get("subject", "")
        # Clean command only — the agent's namespaced /<slug>:turn command does everything (reads the
        # thread, triages under guardrails, marks it read). The runner hands the exact
        # thread it already resolved so the agent doesn't re-scan the inbox.
        res = client.enqueue_turn(
            agent, "email", f"email-{agent}-{tid}-{count}",
            # `discovered_by` is what makes the 300s timer an AUDITOR of the push
            # path: the server compares it against the mailbox's Gmail watch, and
            # a `poll`-discovered message on a mailbox with a live watch means
            # push is registered but not delivering.
            origin_ref={"thread_id": tid, "from": frm, "subject": subj,
                        "discovered_by": discovered_by},
            prompt=f"/{agent}:turn --thread {tid}",
        )
        _seen_state[(box, tid)] = count
        # Stamp the incident only on a real enqueue, so the window is measured from the
        # turn that owns it. Deliberately NOT refreshed when coalescing: a re-fire must
        # not extend its own suppression, or an alarm flapping for an hour would be
        # reported once and then silently held down for the whole hour.
        if key and key[0] == "ALARM":
            _alarm_enqueued[(box, key[1])] = clock()
        (new if (res or {}).get("_created") else seen).append(tid)
    return {"new": new, "seen": seen, "skipped": skipped, "coalesced": coalesced,
            "created": created}
