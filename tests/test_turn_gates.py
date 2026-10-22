"""The enqueue gates (apps/harness/turn_gates.py), each on its own.

The route tests (tests/test_harness_api.py and friends) cover them end to end;
these pin each gate's refusal without a request through Ninja."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from ninja.errors import HttpError

from apps.harness import initiator as who
from apps.harness import turn_gates
from apps.harness.models import Turn


def _payload(**kw):
    base = dict(agent_slug="echo", project="", origin=Turn.ORIGIN_API, routing=Turn.PREFER_LOCAL,
                origin_ref={}, acts_as=None, runner_id=None, turn_mode=None)
    base.update(kw)
    return SimpleNamespace(**base)


class _Agent:
    slug = "echo"

    def __init__(self, admin: bool):
        self._admin = admin

    def is_admin(self, user):
        return self._admin


@pytest.mark.parametrize("kw,detail", [
    (dict(agent_slug="echo", project="repo"), "a turn targets an agent_slug XOR a project"),
    (dict(agent_slug="", project=""), "a turn targets an agent_slug XOR a project"),
    (dict(origin="carrier-pigeon"), "unknown origin 'carrier-pigeon'"),
    (dict(routing="sideways"), "unknown routing 'sideways'"),
])
def test_check_shape_refuses_a_malformed_turn(kw, detail):
    with pytest.raises(HttpError) as exc:
        turn_gates.check_shape(_payload(**kw))
    assert exc.value.status_code == 422
    assert str(exc.value) == detail


def test_check_shape_passes_a_well_formed_turn():
    turn_gates.check_shape(_payload())


def test_capped_origin_ref_drops_a_client_asserted_cap():
    request = SimpleNamespace(user=SimpleNamespace(is_authenticated=True))
    ref = turn_gates.capped_origin_ref(request, _payload(origin_ref={"acts_as": "owner", "k": 1}))
    assert ref == {"k": 1}


@pytest.mark.parametrize("agent", [None, _Agent(admin=False)])
def test_only_an_agent_admin_posts_an_email_turn(agent):
    request = SimpleNamespace(user=object())
    with pytest.raises(HttpError) as exc:
        turn_gates.check_email_poster(request, _payload(origin=Turn.ORIGIN_EMAIL), agent)
    assert exc.value.status_code == 403


def test_an_admin_posts_an_email_turn_and_other_origins_are_not_asked():
    request = SimpleNamespace(user=object())
    turn_gates.check_email_poster(request, _payload(origin=Turn.ORIGIN_EMAIL), _Agent(admin=True))
    turn_gates.check_email_poster(request, _payload(), None)


def test_no_pin_is_no_runner():
    assert turn_gates.resolve_pin(SimpleNamespace(user=object()), _payload(), None) is None


def test_turn_mode_is_refused_on_a_project_or_email_turn():
    request = SimpleNamespace(user=object())
    with pytest.raises(HttpError) as exc:
        turn_gates.check_requested_turn_mode(request, _payload(turn_mode="manual"), None, None)
    assert exc.value.status_code == 422
    with pytest.raises(HttpError) as exc:
        turn_gates.check_requested_turn_mode(
            request, _payload(origin=Turn.ORIGIN_EMAIL, turn_mode="manual"), _Agent(admin=True), None)
    assert exc.value.status_code == 422


def test_auto_needs_a_verified_credential():
    request = SimpleNamespace(user=object())
    widget = SimpleNamespace(kind=who.USER, assurance="delegated")
    with pytest.raises(HttpError) as exc:
        turn_gates.check_requested_turn_mode(request, _payload(turn_mode="auto"), _Agent(admin=True), widget)
    assert exc.value.status_code == 403


def test_manual_is_open_to_whoever_may_enqueue():
    turn_gates.check_requested_turn_mode(
        SimpleNamespace(user=object()), _payload(turn_mode="manual"), _Agent(admin=False), None)
