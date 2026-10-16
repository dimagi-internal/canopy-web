"""Decision rows on a storyboard — ace-web's decision-log pattern.

Every default a set of demos is built on is ONE row. Whether it reads as an
assumption, an open ask or a ruling follows from `status` and `review_ask`,
exactly as in ACE `docs/decisions-contract.md` and ace-web's DecisionsReview.

Request-free on purpose: the publish routes, the edit route, the round close
(plan C) and the data migration all call this module, so they cannot drift.
"""

from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

STATUSES = ("ai-default", "human-decided", "overridden", "deferred")
REVIEW_ASKS = ("recommended-confirmation", "required-before")
NEEDED_BY = ("external-share", "next-run")
EVIDENCE_BASES = ("stated", "inferred", "conflicting")

#: A person ruled on these; a publish may not move their value.
HUMAN_STATUSES = frozenset({"human-decided", "overridden"})


EDIT_ACTIONS = ("confirm", "pick", "write_in", "undo")
_MAX_OPTION_WORDS = 8
#: What a publisher may send. decided_* / confirmed / history / override_reasoning
#: belong to people and are set only by apply_edit.
_PUBLISHABLE = (
    "id",
    "scope",
    "question",
    "plain_question",
    "options",
    "recommended",
    "override",
    "reasoning",
    "source",
    "evidence_basis",
    "conflict_signals",
    "status",
    "revisit_when",
    "review_ask",
    "needed_by",
    "confirm_reason",
    "supersedes",
)
_PERSON_OWNED = (
    "decided_by",
    "decided_by_name",
    "decided_by_verified",
    "decided_at",
    "confirmed",
    "history",
    "override_reasoning",
)
#: What a history snapshot holds — enough to undo one edit.
_SNAPSHOT = (
    "status",
    "override",
    "override_reasoning",
    "confirmed",
    "options",
    "decided_by_id",
    "decided_by_name",
    "decided_by_verified",
    "decided_at",
)


