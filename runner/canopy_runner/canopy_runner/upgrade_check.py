"""The emdash upgrade check — run it after every emdash update.

Canopy couples to emdash across FOUR surfaces, and they fail in different ways:

    surface        how it breaks                          who noticed, before this
    -------------  -------------------------------------  ------------------------
    sqlite reads   silently (reads are fail-soft)          `verify-emdash`
    worktree paths silently (a miss returns None)          nobody
    DOM contracts  loudly, but only on the next real turn  nobody, until it mattered
    app version    not a failure — the reason to look      nobody

Only the first had a check. That asymmetry is why emdash 1.2 was able to change the
worktree layout and drop `conversations.last_interacted_at` in the same release and
have exactly one of the two get caught: the schema check named its missing column
immediately, while every session created on 1.2 resolved to no transcript at all and
said nothing about it — no error, no log line, just a `continue` on the streams and
backfills ticks, forever.

So this check asserts all four, and the ordering is deliberate: cheapest and most
diagnostic first, so a box with no emdash at all reports that rather than a cascade of
downstream failures caused by it.

**Every check here is READ-ONLY.** Nothing clicks, opens, selects a tab, or writes a
row. A check that perturbs the fleet is a check nobody runs on the fleet, and the one
time canopy read emdash's UI on a signal it stole focus from a human (#510). It is
therefore safe to run mid-turn, on a working box, as often as you like.

Exit codes are the same three `verify-emdash` always used: 0 intact, 1 drifted, 2 the
DB could not be read at all.
"""
from __future__ import annotations

import json
import plistlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import emdash

# Where macOS keeps the version string. Read from the app bundle rather than asked of
# a running emdash: the check must work with emdash closed, and — more usefully — the
# bundle is what a NEXT launch will run, so a replaced-but-not-restarted emdash reports
# the version the box is about to be on rather than the one it is still executing.
_INFO_PLIST = Path("/Applications/Emdash.app/Contents/Info.plist")

# The last version this check passed against, so a rerun can say "same version, still
# fine" vs "this is new, look at the report properly". Advisory only — never a gate.
_STAMP = Path.home() / ".canopy" / "emdash-verified.json"

# What a healthy `cdp_control.probe()` looks like. A contract is (key, minimum,
# required) — `required=False` means an absence is only meaningful when the UI is
# showing that part of itself, so it degrades to a NOTE rather than a drift.
#
# `claude_tabs` is deliberately non-required AND unbounded above: `send-keys` needs
# exactly one VISIBLE Claude tab in the open task, but the probe opens nothing, so
# what it counts here is whatever happens to be on screen.
CDP_CONTRACTS: tuple[tuple[str, int, bool], ...] = (
    ("open_task_labels", 1, False),   # no tasks in the sidebar is a legitimate state
    ("new_task_labels", 1, True),     # a project list is always rendered
    ("sidebar_scroller", 1, True),    # `.overflow-y-auto` — the virtualization scan
    ("xterm_any", 1, False),          # no terminal until a task is open
    ("xterm_rows", 1, False),
    ("terminal_input", 1, False),
    ("claude_tabs", 1, False),
)


@dataclass
class Check:
    """One surface's verdict. `notes` carry the evidence a human needs to act."""
    name: str
    ok: bool
    summary: str
    notes: list[str] = field(default_factory=list)
    skipped: bool = False


def installed_version() -> str | None:
    """emdash's `CFBundleShortVersionString`, or None if it isn't installed here."""
    try:
        with _INFO_PLIST.open("rb") as fh:
            return plistlib.load(fh).get("CFBundleShortVersionString")
    except (OSError, ValueError):
        return None


def _last_verified() -> str | None:
    try:
        return json.loads(_STAMP.read_text()).get("version")
    except (OSError, ValueError):
        return None


def record_verified(version: str | None) -> None:
    """Stamp a clean run. Best-effort: a read-only check must not fail because it
    could not write its own bookkeeping."""
    if not version:
        return
    try:
        _STAMP.parent.mkdir(parents=True, exist_ok=True)
        _STAMP.write_text(json.dumps({"version": version}))
    except OSError:
        pass


def check_version() -> Check:
    version = installed_version()
    if version is None:
        return Check("version", True, "emdash not installed at /Applications — skipping",
                     skipped=True)
    last = _last_verified()
    if last == version:
        return Check("version", True, f"emdash {version} (unchanged since the last clean check)")
    was = f"last verified against {last}" if last else "no previous check recorded"
    return Check("version", True, f"emdash {version} — NEW ({was})",
                 notes=["a version bump is the reason to read the rest of this report closely"])


def check_schema(db_path: str) -> Check:
    """The sqlite columns the reads name.

    Distinguishes a REQUIRED column going missing (a real drift, and the failure this
    check was built for) from a version-gated 1.2 column simply not being there yet —
    which is an older emdash, reported as a note. The reads tolerate both shapes, so
    calling the older one a failure would redden a healthy box for no action.
    """
    try:
        problems, older = emdash.check_read_schema(db_path)
    except emdash.SchemaCheckError as exc:
        return Check("db schema", False, str(exc))
    if problems:
        return Check(
            "db schema", False,
            "read schema drifted — the reads would SILENTLY degrade",
            notes=problems + [
                "fix: reconcile emdash.py's SQL against emdash's new schema, then "
                "update REQUIRED_SCHEMA/OPTIONAL_SCHEMA to match",
            ],
        )
    n = sum(len(c) for c in emdash.REQUIRED_SCHEMA.values())
    if older:
        return Check(
            "db schema", True,
            f"all {n} required columns present; this emdash predates 1.2",
            notes=older + ["the reads adapt to it — nothing to do unless you want 1.2's features"],
        )
    total = sum(len(c) for c in emdash.READ_SCHEMA.values())
    return Check("db schema", True,
                 f"all {total} columns across {', '.join(emdash.READ_SCHEMA)} present")


