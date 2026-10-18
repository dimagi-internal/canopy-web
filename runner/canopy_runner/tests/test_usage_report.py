"""The laptop's usage report rides the sweep: scope every minute, scan when due or asked."""
import json
from types import SimpleNamespace

from canopy_runner import usage_report


class FakeClient:
    def __init__(self, full=False):
        self.full, self.posts, self.scopes = full, [], 0

    def usage_scope(self, runner_id):
        self.scopes += 1
        return {"repos": ["hal"], "full": self.full}

    def post_usage(self, runner_id, sessions, *, full=False):
        self.posts.append((len(sessions), full))


def _transcript(home):
    d = home / ".claude" / "projects" / "-x-hal"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "cwd": "/x/hal", "message": {"content": "go"}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z",
         "message": {"id": "m1", "model": "m", "content": "ok", "usage": {"output_tokens": 5}}}]))


def test_reports_when_due_and_at_once_when_a_rescan_is_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_report, "_state", {"scope_at": 0.0, "scan_at": 0.0})
    _transcript(tmp_path)
    cfg, client, t = SimpleNamespace(runner_id="r1"), FakeClient(), [1000.0]
    assert usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    assert client.posts == [(1, False)]
    t[0] += 30  # before the next scope read: nothing asked
    assert not usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    t[0] += 60  # scope read, but not due and not asked
    assert not usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    client.full = True
    t[0] += 61
    assert usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    assert client.posts[-1] == (1, True)
