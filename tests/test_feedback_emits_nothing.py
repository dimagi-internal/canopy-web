"""apps.feedback emits no signal. This is a design decision, not an oversight.

Feedback is INPUT TO A DECISION. The owner fires a turn when ready; the turn
reads the pool, clusters it across channels, and proposes a disposition for each
piece. Auto-promoting each one into an `Item` would rebuild the queue-grooming
step the inbox redesign deliberately removed — and would mean an external
reviewer could enqueue work on an agent directly.

If you are here because you added a notification and this failed: the
notification is the thing to remove. Ask for a signal on the READ side (a turn
that polls) rather than the write side.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "feedback"

# Tokens that would turn a write into work. `on_commit` is included because the
# push app uses exactly that shape to coalesce and send — the pattern to avoid.
BANNED = (
    "post_save",
    "post_delete",
    "pre_save",
    "on_commit",
    "receiver",
    "Item(",
    "Item.objects",
)


def test_the_app_has_no_signals_module():
    assert not (APP / "signals.py").exists(), (
        "apps/feedback must not emit signals — feedback is input to a decision, "
        "not work. See the module docstring in apps/feedback/models.py."
    )


def test_no_module_connects_a_receiver_or_creates_an_item():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text()
        for token in BANNED:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert offenders == [], f"apps/feedback grew a side effect: {offenders}"


def test_the_app_config_does_not_import_signals_on_ready():
    """The usual way a signal sneaks in: AppConfig.ready() importing signals."""
    text = (APP / "apps.py").read_text()
    assert "def ready" not in text, (
        "apps/feedback's AppConfig has a ready() hook — the standard place a "
        "signal receiver gets registered. Feedback must stay inert."
    )
