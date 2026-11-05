"""The Session Context and Turn Context as JSON: what the context routes serve and `Turn.resolved_context` holds.

Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md` § 5. An allowlist of named fields, built from
the two read-models, neither of which holds the prompt, a transcript, the envelope's `person` block or a
credential's value (a credential line is a name and a kind), so nothing here can carry them. Tool lists, paths and
names are configuration, already readable to members. It only reads.
"""
from __future__ import annotations

#: Bump when a stored field's MEANING changes; adding a field does not need it.
SNAPSHOT_VERSION = 1

_PROFILE_FIELDS = ("kind", "name", "site", "description", "entry", "tools", "bash", "read_paths", "write_paths",
                   "data", "default_profile_version")


def _profile(profile) -> dict | None:
    return None if profile is None else {name: getattr(profile, name) for name in _PROFILE_FIELDS}


def turn_context(ctx) -> dict:
    standing, review, refusal = ctx.standing, ctx.review, ctx.refusal
    return {
        "standing": {"relationship": standing.relationship, "permissions": sorted(standing.permissions),
                     "agent_role": standing.agent_role, "source": standing.source, "via": standing.via},
        "access": ctx.access,
        "basis": ctx.basis,
        "profile": _profile(ctx.profile),
        "session_key_suffix": ctx.session_key_suffix,
        "credentials": [{"name": line.name, "kind": line.kind, "gets": line.gets, "why": line.why}
                        for line in ctx.credentials],
        "review": {"mode": review.mode, "basis": review.basis, "manual_only": review.manual_only,
                   "may_request_auto": review.may_request_auto, "may_pin_runner": review.may_pin_runner},
        "stamped": ctx.stamped,
        "refusal": None if refusal is None else {"code": refusal.code, "why": refusal.why,
                                                 "how_to_fix": refusal.how_to_fix, "who_can_fix": refusal.who_can_fix},
        "trace": [{"rule": entry.rule, "outcome": entry.outcome, "detail": entry.detail} for entry in ctx.trace],
    }


def snapshot(ctx, *, moment: str, at, runner=None, requested: str | None = None, admission: dict | None = None) -> dict:
    """What `Turn.resolved_context` holds: what the turn ran as, when that was decided, and what an admission rule
    did about it. `requested` is the profile a refused turn named, which nothing else keeps."""
    return {
        "version": SNAPSHOT_VERSION,
        "moment": moment,
        "recorded_at": at.isoformat(),
        "runner": None if runner is None else {"id": str(runner.pk), "name": runner.name, "kind": runner.kind},
        "requested": requested,
        "admission": admission,
        **turn_context(ctx),
    }


def _people(value):
    """`audience.pieces` holds sets, and a dict of them: as JSON, in a stable order."""
    if isinstance(value, dict):
        return {str(key): _people(item) for key, item in value.items()}
    return sorted(value) if isinstance(value, (set, frozenset)) else value


def session_context(ctx, *, sees_page: bool = True) -> dict:
    """`sees_page`: in a sensitive conversation the resource its page declared is its own people's, as the page
    itself is (`sensitivity.NOT_IN_IT_PAGE`), and every other fact here says nothing of what it holds."""
    return {
        "session_id": None if ctx.session_id is None else str(ctx.session_id),
        "agent": ctx.agent,
        "workspace": ctx.workspace,
        "channel": ctx.channel,
        "runner_requested": None if ctx.runner_requested is None else str(ctx.runner_requested),
        "runner_bound": None if ctx.runner_bound is None else str(ctx.runner_bound),
        "site": ctx.site,
        "page": ctx.page if sees_page else "",
        "sensitive": ctx.sensitive,
        "sensitive_requirement": ctx.sensitive_requirement,
        "is_review": ctx.is_review,
        "review_ask": None if ctx.review_ask is None else str(ctx.review_ask),
        "audience": _people(ctx.audience),
        "participants": [{"user_id": user_id, "role": role} for user_id, role in ctx.participants],
        "session_grants": [{"kind": g.kind, "user_id": g.user_id, "role": g.role, "source": g.source}
                           for g in ctx.session_grants],
        "send_blocks": [{"code": b.code, "why": b.why, "scope": b.scope} for b in ctx.send_blocks],
    }
