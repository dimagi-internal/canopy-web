"""The default model lives in ONE place, and is read at call time.

It used to be written out four times as a default argument — `_api_call`,
`_api_stream`, `call_ai`, `stream_message` — which is precisely how it came to be
a generation stale: moving it meant finding every copy, so nobody did.

The call-time read is the other half, and this repo has paid for getting it wrong
before: `DEFAULT_TIMEOUT_SECONDS` was a default argument, bound once at import,
so the test that tried to shrink it waited the full twenty seconds anyway.
"""

from django.test import override_settings

from apps.common import anthropic_client as ac


def test_there_is_one_default_and_it_is_current():
    assert ac.DEFAULT_MODEL == "claude-sonnet-5"


def test_the_default_is_not_duplicated_as_a_default_argument():
    """A second copy would silently diverge — which is the bug being fixed."""
    import inspect

    for fn in (ac._api_call, ac._api_stream, ac.call_ai, ac.stream_message):
        default = inspect.signature(fn).parameters["model"].default
        assert default is None, f"{fn.__name__} pins a model in its signature"


@override_settings(ANTHROPIC_MODEL="claude-opus-5")
def test_a_deployment_can_override_without_a_code_change():
    assert ac._default_model() == "claude-opus-5"


def test_an_override_is_read_at_call_time_not_captured_at_import():
    """The failure mode this guards: a module-level capture ignores every
    override set after import, which is every override."""
    with override_settings(ANTHROPIC_MODEL="claude-haiku-4-5-20251001"):
        assert ac._default_model() == "claude-haiku-4-5-20251001"
    # And it goes back when the override lifts, rather than sticking.
    assert ac._default_model() == ac.DEFAULT_MODEL


@override_settings(ANTHROPIC_MODEL="")
def test_an_empty_override_falls_back_rather_than_asking_for_a_nameless_model():
    assert ac._default_model() == ac.DEFAULT_MODEL


def test_an_explicit_caller_argument_still_wins():
    """The per-call argument is what it is for."""
    import inspect

    src = inspect.getsource(ac._api_call)
    assert "model or _default_model()" in src
