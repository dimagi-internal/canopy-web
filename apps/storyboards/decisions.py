"""Decision rows on a storyboard — ace-web's decision-log pattern.

Every default a set of demos is built on is ONE row. Whether it reads as an
assumption, an open ask or a ruling follows from `status` and `review_ask`,
exactly as in ACE `docs/decisions-contract.md` and ace-web's DecisionsReview.

Request-free on purpose: the publish routes, the edit route, the round close
(plan C) and the data migration all call this module, so they cannot drift.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

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


def validate(raw: dict) -> dict:
    """One publisher row, normalized. Raises DecisionInvalid naming the field."""
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
    return row


def _is_ruled(row) -> bool:
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
        if key not in seen and _is_ruled(old) and key not in superseded:
            raise DecisionConflict(f"{key}: a person ruled on this row; supersede it instead of dropping it")

    out = []
    for pos, r in enumerate(clean):
        old = existing.get(r["id"])
        incoming_value = r["override"] or r["recommended"]
        if old is not None and _is_ruled(old):
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


def _person_name(user, by_name: str) -> str:
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_full_name() or user.first_name or user.email or user.username
    return (by_name or "").strip()


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
                row.options = [*row.options, value]
                match = value
            value, action = match, "pick"
        if action == "pick":
            if value not in row.options:
                raise DecisionInvalid(f"{value!r} is not one of this decision's options")
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
        row.decided_by_name = name
        row.decided_by_verified = user is not None
        row.decided_at = timezone.now()
        row.history = [before, *row.history]
        row.save()
        return row


def serialize(row, *, editable: bool = True) -> dict:
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
    }
