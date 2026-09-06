"""The emdash session-name format — one owner, so the shape has a single home.

WHAT A NAME IS FOR. It is read in the emdash sidebar, next to the human's own
sessions, and it answers two questions at a glance: did the fleet start this, and
what is it about. Everything here serves those two and nothing else.

    c-<subject>-<disc>          e.g. c-issue-triage-4a4e

  c-      canopy launched it. The sidebar interleaves fleet turns with sessions a
          human opened by hand ("bednet", "close-sessions"); without a marker the
          two are indistinguishable, and the fleet's are the ones you don't want
          to interrupt.
  subject WHAT it is about, from the ladder below.
  disc    four characters of the thread key. Uniqueness, and the reason it cannot
          be dropped: two DIFFERENT threads with the same subject used to collide
          onto one name, and the collision was only ever masked by the timestamp
          that used to follow it (test_execute.py has carried that case since).

WHAT IS DELIBERATELY NOT IN IT. The agent/repo slug: emdash already groups tasks
under their project, so `ace-` inside a name under `ace` is ten characters that
say nothing. The `MMDD-HHMM` stamp: emdash shows created-at in its own column and
sorts by it. Between them they took ~14 of a ~28-character budget, which is why
subjects arrived truncated mid-word ("...cpu-high-i").

THE LADDER is the substance of this module. The old rule was "email subject, else
the origin word", and since most turns carry no subject, most names came out as
the origin word — `ace-api-4a4e-0905-0920`, which says only that something used
the API. Each rung below is a source of MEANING the turn was already carrying and
nobody read: the board item's title, the schedule's human name, the slash command
in the brief. `origin` stays as the floor, where it now honestly means "this turn
told us nothing about itself".

READERS OF THIS FORMAT LIVE IN ANOTHER REPO. `canopy`'s
`orchestrator.agent_client._EMDASH_TASK` decides whether a worktree is a
dispatched session; it cannot import this module (the CLI and the runner share no
dependency), so it mirrors `CANOPY_PREFIX`. Keep `is_canopy_task` and that mirror
in step — a false negative there silently detaches an agent's close-out report
from its turn.
"""
from __future__ import annotations

import hashlib
import re

# The marker. Mirrored in canopy's orchestrator.agent_client — see module docstring.
CANOPY_PREFIX = "c-"

# Long enough for a real sentence fragment, short enough to read in a narrow
# sidebar. The old budget was 28 INCLUDING the agent slug; this one is the subject
# alone, so it is a meaningful increase despite the smaller-looking number.
SUBJECT_MAX = 34

# Four characters of thread key. Not a hash of anything meaningful — just enough
# entropy to separate same-subject threads.
#
# EXACTLY four, always — `_disc` pads a short key rather than emitting three. The
# rigidity is load-bearing outside this module: emdash appends its own 5-character
# de-dupe suffix to the WORKTREE directory (`c-issue-triage-4a4e-7ohfp`), and
# canopy's `emdash_task_from_cwd` recovers the task id from that directory by
# telling a 4-character tail from a 5-character one. The old format anchored that
# on its `-MMDD-HHMM` stamp; with the stamp gone, this is what replaces it.
DISC_LEN = 4

# The stamp `dispatch_marker.stamp_dispatched` appends, plus the provenance
# sentence beside it and the `_with_reply` delimiter. Every dispatched brief
# carries these verbatim, so a name derived from them would be identical across
# unrelated turns — the exact opposite of what a name is for.
_BOILERPLATE = re.compile(
    r"^(?:<!--.*?-->|—\s*Dispatched by\b.*|ANSWERED BY\b.*|-{3,}|#{1,6}\s*)$",
    re.IGNORECASE,
)

# `Re:`, `Fwd:` and friends, which say nothing about the thread and used to eat
# the front of every email-origin name ("hal-re-bednet-demo-...").
_REPLY_NOISE = re.compile(r"^(?:re|fwd|fw|aw|sv)\s*[:\-]\s*", re.IGNORECASE)

# A leading slash command: `/ada:turn`, `/canopy:issue-triage`, plain `/turn`.
_SLASH_COMMAND = re.compile(r"^/(?:(?P<ns>[A-Za-z0-9_-]+):)?(?P<cmd>[A-Za-z0-9_-]+)")

# Markdown/list scaffolding at the head of a line — bullets, quotes, numbering.
_LINE_SCAFFOLD = re.compile(r"^\s*(?:[-*+>]\s+|\d+[.)]\s+)")


