"""Arming the Gmail watch: token exchange, the watch call, and the re-arm window.

The live call can't be proven without a provisioned Pub/Sub topic, so everything
here runs against a fake subprocess runner and a fake HTTP opener — which is
enough to pin the parts that are ours: which endpoints, which payload, how the
expiry is parsed, when a re-arm is due, and that the exported refresh token never
outlives the call.
"""
import datetime as dt
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from canopy_runner import gmail_watch


@pytest.fixture()
def creds(tmp_path):
    (tmp_path / "credentials-canopy.json").write_text(
        json.dumps({"client_id": "cid.apps.googleusercontent.com", "client_secret": "shh"})
    )
    return tmp_path


def _gog(token="rt-1", *, rc=0, seen=None):
    def run(cmd, capture_output, text, timeout):
        if seen is not None:
            seen.append(cmd)
        if rc == 0:
            out = cmd[cmd.index("--out") + 1]
            with open(out, "w") as fh:
                json.dump({"email": cmd[4], "refresh_token": token}, fh)
        return SimpleNamespace(returncode=rc, stdout="", stderr="nope" if rc else "")
    return run


def _opener(responses, calls=None):
    """Answer each request in order from `responses`."""
    seq = list(responses)

    def open_(req, timeout=None):
        if calls is not None:
            calls.append(req)
        body = seq.pop(0)
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(json.dumps(body).encode())

    return _ctx(open_)


def _ctx(fn):
    class _Wrap:
        def __init__(self, stream):
            self._s = stream

        def __enter__(self):
            return self._s

        def __exit__(self, *a):
            return False

    def open_(req, timeout=None):
        return _Wrap(fn(req, timeout))

    return open_


# ── the token exchange ───────────────────────────────────────────────────────


def test_access_token_posts_the_refresh_grant(creds):
    calls = []
    tok = gmail_watch.access_token(
        "eva@dimagi-ai.com", "canopy", config_dir=creds,
        runner=_gog(), opener=_opener([{"access_token": "at-1"}], calls),
    )
    assert tok == "at-1"
    assert calls[0].full_url == gmail_watch.TOKEN_URL
    sent = dict(p.split("=", 1) for p in calls[0].data.decode().split("&"))
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "rt-1"


def test_the_exported_token_file_is_always_cleaned_up(creds):
    """It holds a refresh token; a failure must not leave it in the temp dir."""
    seen = []
    with pytest.raises(gmail_watch.WatchError):
        gmail_watch.access_token(
            "eva@dimagi-ai.com", "canopy", config_dir=creds,
            runner=_gog(seen=seen),
            opener=_opener([urllib.error.URLError("down")]),
        )
    out = seen[0][seen[0].index("--out") + 1]
    import os
    assert not os.path.exists(out)


def test_a_missing_gog_client_is_a_clear_error(tmp_path):
    with pytest.raises(gmail_watch.WatchError, match="no gog client credentials"):
        gmail_watch.access_token("eva@dimagi-ai.com", "nope", config_dir=tmp_path,
                                 runner=_gog(), opener=_opener([{}]))


def test_a_failed_export_is_a_clear_error(creds):
    with pytest.raises(gmail_watch.WatchError):
        gmail_watch.access_token("eva@dimagi-ai.com", "canopy", config_dir=creds,
                                 runner=_gog(rc=1), opener=_opener([{}]))


def test_a_response_with_no_access_token_is_an_error(creds):
    with pytest.raises(gmail_watch.WatchError, match="no access_token"):
        gmail_watch.access_token("eva@dimagi-ai.com", "canopy", config_dir=creds,
                                 runner=_gog(), opener=_opener([{"scope": "x"}]))


# ── the watch call ───────────────────────────────────────────────────────────


def test_arm_posts_the_topic_and_parses_the_expiry(creds):
    calls = []
    expires = gmail_watch.arm(
        "eva@dimagi-ai.com", "canopy", "projects/p/topics/t", config_dir=creds,
        runner=_gog(),
        opener=_opener(
            [{"access_token": "at-1"},
             {"historyId": "99", "expiration": "1785600000000"}],
            calls,
        ),
    )
    watch_req = calls[1]
    assert watch_req.full_url == gmail_watch.WATCH_URL
    assert watch_req.headers["Authorization"] == "Bearer at-1"
    body = json.loads(watch_req.data.decode())
    assert body["topicName"] == "projects/p/topics/t"
    # INBOX-only: a watch on every label would ring the doorbell for sent mail
    # and label reshuffles, each costing a gog read that finds nothing new.
    assert body["labelIds"] == ["INBOX"]
    assert expires == dt.datetime.fromtimestamp(1785600000000 / 1000, dt.UTC)


def test_arm_errors_when_the_response_has_no_expiration(creds):
    with pytest.raises(gmail_watch.WatchError, match="no expiration"):
        gmail_watch.arm("eva@dimagi-ai.com", "canopy", "projects/p/topics/t",
                        config_dir=creds, runner=_gog(),
                        opener=_opener([{"access_token": "at"}, {"historyId": "1"}]))


# ── when to re-arm ───────────────────────────────────────────────────────────


def test_never_armed_is_due():
    assert gmail_watch.due(None, now=dt.datetime.now(dt.UTC)) is True


def test_inside_the_window_is_due():
    now = dt.datetime.now(dt.UTC)
    assert gmail_watch.due(now + dt.timedelta(hours=6), now=now) is True


def test_a_fresh_watch_is_not_due():
    now = dt.datetime.now(dt.UTC)
    assert gmail_watch.due(now + dt.timedelta(days=6), now=now) is False


def test_an_expired_watch_is_due():
    now = dt.datetime.now(dt.UTC)
    assert gmail_watch.due(now - dt.timedelta(hours=1), now=now) is True


def test_the_window_leaves_six_days_of_retries():
    """Google's ceiling is 7 days; re-arming 24h early means six further ticks
    can still succeed before push actually lapses."""
    assert gmail_watch.REARM_WINDOW == dt.timedelta(hours=24)
