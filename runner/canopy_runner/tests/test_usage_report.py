"""The laptop's usage report rides the sweep: it scans when due, and retries a failure soon."""
import json
from types import SimpleNamespace

from canopy_runner import usage_report


class FakeClient:
    def __init__(self):
        self.posts, self.scopes, self.down = [], 0, False

    def usage_scope(self, runner_id):
        self.scopes += 1
        if self.down:
            raise ConnectionError("down")
        return {"repos": ["hal"]}

    def post_usage(self, runner_id, sessions):
        self.posts.append(len(sessions))


def _transcript(home):
    d = home / ".claude" / "projects" / "-x-hal"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "cwd": "/x/hal", "message": {"content": "go"}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z",
         "message": {"id": "m1", "model": "m", "content": "ok", "usage": {"output_tokens": 5}}}]))


def test_reports_when_due_and_retries_a_failure_soon(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_report, "_state", {"scan_at": 0.0})
    _transcript(tmp_path)
    cfg, client, t = SimpleNamespace(runner_id="r1"), FakeClient(), [1000.0]
    assert usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    assert client.posts == [1]
    t[0] += usage_report.INTERVAL_SECONDS - 1  # not due
    assert not usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    assert client.scopes == 1
    t[0] += 1  # due, but canopy-web is unreachable
    client.down = True
    assert not usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    client.down = False
    t[0] += usage_report.RETRY_SECONDS  # retried a minute later; the unchanged transcript is not re-sent
    assert usage_report.maybe_report(cfg, client, now_fn=lambda: t[0], home=tmp_path)
    assert client.posts == [1]
