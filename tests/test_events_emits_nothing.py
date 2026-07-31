"""apps.events emits no signal. This is a design decision, not an oversight.

An event is a RECORD OF SOMETHING THAT HAPPENED, not work. Hal sweeps the pool
on a turn and decides what deserves acting on.

Auto-promoting each fault into an `Item` would be wrong twice over. `Item`'s
closed decision set (`implement`/`skip`/`defer`) does not describe "transcript
flush failed 400 times" — there is nothing to decide, only something to look
at. And `Item` count increases drive Web Push (apps/push/signals.py), so a
flapping runner would turn a log line into a notification storm on someone's
phone.

If you are here because you added a promotion rule and this failed: that rule
belongs on the READ side (a turn that queries the pool and raises Items for
what it judges significant), not on the write side. The write side must stay
cheap enough that logging is never a decision.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "apps" / "events"

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
        "apps/events must not emit signals — an event is a record, not work. "
        "See the module docstring in apps/events/models.py."
    )


def test_no_module_connects_a_receiver_or_creates_an_item():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text()
        for token in BANNED:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert offenders == [], f"apps/events grew a side effect: {offenders}"


def test_the_app_config_does_not_import_signals_on_ready():
    """The usual way a signal sneaks in: AppConfig.ready() importing signals."""
    text = (APP / "apps.py").read_text()
    assert "def ready" not in text, (
        "apps/events' AppConfig has a ready() hook — the standard place a signal "
        "receiver gets registered. The log must stay inert."
    )
