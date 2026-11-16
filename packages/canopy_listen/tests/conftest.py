import pytest


@pytest.fixture(autouse=True)
def _no_caffeinate(monkeypatch):
    """Tests record for real but must not hold the Mac awake: the holder is recorded instead."""
    from canopy_listen import recorder

    held = []
    monkeypatch.setattr(recorder, "keep_awake", lambda pid: held.append(pid) or _Holder(held))
    return held


class _Holder:
    def __init__(self, held):
        self.held = held

    def terminate(self):
        self.held.append("released")
