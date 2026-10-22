"""The session engine (`SESSION_ENGINE`): Django's database sessions, minus the
lost update that `SESSION_SAVE_EVERY_REQUEST` invites.

A rolling session saves on every request, and Django's save writes the WHOLE
session dict as that request loaded it. So a request already in flight while
another one writes a key — a page poll overlapping the passkey ceremony's
`begin`, which stashes its challenge — puts its stale copy back on the way out,
and the challenge is gone by `finish` ("no passkey ceremony in progress"). The
step-up window is lost the same way.

A request that did not change the session has nothing to write but the new
expiry, so that is all it writes. A request that did change it saves as before.
"""
from __future__ import annotations

from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.backends.db import SessionStore as DbSessionStore


class SessionStore(DbSessionStore):
    def save(self, must_create: bool = False) -> None:
        if must_create or self.session_key is None or self.modified:
            return super().save(must_create=must_create)
        refreshed = self.model.objects.filter(session_key=self.session_key).update(
            expire_date=self.get_expiry_date())
        if not refreshed:
            # Gone (signed out elsewhere): what Django's own update does here.
            raise UpdateError
