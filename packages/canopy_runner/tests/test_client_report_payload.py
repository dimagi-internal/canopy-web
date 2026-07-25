"""Wire-shape of the session report. Pinned against the server contract in
apps/harness/schemas.py::ReportSessionsIn."""
from canopy_runner.client import Client


def _client(monkeypatch):
    """A Client whose transport is replaced by a recorder — no socket needed."""
    c = Client(base_url="http://x", token="tok")
    sent = []

    def _call(method, path, body=None):
        sent.append({"method": method, "path": path, "body": body})
        return 200, {}

    monkeypatch.setattr(c, "_call", _call)
    return c, sent


def test_report_sessions_carries_the_archived_list(monkeypatch):
    c, sent = _client(monkeypatch)
    c.report_sessions("r1", [{"emdash_task": "a"}], ["gone", "also-gone"])
    assert sent[-1]["path"] == "/runners/r1/sessions"
    assert sent[-1]["body"] == {
        "sessions": [{"emdash_task": "a"}],
        "archived": ["gone", "also-gone"],
    }


def test_report_sessions_defaults_archived_to_empty(monkeypatch):
    """An empty list, never a missing key — the server must be able to tell 'nothing
    was archived' from an older runner that cannot report it."""
    c, sent = _client(monkeypatch)
    c.report_sessions("r1", [])
    assert sent[-1]["body"] == {"sessions": [], "archived": []}