def slugify(text: str, limit: int = SUBJECT_MAX) -> str:
    """Lowercase, hyphenated, and cut on a WORD boundary.

    The word boundary is the point: the previous implementation sliced at a fixed
    character count and produced `...cpu-high-i`, which reads as a typo rather than
    a truncation. A cut that lands mid-word falls back to the previous hyphen; a
    single word longer than the budget is cut hard, because a hyphenless 60-character
    token has no boundary to find.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(slug) <= limit:
        return slug
    cut = slug[:limit]
    if "-" in cut:
        cut = cut[: cut.rindex("-")]
    return cut.strip("-")


def _thread_key(turn: dict, target: str) -> str:
    """Mirror of execute._thread_key, which is the authority.

    Duplicated rather than imported because execute imports THIS module; the pair
    is asserted equal in tests/test_execute.py so the two cannot drift.
    """
    ref = turn.get("origin_ref") or {}
    explicit = ref.get("thread_key") or ref.get("thread_id")
    return explicit or f"{target}:{turn.get('id') or ''}"


def _from_slash_command(prompt: str, target: str) -> str:
    """The command a dispatched brief opens with, if it opens with one.

    The namespace is dropped ONLY when it repeats the target — `/ace:issue-triage`
    on ace is `issue-triage`, because emdash's own grouping already said "ace". A
    FOREIGN namespace is kept: `/canopy:issue-triage` running on ace is a canopy
    skill borrowed by ace, and that is worth the four characters.
    """
    for line in (prompt or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SLASH_COMMAND.match(line)
        if not m:
            return ""                      # a brief that starts with prose, not a command
        ns, cmd = (m.group("ns") or "").lower(), m.group("cmd")
        return cmd if not ns or ns == (target or "").lower() else f"{ns}-{cmd}"
    return ""


def _from_prompt_body(prompt: str) -> str:
    """The first line of the brief that a human actually wrote.

    Skips the dispatch boilerplate and any markdown scaffolding, and requires two
    word characters so a stray `ok` or a bare delimiter cannot name a session.
    """
    for raw in (prompt or "").splitlines():
        line = _LINE_SCAFFOLD.sub("", raw.strip())
        if not line or _BOILERPLATE.match(line) or line.startswith("/"):
            continue
        if len(re.findall(r"\w", line)) >= 2:
            return line
    return ""


def subject_for(turn: dict, target: str = "") -> str:
    """The meaningful half of the name — first rung that yields something, wins.

    Ordered by how deliberately a human chose the words. A board item's title and a
    schedule's name were TYPED by someone to name this exact work, so they outrank
    the brief's slash command, which is only how the work is spelled to the agent
    (`/echo:manager-report` vs "Weekly manager report" — same turn, and the second
    is the one worth reading in a sidebar).
    """
    ref = turn.get("origin_ref") or {}
    for candidate in (
        ref.get("item_title"),                                  # a board card someone wrote
        ref.get("schedule_name"),                               # "Weekly manager report"
        _REPLY_NOISE.sub("", (ref.get("subject") or "").strip()),   # an email thread
        _from_slash_command(turn.get("prompt"), target),        # /canopy:issue-triage
        _from_prompt_body(turn.get("prompt")),                  # the brief's own first line
        turn.get("origin"),                                     # floor: "we were told nothing"
    ):
        slug = slugify(candidate or "")
        if slug:
            return slug
    return ""


def _disc(turn: dict, target: str) -> str:
    """Exactly DISC_LEN characters of the thread key — see DISC_LEN on why exactly.

    A key with too few alphanumerics to fill the budget (a hand-set `thread_key`
    like "a:b") falls back to a digest OF that key, so the value stays derived from
    the thread — two turns on one thread still agree, which is what reuse needs.
    """
    key = _thread_key(turn, target)
    flat = re.sub(r"[^a-z0-9]", "", key.lower())
    if len(flat) >= DISC_LEN:
        return flat[-DISC_LEN:]
    return hashlib.sha256(key.encode()).hexdigest()[:DISC_LEN]


def build_task_name(target: str, turn: dict) -> str:
    """The emdash task name for `turn`, driven under project `target`.

    `target` is the emdash project (an agent slug or a repo name) — it is NOT put
    in the name; it is read to decide whether a slash command's namespace is
    redundant, and it keys the thread fallback.
    """
    bits = [b for b in (CANOPY_PREFIX.rstrip("-"), subject_for(turn, target),
                        _disc(turn, target)) if b]
    return "-".join(bits)


def is_canopy_task(name: str) -> bool:
    """True when canopy launched the session called `name`.

    Accepts the LEGACY shape too (`<agent>-<subject>-<disc>-MMDD-HHMM`). The fleet
    has live sessions carrying it and reuse resolves them by name; treating one as
    human-typed would detach its close-out from its turn.
    """
    n = (name or "").strip().lower()
    n = n[len("emdash-"):] if n.startswith("emdash-") else n
    return n.startswith(CANOPY_PREFIX) or bool(re.search(r"-\d{4}-\d{4}$", n))


def parse_task_name(name: str) -> dict | None:
    """Split a CURRENT-shape name back into its parts, or None if it isn't one.

    Legacy names return None deliberately: `is_canopy_task` is the question worth
    asking about them, and inventing a subject/disc split for a shape this module
    no longer produces would be guessing.
    """
    n = (name or "").strip().lower()
    n = n[len("emdash-"):] if n.startswith("emdash-") else n
    if not n.startswith(CANOPY_PREFIX):
        return None
    rest = n[len(CANOPY_PREFIX):]
    subject, _, disc = rest.rpartition("-")
    return {"subject": subject, "disc": disc}