#: A stored row's id / supersedes: lowercase kebab, as `key` is a SlugField.
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: StoryboardDecision column limits. A row over one passes SQLite (the test
#: database) and dies in Postgres as a DataError (500), so validate checks them.
_KEY_MAX = 120  # key, supersedes, superseded_by
_SCOPE_MAX = 200
_VALUE_MAX = 300  # recommended, override — and so every option a person may pick
_NAME_MAX = 200  # decided_by_name


class DecisionInvalid(ValueError):  # noqa: N818 - name fixed by the plan's interface
    """The row breaks the contract (422)."""


class DecisionConflict(Exception):  # noqa: N818 - name fixed by the plan's interface
    """The write would undo a person's ruling, or there is nothing to undo (409)."""


def value_of(row) -> str:
    return row.override or row.recommended


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def _clean_str(raw: dict, name: str) -> str:
    v = raw.get(name, "")
    if v is None:
        return ""
    if not isinstance(v, str):
        raise DecisionInvalid(f"{name} must be a string")
    return v.strip()


def _check_bounds(row: dict) -> None:
    """What the columns can hold. Only for rows that will be stored: a read-time
    narrative caveat is never saved and its id carries the narrative slug."""
    rid = row["id"]
    for name in ("id", "supersedes"):
        v = row[name]
        if len(v) > _KEY_MAX:
            raise DecisionInvalid(f"{rid[:_KEY_MAX]}: {name} is longer than {_KEY_MAX} characters")
        if v and not _ID_RE.match(v):
            raise DecisionInvalid(f"{rid}: {name} must be lowercase words joined by hyphens")
    if row["supersedes"] == rid:
        raise DecisionInvalid(f"{rid}: a row cannot supersede itself")
    for name, limit in (("scope", _SCOPE_MAX), ("recommended", _VALUE_MAX), ("override", _VALUE_MAX)):
        if len(row[name]) > limit:
            raise DecisionInvalid(f"{rid}: {name} is longer than {limit} characters")
    for o in row["options"]:
        if len(o) > _VALUE_MAX:
            raise DecisionInvalid(f"{rid}: an option is longer than {_VALUE_MAX} characters")


def validate(raw: dict, *, stored: bool = True) -> dict:
    """One publisher row, normalized. Raises DecisionInvalid naming the field.

    ``stored=False`` skips the column checks, for read-time rows that are only
    rendered, never saved."""
    unknown = set(raw) - set(_PUBLISHABLE)
    if unknown & set(_PERSON_OWNED):
        raise DecisionInvalid(
            f"{sorted(unknown & set(_PERSON_OWNED))} are set by the server from the signed-in "
            "person (decided_by …) — a publisher may not send them"
        )
    if unknown:
        raise DecisionInvalid(f"unknown decision fields: {sorted(unknown)}")
    row = {name: _clean_str(raw, name) for name in _PUBLISHABLE if name not in ("options", "conflict_signals")}
    if not row["id"]:
        raise DecisionInvalid("id is required")
    if not row["question"]:
        raise DecisionInvalid(f"{row['id']}: question is required")
    options = raw.get("options") or []
    signals = raw.get("conflict_signals") or []
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise DecisionInvalid(f"{row['id']}: options must be a list of strings")
    if not isinstance(signals, list) or not all(isinstance(s, str) for s in signals):
        raise DecisionInvalid(f"{row['id']}: conflict_signals must be a list of strings")
    row["options"] = [o.strip() for o in options if o.strip()]
    row["conflict_signals"] = [s.strip() for s in signals if s.strip()]
    for o in row["options"]:
        if len(o.split()) > _MAX_OPTION_WORDS:
            raise DecisionInvalid(f"{row['id']}: option {o!r} is longer than {_MAX_OPTION_WORDS} words")
    if row["options"] and row["recommended"] not in row["options"]:
        raise DecisionInvalid(f"{row['id']}: recommended must be one of options")
    if row["override"] and row["override"] not in row["options"]:
        raise DecisionInvalid(f"{row['id']}: override must be one of options")
    row["status"] = row["status"] or "ai-default"
    row["evidence_basis"] = row["evidence_basis"] or "inferred"
    for name, allowed in (("status", STATUSES), ("evidence_basis", EVIDENCE_BASES)):
        if row[name] not in allowed:
            raise DecisionInvalid(f"{row['id']}: {name} must be one of {list(allowed)}")
    if row["review_ask"] and row["review_ask"] not in REVIEW_ASKS:
        raise DecisionInvalid(f"{row['id']}: review_ask must be one of {list(REVIEW_ASKS)}")
    if row["needed_by"] and row["needed_by"] not in NEEDED_BY:
        raise DecisionInvalid(f"{row['id']}: needed_by must be one of {list(NEEDED_BY)}")
    if row["review_ask"] == "required-before" and not row["needed_by"]:
        raise DecisionInvalid(f"{row['id']}: required-before needs needed_by")
    if row["status"] == "deferred" and not row["revisit_when"]:
        raise DecisionInvalid(f"{row['id']}: a deferred row needs revisit_when")
    if row["status"] in HUMAN_STATUSES:
        raise DecisionInvalid(f"{row['id']}: only a person's edit can make a row {row['status']}")
    if stored:
        _check_bounds(row)
    return row


def publishable(row) -> dict:
    """A stored row back in publisher shape, so a brief-only write can carry the
    existing set through publish() unchanged."""
    return {
        "id": row.key,
        **{k: getattr(row, k) for k in _PUBLISHABLE if k != "id"},
        "status": row.status if row.status not in HUMAN_STATUSES else "ai-default",
        "recommended": value_of(row) if is_ruled(row) else row.recommended,
        "override": "",
    }


def is_ruled(row) -> bool:
    return row.status in HUMAN_STATUSES or row.confirmed


@transaction.atomic
def publish(board, rows: list[dict]):
    """Replace the board's decision set with ``rows`` (the publisher's list).

    A ruled row (a person decided or confirmed it) keeps its value, status,
    decider and history: the publisher may reword it but not move it, and may
    not drop it — it supersedes it instead. Anything else fails the WHOLE
    publish (atomic), so a board is never half-updated.
    """
    from apps.storyboards.models import StoryboardDecision

    clean = [validate(r) for r in rows]
    seen: set[str] = set()
    for r in clean:
        if r["id"] in seen:
            raise DecisionInvalid(f"duplicate decision id {r['id']!r}")
        seen.add(r["id"])
    existing = {d.key: d for d in StoryboardDecision.objects.select_for_update().filter(storyboard=board)}
    superseded = {r["supersedes"]: r["id"] for r in clean if r["supersedes"]}

    for key, old in existing.items():
        if key not in seen and is_ruled(old) and key not in superseded:
            raise DecisionConflict(f"{key}: a person ruled on this row; supersede it instead of dropping it")

    out = []
    for pos, r in enumerate(clean):
        old = existing.get(r["id"])
        incoming_value = r["override"] or r["recommended"]
        if old is not None and is_ruled(old):
            if incoming_value != value_of(old) and r["id"] not in superseded:
                raise DecisionConflict(
                    f"{r['id']}: a person ruled {value_of(old)!r}; a publish may not change it "
                    "(supersede the row and say why)"
                )
            for name in (
                "scope",
                "question",
                "plain_question",
                "reasoning",
                "source",
                "evidence_basis",
                "conflict_signals",
                "review_ask",
                "needed_by",
                "confirm_reason",
                "revisit_when",
                "supersedes",
            ):
                setattr(old, name, r[name])
            old.options = old.options + [o for o in r["options"] if o not in old.options]
            old.position = pos
            old.superseded_by = superseded.get(r["id"], "")
            old.save()
            out.append(old)
            continue
        fields = {k: v for k, v in r.items() if k != "id"}
        fields["superseded_by"] = superseded.get(r["id"], "")
        row, _ = StoryboardDecision.objects.update_or_create(
            storyboard=board, key=r["id"], defaults={**fields, "position": pos}
        )
        out.append(row)
    for key, old in existing.items():
        if key in superseded:
            old.superseded_by = superseded[key]
            old.save(update_fields=["superseded_by", "updated_at"])
        elif key not in seen:
            old.delete()
    return out


def _snapshot(row) -> dict:
    snap = {name: getattr(row, name) for name in _SNAPSHOT}
    if snap["decided_at"] is not None:
        snap["decided_at"] = snap["decided_at"].isoformat()
    snap["at"] = timezone.now().isoformat()
    return snap


def _restore(row, snap: dict) -> None:
    from django.utils.dateparse import parse_datetime

    for name in _SNAPSHOT:
        value = snap.get(name)
        if name == "decided_at" and value:
            value = parse_datetime(value)
        setattr(row, name, value)


def public_name(user) -> str:
    """A member's name as other readers may see it — never an email address."""
    for candidate in (user.get_full_name(), user.first_name, user.username):
        candidate = (candidate or "").strip()
        if candidate and "@" not in candidate:
            return candidate
    local = (user.email or "").split("@", 1)[0].strip()
    return local or "A teammate"


def _person_name(user, by_name: str) -> str:
    if user is not None and getattr(user, "is_authenticated", False):
        return public_name(user)
    return (by_name or "").strip()


def check_edit(row, *, action: str, value: str = "") -> None:
    """Validate an edit that will be applied later (staged in a round)."""
    if action not in EDIT_ACTIONS:
        raise DecisionInvalid(f"action must be one of {list(EDIT_ACTIONS)}")
    if action == "undo":
        raise DecisionConflict("undo is available after the round closes")
    value = (value or "").strip()
    if action == "pick" and value not in row.options:
        raise DecisionInvalid(f"{value!r} is not one of this decision's options")
    if action == "write_in":
        if not value:
            raise DecisionInvalid("a write-in needs a value")
        if len(value.split()) > _MAX_OPTION_WORDS:
            raise DecisionInvalid(f"a write-in is at most {_MAX_OPTION_WORDS} words")


def apply_edit(row, *, action: str, value: str = "", reasoning: str = "", by_user=None, by_name: str = ""):
    """A person's edit: confirm / pick / write_in / undo. Attributed and undoable.

    Re-reads the row under a lock so two tabs editing at once both land, in
    order, with the second's history holding the first.
    """
    from apps.storyboards.models import StoryboardDecision

    if action not in EDIT_ACTIONS:
        raise DecisionInvalid(f"action must be one of {list(EDIT_ACTIONS)}")
    user = by_user if getattr(by_user, "is_authenticated", False) else None
    name = _person_name(user, by_name)
    if action != "undo" and not name:
        raise DecisionInvalid("an edit needs a name")

    with transaction.atomic():
        row = StoryboardDecision.objects.select_for_update().get(pk=row.pk)
        if row.superseded_by:
            raise DecisionConflict(f"{row.key}: this row was replaced by {row.superseded_by}; edit that one")
        if action == "undo":
            if not row.history:
                raise DecisionConflict(f"{row.key}: nothing to undo")
            snap, *rest = row.history
            _restore(row, snap)
            row.history = rest
            row.save()
            return row

        before = _snapshot(row)
        value = (value or "").strip()
        if action == "write_in":
            if not value:
                raise DecisionInvalid("a write-in needs a value")
            match = next((o for o in row.options if _norm(o) == _norm(value)), None)
            if match is None:
                if len(value.split()) > _MAX_OPTION_WORDS:
                    raise DecisionInvalid(f"a write-in is at most {_MAX_OPTION_WORDS} words")
                if len(value) > _VALUE_MAX:
                    raise DecisionInvalid(f"a write-in is at most {_VALUE_MAX} characters")
                row.options = [*row.options, value]
                match = value
            value, action = match, "pick"
        if action == "pick":
            if value not in row.options:
                raise DecisionInvalid(f"{value!r} is not one of this decision's options")
            if len(value) > _VALUE_MAX:
                raise DecisionInvalid(f"that option is longer than {_VALUE_MAX} characters; write in a shorter one")
            if value == row.recommended:
                action = "confirm"
        if action == "confirm":
            row.override = ""
            row.confirmed = True
            row.status = "human-decided"
        else:
            row.override = value
            row.confirmed = False
            row.status = "overridden"
        row.override_reasoning = (reasoning or "").strip()
        row.decided_by = user
        row.decided_by_name = name[:_NAME_MAX]
        row.decided_by_verified = user is not None
        row.decided_at = timezone.now()
        row.history = [before, *row.history]
        row.save()
        return row


def serialize(row, *, editable: bool = True, pending: list[dict] | None = None) -> dict:
    """The row as the page and the publisher read it. Names, never user ids."""
    return {
        "id": row.key,
        "scope": row.scope,
        "question": row.question,
        "plain_question": row.plain_question,
        "options": list(row.options),
        "recommended": row.recommended,
        "override": row.override,
        "value": value_of(row),
        "reasoning": row.reasoning,
        "override_reasoning": row.override_reasoning,
        "source": row.source,
        "evidence_basis": row.evidence_basis,
        "conflict_signals": list(row.conflict_signals),
        "status": row.status,
        "revisit_when": row.revisit_when,
        "review_ask": row.review_ask,
        "needed_by": row.needed_by,
        "confirm_reason": row.confirm_reason,
        "decided_by": row.decided_by_name,
        "decided_by_verified": row.decided_by_verified,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "confirmed": row.confirmed,
        "history": [
            {
                "status": h.get("status", ""),
                "value": h.get("override") or row.recommended,
                "decided_by": h.get("decided_by_name", ""),
                "at": h.get("at", ""),
            }
            for h in row.history
        ],
        "supersedes": row.supersedes,
        "superseded_by": row.superseded_by,
        "editable": editable,
        "pending": list(pending or []),
    }


LEGACY_BRIEF_KEYS = ("assumptions", "divergences", "open_questions")


_OPTION_MAX_CHARS = 120


def _key(prefix: str, text: str, taken: set[str]) -> str:
    # slugify keeps underscores and a cut can end on a hyphen; neither passes
    # _ID_RE, so fold underscores in and strip hyphens after every cut.
    words = re.sub(r"[-_]+", "-", slugify(text)).strip("-")[:60].strip("-")
    base = (f"{prefix}-{words}" if words else prefix)[:_KEY_MAX].rstrip("-")
    key, n = base, 2
    while key in taken:
        suffix = f"-{n}"
        key, n = f"{base[: _KEY_MAX - len(suffix)].rstrip('-')}{suffix}", n + 1
    taken.add(key)
    return key


def _short_option(text: str) -> list[str]:
    """A statement is its own single option when it fits the 1-8 word rule and
    a sane length; otherwise it carries no options (an over-long label would
    fail validate or overflow the column, and a migration must not fail on
    production prose)."""
    ok = 0 < len(text.split()) <= _MAX_OPTION_WORDS and len(text) <= _OPTION_MAX_CHARS
    return [text] if ok else []


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _texts(items) -> list[str]:
    # A bare string is one item, never iterated per character.
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return []
    return [t.strip() for t in items if isinstance(t, str) and t.strip()]


def _divergences(items) -> list[dict]:
    if isinstance(items, (str, dict)):
        items = [items]
    if not isinstance(items, list):
        return []
    out = []
    for d in items:
        if isinstance(d, str):
            d = {"script": d}
        if not isinstance(d, dict):
            continue
        script, data, resolution = _s(d.get("script")), _s(d.get("data")), _s(d.get("resolution"))
        # Nothing is dropped for lack of a script: derive it from what is there.
        head = script or data or resolution
        if not head:
            continue
        out.append(
            {
                "head": head,
                "script": script,
                "data": data,
                "resolution": resolution,
                "scope": _s(d.get("narrative_slug"))[:_SCOPE_MAX],
            }
        )
    return out


def from_legacy_brief(brief: dict, *, taken: set[str] | None = None, prefix: str = "") -> list[dict]:
    """The old brief lists (#1501) as decision rows (spec B, "Migration from the brief").

    Total over malformed input: it never raises and never drops content it was
    given (the migration strips the lists afterwards)."""
    taken = set() if taken is None else taken
    brief = brief if isinstance(brief, dict) else {}
    rows: list[dict] = []
    for text in _texts(brief.get("assumptions")):
        opts = _short_option(text)
        rows.append(
            {
                "id": _key(f"{prefix}assumption", text, taken),
                "question": text,
                "plain_question": text,
                "options": opts,
                "recommended": opts[0] if opts else "",
                "status": "ai-default",
                "evidence_basis": "inferred",
                "review_ask": "",
            }
        )
    for d in _divergences(brief.get("divergences")):
        script, data, resolution = d["script"], d["data"], d["resolution"]
        opts = _short_option(resolution)
        parts = []
        if script:
            parts.append(f"The script says: {script}.")
        if data:
            parts.append(f"The data shows: {data}.")
        if not parts:
            parts.append(d["head"])
        rows.append(
            {
                "id": _key(f"{prefix}divergence", d["head"], taken),
                "scope": d["scope"],
                "question": " ".join(parts),
                "plain_question": f"The script and the data disagree: {d['head']}",
                "options": opts,
                # Spec: a divergence's recommended is its resolution. Too long
                # for an option label, it is still the recommendation (free text
                # is allowed when options is empty), cut to the column.
                "recommended": opts[0] if opts else resolution[:_VALUE_MAX],
                "reasoning": resolution if not opts else "",
                "evidence_basis": "conflicting",
                "conflict_signals": [
                    s for s in (f"Script: {script}" if script else "", f"Data: {data}" if data else "") if s
                ],
                "status": "ai-default",
                "review_ask": "recommended-confirmation",
            }
        )
    for text in _texts(brief.get("open_questions")):
        rows.append(
            {
                "id": _key(f"{prefix}question", text, taken),
                "question": text,
                "plain_question": text,
                "options": [],
                "recommended": "",
                "status": "ai-default",
                "evidence_basis": "inferred",
                "review_ask": "recommended-confirmation",
            }
        )
    return rows


def from_narrative_caveats(caveats: dict, narrative_slug: str) -> list[dict]:
    """A narrative version's own caveats (request_json), read-time only, scoped
    to that narrative. Not stored: rendered read-only until the publisher sends
    decisions[] instead (spec B)."""
    rows = from_legacy_brief(caveats, prefix=f"{narrative_slug}--")
    for r in rows:
        r["scope"] = narrative_slug
    return rows
