"""Who a turn is FROM — the routing key that source rules cannot express.

Pure, no DB. The two shapes this exists to get right, both observed in
production and both of which the obvious implementation gets wrong:

  - an `email` turn's `enqueued_by` is the RUNNER's account (the inbox watcher
    POSTs as the box's paired user), so the sender lives in `origin_ref["from"]`;
  - a `canopy_web_chat` / `ace_web` turn has no `origin_ref["from"]` at all, so
    its actor is `enqueued_by`.

Spec: docs/superpowers/specs/2026-09-05-actor-aware-runner-routing-design.md
"""
from __future__ import annotations

import pytest

from apps.harness.actors import resolve_actor
from apps.harness.models import Turn


def test_an_email_turns_actor_is_the_sender_not_the_enqueuer():
    """The bug this module exists to prevent.

    Every email turn in production carries `enqueued_by=jjackson@dimagi.com` —
    the account the runner's inbox watcher authenticates as — regardless of who
    wrote in. Keying on it collapses every correspondent onto one rule.
    """
    actor = resolve_actor(
        Turn.ORIGIN_EMAIL,
        {"from": "Beth Geoffroy <egeoffroy@dimagi.com>"},
        "jjackson@dimagi.com",
    )
    assert actor == "egeoffroy@dimagi.com"


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Jonathan Jackson <jjackson@dimagi.com>", "jjackson@dimagi.com"),
        ("stewari@dimagi.com", "stewari@dimagi.com"),
        # A quoted display name containing a comma — real, and the shape that
        # breaks any hand-rolled split on '<' or ','.
        ('"Anthropic, PBC" <invoice+statements@mail.anthropic.com>',
         "invoice+statements@mail.anthropic.com"),
        ("Labs Alerts <no-reply@sns.amazonaws.com>", "no-reply@sns.amazonaws.com"),
        ("  Neal Lesh  <NLesh@Dimagi.com>  ", "nlesh@dimagi.com"),
    ],
)
def test_email_headers_resolve_to_a_bare_lowercase_address(header, expected):
    assert resolve_actor(Turn.ORIGIN_EMAIL, {"from": header}, "") == expected


@pytest.mark.parametrize(
    "origin",
    [Turn.ORIGIN_ACE_WEB, Turn.ORIGIN_CANOPY_WEB_CHAT, Turn.ORIGIN_API, Turn.ORIGIN_SLACK],
)
def test_non_email_origins_use_the_enqueuer(origin):
    assert resolve_actor(origin, {}, "Sarvesh@Dimagi.com") == "sarvesh@dimagi.com"


def test_a_non_email_origin_ignores_a_from_in_origin_ref():
    """`from` is the email producer's field. A chat turn that somehow carries one
    must not be routed by it — the enqueuer is the authenticated caller."""
    assert resolve_actor(
        Turn.ORIGIN_ACE_WEB, {"from": "someone@else.com"}, "real@dimagi.com"
    ) == "real@dimagi.com"


def test_a_scheduled_turn_has_no_actor():
    """A schedule has no live human. It must fall through to the source rule
    rather than match some rule by accident."""
    assert resolve_actor(
        Turn.ORIGIN_CANOPY_SCHEDULER, {"schedule_id": 7, "slot": "..."}, ""
    ) == ""


@pytest.mark.parametrize(
    "origin,ref,enq",
    [
        (Turn.ORIGIN_EMAIL, {}, ""),                       # no header at all
        (Turn.ORIGIN_EMAIL, {"from": ""}, ""),             # empty header
        (Turn.ORIGIN_EMAIL, {"from": "not an address"}, ""),  # unparseable
        (Turn.ORIGIN_EMAIL, {"from": None}, ""),           # null header
        (Turn.ORIGIN_API, {}, ""),                         # unauthenticated enqueue
        (Turn.ORIGIN_API, {}, None),                       # no enqueued_by row
    ],
)
def test_an_unresolvable_actor_is_empty_never_a_guess(origin, ref, enq):
    """Empty means "matches no actor rule, falls through" — which is the safe
    outcome. Returning a partial or made-up value would silently route."""
    assert resolve_actor(origin, ref, enq) == ""


def test_origin_ref_that_is_not_a_dict_does_not_raise():
    """`origin_ref` is a JSONField; a malformed row must not take down claiming."""
    assert resolve_actor(Turn.ORIGIN_EMAIL, None, "") == ""
    assert resolve_actor(Turn.ORIGIN_EMAIL, "garbage", "") == ""
