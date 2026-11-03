"""Origin and routing vocabulary shared by the harness request schemas."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import WithJsonSchema

from ..models import Turn

# Kept in lockstep with Turn.ORIGIN_CHOICES / Turn.ROUTING_CHOICES (models.py).
# These are the values the DB columns accept (origin max_length=32, routing
# max_length=15); typing the INPUT schemas as Literals turns an out-of-set value
# into a 422 at the API boundary instead of a Postgres "value too long" 500 that
# SQLite CI can't reproduce. Output schemas stay `str` — they serialize values the
# DB already validated, and a Literal there would break on any legacy row.
#
# POST-able sources only. `canopy_web_chat` / `canopy_scheduler` are server-set
# (one in-repo producer each) — a caller spelling them would borrow that source's
# routing rule. Retired spellings are normalized, not rejected: the live fleet
# posts `cron`/`manual` today and 422'ing them would break Echo/Ada mid-flight.
# But they are NOT advertised: the published schema (OpenAPI, so every MCP tool)
# lists only what a caller should send. Advertising them led agents to pick
# `manual`, which is then silently stored as `api`, so a routing rule written
# for "manual" turns could never match.
_POSTABLE_ORIGINS = ("api", "ace_web", "email", "slack")  # == Turn.POSTABLE_ORIGINS (tests/test_origin_vocabulary.py)


Origin = Annotated[
    Literal[
        "api", "ace_web", "email", "slack",           # postable
        "board", "cron", "manual", "drill",           # legacy, normalized below
    ],
    WithJsonSchema({"type": "string", "enum": list(_POSTABLE_ORIGINS)}),
]


# What a CHAT send may name (`Turn.CHAT_SEND_ORIGINS`, enforced by `chat_send_origin`).
ChatSendOrigin = Annotated[
    Literal["api", "ace_web", "board", "manual", "drill"],  # legacy spellings, normalized to api
    WithJsonSchema({"type": "string", "enum": ["api", "ace_web"]}),  # == Turn.CHAT_SEND_ORIGINS
]


# What a per-agent routing rule may name — the full vocabulary, including the
# server-only sources (a rule NAMES a source, it does not produce one).
RoutableSource = Literal[
    "ace_web", "email", "canopy_scheduler", "canopy_web_chat", "slack", "api",
]


Routing = Literal["prefer_local", "local_only", "any"]


def normalize_origin(value: str) -> str:
    """Map a retired spelling onto its replacement. Shared by every input schema
    carrying an `origin`, so the boundary can't normalize inconsistently.

    `enqueue_turn` applies the SAME mapping, deliberately: TurnSpec.from_dict
    parses stored Item JSON with no schema in the path, so a request-boundary-only
    rule would miss every Item raised before this shipped.
    """
    return Turn.LEGACY_ORIGIN_ALIASES.get(value, value)


def chat_send_origin(value: str | None) -> str | None:
    """`normalize_origin`, then refuse a source a chat send may not name
    (`Turn.CHAT_SEND_ORIGINS`) — shared by every chat door's input schema."""
    if not value:
        return value
    value = normalize_origin(value)
    if value not in Turn.CHAT_SEND_ORIGINS:
        raise ValueError(f"origin {value!r} is not one a chat send may name "
                         f"(one of {', '.join(sorted(Turn.CHAT_SEND_ORIGINS))})")
    return value