def check_transcripts(db_path: str, *, home: Path, claude_home: Path) -> Check:
    """Can we still find the transcript for the sessions emdash says are open?

    This is the check that did not exist, and the reason it now does. It asserts the
    worktree-path CONVENTION — which emdash owns, has changed twice, and signals in no
    way — against emdash's own list of open tasks. A miss here is invisible everywhere
    else: `resolve_emdash_transcript` returns None and its callers `continue`.

    A session with no transcript YET is not a miss, so the check does not demand 100%;
    it compares our answer against emdash's own `provider_session_id`, which is the
    ground truth for "a transcript exists at all". Sessions where emdash knows the file
    and we cannot find it are the drift, and they are named individually.
    """
    from canopy_transcript import resolve_emdash_transcript

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            # `provider_session_id` is the referee, and emdash only started recording
            # it in 1.2. Its absence means an OLDER emdash, which is a fine thing to be
            # running — so the check loses its ground truth and says so, rather than
            # reporting the older emdash as a drift. A check that goes red on a
            # perfectly healthy box is a check that gets muted.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
            if "provider_session_id" not in cols:
                return Check(
                    "transcripts", True,
                    "emdash predates provider_session_id — no ground truth to check "
                    "the worktree layout against, skipping",
                    skipped=True,
                )
            rows = conn.execute(
                """
                SELECT t.name AS task, COALESCE(p.name, '') AS repo,
                       cv.provider_session_id AS sid
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                LEFT JOIN conversations cv ON cv.task_id = t.id
                WHERE t.archived_at IS NULL AND t.type = 'task'
                """
            ).fetchall()
    except sqlite3.Error as exc:
        return Check("transcripts", False, f"could not list open sessions: {exc}")

    if not rows:
        return Check("transcripts", True, "no open sessions to check", skipped=True)

    misses: list[str] = []
    resolved = truth = 0
    for r in rows:
        ours = resolve_emdash_transcript(r["repo"], r["task"], home=home, claude_home=claude_home)
        # emdash 1.2 records the provider's own session id; a matching .jsonl anywhere
        # under ~/.claude/projects proves a transcript exists independently of any path
        # convention, which is exactly the neutral referee this check needs.
        emdash_knows = bool(r["sid"]) and any(claude_home.glob(f"*/{r['sid']}.jsonl"))
        resolved += bool(ours)
        truth += emdash_knows
        if emdash_knows and not ours:
            misses.append(f"{r['repo']}/{r['task']} — emdash has the transcript, our path convention does not find it")

    if misses:
        return Check(
            "transcripts", False,
            f"worktree layout drifted — {resolved}/{truth} resolvable transcripts found",
            notes=misses + [
                "fix: teach _worktree_bases()/parse_emdash_worktree() in "
                "canopy_transcript/paths.py the new layout",
            ],
        )
    return Check("transcripts", True,
                 f"{resolved}/{truth} resolvable transcripts found across {len(rows)} open sessions")


def check_cdp(*, port: int) -> Check:
    """The DOM contracts the CDP sidecar depends on. Read-only; nothing is clicked."""
    from . import cdp_control

    if not cdp_control.cdp_healthy(port=port):
        return Check("cdp dom", True,
                     f"emdash not reachable on CDP :{port} — skipping (start emdash to check)",
                     skipped=True)
    try:
        data = cdp_control.probe(port=port)
    except cdp_control.CDPError as exc:
        return Check("cdp dom", False, f"probe failed: {exc}")

    broken, notes = [], []
    for key, minimum, required in CDP_CONTRACTS:
        got = data.get(key, 0)
        if got >= minimum:
            continue
        if required:
            broken.append(f"{key}: found {got}, expected >= {minimum}")
        else:
            notes.append(f"{key}: 0 — not on screen right now, so not conclusive")
    if broken:
        return Check("cdp dom", False, "DOM contract drifted — the CDP path would fail on the next turn",
                     notes=broken + [
                         "fix: reconcile the selectors in cdp/emdash_control.mjs against emdash's new UI",
                     ])
    seen = ", ".join(f"{k}={data.get(k, 0)}" for k, _, _ in CDP_CONTRACTS)
    return Check("cdp dom", True, f"all required contracts resolve ({seen})", notes=notes)


def run(db_path: str, *, port: int, home: Path, claude_home: Path) -> tuple[list[Check], int]:
    """Every check, in order. Returns (checks, exit code)."""
    checks = [check_version(), check_schema(db_path)]
    # The schema check is the gate for the transcript one: both read the same DB, and
    # a DB we cannot open would report a second, derivative failure that tells a human
    # nothing they don't already know from the first.
    if checks[-1].ok:
        checks.append(check_transcripts(db_path, home=home, claude_home=claude_home))
    checks.append(check_cdp(port=port))

    failed = [c for c in checks if not c.ok]
    if not failed:
        record_verified(installed_version())
        return checks, 0
    # 2 is reserved for "could not read the DB at all" — a setup problem, not a drift.
    return checks, 2 if not checks[1].ok and "not found" in checks[1].summary else 1


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "-" if c.skipped else ("+" if c.ok else "x")
        lines.append(f"  {mark} {c.name}: {c.summary}")
        lines.extend(f"      {n}" for n in c.notes)
    return "\n".join(lines)
