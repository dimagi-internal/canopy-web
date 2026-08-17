"""Keep each mailbox's Gmail watch armed, so push keeps being delivered.

A Gmail ``users.watch`` registration expires within 7 days and Google will not
renew it. Nothing else in the system can re-arm it: the server holds no mailbox
credentials (that is the whole point of the doorbell — see ``apps/inbound``), and
``gog`` has no ``watch`` verb and no way to print an access token. The runner is
the one process that already holds every agent's OAuth grant, so it does it here.

**This is NOT the delivery path.** Arming is a registration call made at most
once a week; delivery is Gmail → Pub/Sub → canopy-web → the ``check_inbox``
doorbell, continuous and in seconds. The runner is not in the delivery path at
all, and a runner that is down costs re-arming, never realtime.

Why credentials are read rather than shelled out to: ``gog`` stores the OAuth
client (``credentials-<client>.json``) and the refresh token (keyring, exportable
to a file) but exposes no bearer, and Gmail's watch endpoint needs one. So this
does the standard refresh-token exchange itself. Nothing is stored anywhere new —
the secrets stay on this box, in gog's own files, which is what makes this
strictly narrower than domain-wide delegation: it reaches the five mailboxes we
own and nothing else in the domain.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger("canopy_runner")

TOKEN_URL = "https://oauth2.googleapis.com/token"
WATCH_URL = "https://gmail.googleapis.com/gmail/v1/users/me/watch"

#: gog's config dir, where credentials-<client>.json lives.
GOG_CONFIG_DIR = Path.home() / "Library" / "Application Support" / "gogcli"

#: Re-arm this far ahead of expiry. Google's ceiling is 7 days, so a day of slack
#: leaves six chances to succeed before push actually lapses — and the server
#: warns at the same threshold, so a warn row means the re-arm should have
#: happened and didn't.
REARM_WINDOW = dt.timedelta(hours=24)

#: How long to wait after N consecutive FAILED arms before attempting again.
#: A failure used to leave the state file untouched, so the 24h `REARM_WINDOW`
#: stayed open and the loop retried every tick (~5s) for as long as the failure
#: lasted — 18,680 token requests and a 20MB log over one 35-hour outage. The
#: schedule is per consecutive failure and CAPS, because the failures worth
#: guarding against (a revoked grant, a dead OAuth client) are permanent until a
#: human acts: retrying a dead client faster does not make it less dead.
BACKOFF = (
    dt.timedelta(minutes=1),
    dt.timedelta(minutes=5),
    dt.timedelta(minutes=15),
    dt.timedelta(minutes=30),
)

#: Tell the server "this mailbox is not being watched" once a failure has
#: repeated this many times. Not the first: a single failed arm is usually a
#: blip, and there are still ~6 days of slack before push actually lapses. With
#: BACKOFF that is roughly a minute in — fast enough to be actionable, slow
#: enough that a transient 401 never raises an error someone has to clear.
REPORT_AFTER_FAILURES = 2


def backoff_until(consecutive: int, *, now: dt.datetime) -> dt.datetime:
    """When the next arm attempt is allowed, after `consecutive` failures."""
    idx = min(max(consecutive, 1), len(BACKOFF)) - 1
    return now + BACKOFF[idx]


class WatchError(Exception):
    pass


def _client_credentials(client: str, *, config_dir: Path | None = None) -> tuple[str, str]:
    """(client_id, client_secret) for a gog OAuth client."""
    path = (config_dir or GOG_CONFIG_DIR) / f"credentials-{client}.json"
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise WatchError(f"no gog client credentials at {path}") from exc
    except ValueError as exc:
        raise WatchError(f"malformed gog client credentials at {path}") from exc
    cid, secret = data.get("client_id"), data.get("client_secret")
    if not cid or not secret:
        raise WatchError(f"gog client credentials at {path} are missing client_id/secret")
    return cid, secret


def _refresh_token(email: str, *, runner=subprocess.run) -> str:
    """Pull this mailbox's refresh token out of gog's keyring.

    ``gog auth tokens export`` requires ``--out <path>`` (it will not print), so
    the token necessarily touches disk. It is written into a private
    0600 temp file and unlinked in a finally — including on failure, which is the
    branch that matters, since an exception here would otherwise leave a refresh
    token lying in the temp dir.
    """
    fd, tmp = tempfile.mkstemp(prefix="canopy-gogtok-", suffix=".json")
    os.close(fd)
    os.chmod(tmp, 0o600)
    try:
        r = runner(
            ["gog", "auth", "tokens", "export", email, "--out", tmp, "--overwrite"],
            capture_output=True, text=True, timeout=45,
        )
        if r.returncode != 0:
            raise WatchError(r.stderr.strip() or f"gog token export failed for {email}")
        try:
            token = json.loads(Path(tmp).read_text()).get("refresh_token") or ""
        except (OSError, ValueError) as exc:
            raise WatchError(f"unreadable token export for {email}") from exc
        if not token:
            raise WatchError(f"no refresh_token in gog's export for {email}")
        return token
    except FileNotFoundError as exc:
        raise WatchError("gog not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise WatchError("gog token export timed out") from exc
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _post_form(url: str, form: dict, *, opener=urllib.request.urlopen) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with opener(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300] if hasattr(exc, "read") else str(exc)
        raise WatchError(f"{url} -> {exc.code}: {detail}") from exc
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise WatchError(f"{url} failed: {exc}") from exc


def _post_json(url: str, payload: dict, token: str, *, opener=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with opener(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300] if hasattr(exc, "read") else str(exc)
        raise WatchError(f"{url} -> {exc.code}: {detail}") from exc
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise WatchError(f"{url} failed: {exc}") from exc


def access_token(email: str, client: str, *, config_dir: Path | None = None,
                 runner=subprocess.run, opener=urllib.request.urlopen) -> str:
    cid, secret = _client_credentials(client, config_dir=config_dir)
    data = _post_form(
        TOKEN_URL,
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": _refresh_token(email, runner=runner),
            "grant_type": "refresh_token",
        },
        opener=opener,
    )
    token = data.get("access_token") or ""
    if not token:
        raise WatchError(f"no access_token in the refresh response for {email}")
    return token


def arm(email: str, client: str, topic: str, *, config_dir: Path | None = None,
        runner=subprocess.run, opener=urllib.request.urlopen) -> dt.datetime:
    """Register/renew the watch. Returns the new expiry.

    ``labelIds: [INBOX]`` matches the runner's own ``in:inbox is:unread`` query —
    a watch on every label would ring the doorbell for sent mail and label
    reshuffles, each costing a `gog` read that finds nothing new.
    """
    token = access_token(email, client, config_dir=config_dir, runner=runner, opener=opener)
    data = _post_json(
        WATCH_URL,
        {"topicName": topic, "labelIds": ["INBOX"], "labelFilterBehavior": "include"},
        token,
        opener=opener,
    )
    raw = data.get("expiration")
    if not raw:
        raise WatchError(f"watch response for {email} carried no expiration: {data}")
    # Google returns epoch MILLISECONDS as a string.
    return dt.datetime.fromtimestamp(int(raw) / 1000, dt.UTC)


def due(expires_at: dt.datetime | None, *, now: dt.datetime) -> bool:
    """Never armed, or inside the re-arm window."""
    return expires_at is None or (expires_at - now) <= REARM_WINDOW
