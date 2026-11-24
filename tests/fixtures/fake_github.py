"""A fake of the GitHub API calls a delegation makes (`delegations.requests`)."""
from __future__ import annotations

from unittest import mock

# Far off, so no test lands inside the "expires soon" window as the calendar moves on.
EXPIRY = "2099-12-01 10:00:00 UTC"


class FakeGitHub:
    """Answers `/user` and the two probes like GitHub does. `pr` / `push` map a
    repo to a probe's status code: 422 allowed, 403 no permission, 404 unseen.
    Push defaults to whatever the PR probe says, as it does for a token that
    has both permissions or neither."""

    def __init__(self, *, login="olive", pr=None, push=None, user_status=200, expiry=EXPIRY):
        self.login, self.pr, self.user_status, self.expiry = login, pr or {}, user_status, expiry
        self.push = push or {}
        self.probed: list[str] = []

    def _resp(self, status, body=None, headers=None):
        r = mock.Mock(status_code=status, headers=headers or {})
        r.json.return_value = body or {}
        return r

    def get(self, url, **_):
        assert url.endswith("/user")
        return self._resp(self.user_status, {"login": self.login, "id": 4242, "name": "Olive Owner"},
                          {"github-authentication-token-expiration": self.expiry} if self.expiry else {})

    def post(self, url, json=None, **_):
        path = url.split("/repos/", 1)[1]
        if path.endswith("/git/refs"):
            repo = path.rsplit("/git/refs", 1)[0]
            # A ref at a commit that cannot exist: nothing is ever created.
            assert json["ref"].startswith("refs/heads/canopy-permission-probe/")
            assert json["sha"] == "0" * 39 + "1"
            return self._resp(self.push.get(repo, self.pr.get(repo, 404)))
        repo = path.rsplit("/pulls", 1)[0]
        self.probed.append(repo)
        assert json["head"].startswith("canopy-permission-probe/")  # never a real branch
        return self._resp(self.pr.get(repo, 404))
