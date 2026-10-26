"""The handful of Slack Web API calls canopy makes, over ``requests``.

Not ``slack_sdk``: five POSTs do not need a dependency. The one trap worth a
wrapper is that Slack answers a failed call with **HTTP 200 and ``ok: false``**,
so a status-code check reports failure as success. ``call`` raises instead.
"""
from __future__ import annotations

import requests

API = "https://slack.com/api/"
TIMEOUT = 10


def unescape(text: str) -> str:
    """Slack's message text with its three escapes undone.

    Slack sends `&`, `<` and `>` in a message as `&amp;`, `&lt;` and `&gt;` (its
    own markup — mentions, links — uses the bare brackets). Call it AFTER the
    markup has been read, so a person who typed `<@U1>` as text never becomes a
    mention; `&amp;` goes last, so a typed `&lt;` stays `&lt;`.
    """
    return (text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class SlackApiError(Exception):
    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


class RateLimited(Exception):
    """Slack answered HTTP 429. `retry_after` is its Retry-After, in seconds."""

    def __init__(self, method: str, retry_after: int):
        super().__init__(f"{method}: rate limited, retry after {retry_after}s")
        self.method = method
        self.retry_after = retry_after


def call(method: str, *, token: str = "", data: dict | None = None, json: dict | None = None,
         timeout: float = TIMEOUT) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if json is not None:
        resp = requests.post(API + method, headers=headers, json=json, timeout=timeout)
    else:
        resp = requests.post(API + method, headers=headers, data=data or {}, timeout=timeout)
    if getattr(resp, "status_code", 200) == 429:
        try:
            retry_after = int((getattr(resp, "headers", None) or {}).get("Retry-After") or 30)
        except (TypeError, ValueError):
            retry_after = 30
        raise RateLimited(method, max(1, retry_after))
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise SlackApiError(method, str(body.get("error") or "unknown_error"))
    return body


#: `agents.sessions.setStatus` values. PROCESSING draws Slack's own "Working…"
#: indicator (with a Stop button, since we subscribe to `agent_session_stopped`);
#: SUSPENDED is "it needs a person"; ACTIVE is "ready for your next message".
PROCESSING, ACTIVE, SUSPENDED, CLOSED = "processing", "active", "suspended", "closed"

#: Every way this call can mean "this app/workspace does not do agent sessions".
#: All are configuration facts, not failures of the thing being reported, so the
#: caller degrades to the text line it already posts.
_NO_AGENT_SESSIONS = frozenset({
    "feature_disabled", "missing_scope", "not_allowed_token_type", "invalid_arguments",
    "unknown_method", "method_not_supported_for_channel_type", "invalid_channel_type",
    "agent_not_enabled", "not_an_agent",
})


def set_session_status(token: str, *, channel: str, status: str, thread_ts: str = "") -> bool:
    """Drive Slack's NATIVE working indicator for this thread. True if it took.

    The one thing a posted message cannot do: an edit to text is silent and
    static, while this is the spinner Slack draws under the composer in the
    thread itself, and it is what carries the Stop button. It needs the app to
    be declared an agent (`features.agent_view` + `assistant:write` — see
    `commands.declare_agent`), so on any deployment where that has not been done
    every call answers one of `_NO_AGENT_SESSIONS`. That is not an error worth
    raising: the status line says the same thing in words, so this returns False
    and the thread is no worse off than before.
    """
    payload = {"channel_id": channel, "status": status}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        call("agents.sessions.setStatus", token=token, json=payload)
        return True
    except SlackApiError as e:
        if e.error in _NO_AGENT_SESSIONS:
            return False
        raise


def post_message(token: str, *, channel: str, text: str, thread_ts: str = "",
                 blocks: list | None = None, persona: dict | None = None) -> str:
    """Post, optionally AS an agent (`persona` = {"username", "icon_url"}). Returns the ts."""
    return post_message_body(token, channel=channel, text=text, thread_ts=thread_ts,
                             blocks=blocks, persona=persona)["ts"]


def post_message_body(token: str, *, channel: str, text: str, thread_ts: str = "",
                      blocks: list | None = None, persona: dict | None = None) -> dict:
    """`post_message`, returning Slack's whole answer — including the channel ID,
    which is what a caller that posted to a `#name` needs to store.

    A persona needs the `chat:write.customize` scope. An install from before it
    was added answers `missing_scope`; the post is retried as the plain bot, so
    a scope nobody has re-approved costs the agent's face, never the reply.
    """
    # `text` is always sent, blocks or not: it is what notifications, screen
    # readers and any client that cannot render blocks show.
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    if persona:
        try:
            return call("chat.postMessage", token=token, json={**payload, **persona})
        except SlackApiError as e:
            if e.error != "missing_scope":
                raise
    return call("chat.postMessage", token=token, json=payload)


def update_message(token: str, *, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
    call("chat.update", token=token, json={"channel": channel, "ts": ts, "text": text,
                                           "blocks": blocks or []})


def post_ephemeral(token: str, *, channel: str, user: str, text: str, thread_ts: str = "") -> None:
    payload = {"channel": channel, "user": user, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    call("chat.postEphemeral", token=token, json=payload)


def user_info(token: str, slack_user_id: str) -> dict:
    return call("users.info", token=token, data={"user": slack_user_id}).get("user") or {}


def display_name(info: dict, default: str = "") -> str:
    """The name a person reads for a `users.info` user: their full name, else
    the display name they chose, else their handle, else `default`."""
    profile = info.get("profile") or {}
    return str(profile.get("real_name") or profile.get("display_name") or info.get("real_name")
               or info.get("name") or default).strip()


def user_name(token: str, slack_user_id: str) -> str:
    """`display_name` of one user, falling back to their id when Slack will not
    say (a deleted user, a foreign workspace's). A rate limit still raises."""
    try:
        info = user_info(token, slack_user_id)
    except SlackApiError:
        info = {}
    return display_name(info, slack_user_id)


def message_name(m: dict) -> str:
    """A display name Slack sent with a message itself — a bot's post (canopy's
    own relayed replies carry the agent's name), which `users.info` cannot name."""
    return str(m.get("username") or (m.get("bot_profile") or {}).get("name") or "")


#: Slack's page size for the conversations.* history calls.
PAGE = 200


def page(token: str, method: str, data: dict, cursor: str = "", *, limit: int = PAGE) -> tuple[list[dict], str]:
    """One page of a cursor-paged `conversations.*` call: (messages, next cursor),
    the cursor "" when there is no more."""
    body = call(method, token=token, data={**data, "limit": limit, **({"cursor": cursor} if cursor else {})})
    nxt = (body.get("response_metadata") or {}).get("next_cursor") or ""
    return body.get("messages") or [], (nxt if body.get("has_more", True) else "")


def paginate(token: str, method: str, data: dict, *, budget: int) -> list[dict]:
    """Up to `budget` messages of a cursor-paged `conversations.*` call, page by page."""
    out: list[dict] = []
    cursor = ""
    while len(out) < budget:
        msgs, cursor = page(token, method, data, cursor, limit=min(PAGE, budget - len(out)))
        out.extend(msgs)
        if not cursor:
            break
    return out[:budget]


def lookup_user_id(token: str, email: str) -> str:
    """The Slack user with this email, or "" (needs `users:read.email`)."""
    try:
        return str((call("users.lookupByEmail", token=token, data={"email": email}).get("user") or {})
                   .get("id") or "")
    except SlackApiError:
        return ""


def permalink(token: str, *, channel: str, ts: str, timeout: float = TIMEOUT) -> str:
    try:
        return str(call("chat.getPermalink", token=token, timeout=timeout,
                        data={"channel": channel, "message_ts": ts}).get("permalink") or "")
    except SlackApiError:
        return ""


def channel_name(token: str, channel: str, *, timeout: float = TIMEOUT) -> str:
    """A channel's name, or "" — Slack answers `missing_scope` to an install without
    `channels:read` (public) / `groups:read` (private), and a DM has no name."""
    try:
        return str((call("conversations.info", token=token, timeout=timeout,
                         data={"channel": channel}).get("channel") or {}).get("name") or "")
    except SlackApiError:
        return ""


def channel_info(token: str, channel: str) -> dict:
    """`conversations.info`'s channel (id, name, `creator`, …). Raises on failure."""
    return call("conversations.info", token=token, data={"channel": channel}).get("channel") or {}


def is_channel_member(token: str, channel: str, slack_user_id: str) -> bool:
    """Whether this person is in the channel now — `conversations.members`, paged.

    Being able to SEE a channel is not being in it: anyone in the Slack can
    open a public channel and press a button there."""
    cursor = ""
    for _ in range(100):  # 100 pages x 1000 is far past any real channel
        data = {"channel": channel, "limit": 1000, **({"cursor": cursor} if cursor else {})}
        body = call("conversations.members", token=token, data=data)
        if slack_user_id in (body.get("members") or []):
            return True
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return False
    return False


def conversation_members(token: str, channel: str) -> list[str]:
    """Everyone in a conversation now (channel, private channel, DM) — `conversations.members`,
    paged. Raises on failure: the caller must tell "nobody" from "Slack did not say"."""
    out: list[str] = []
    cursor = ""
    for _ in range(100):
        data = {"channel": channel, "limit": 1000, **({"cursor": cursor} if cursor else {})}
        body = call("conversations.members", token=token, data=data)
        out.extend(str(m) for m in body.get("members") or [])
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return out


def team_users(token: str) -> list[dict]:
    """Every user of the Slack (`users.list`, paged) — the same objects `users.info` returns.
    Raises on failure."""
    out: list[dict] = []
    cursor = ""
    for _ in range(100):
        data = {"limit": 200, **({"cursor": cursor} if cursor else {})}
        body = call("users.list", token=token, data=data)
        out.extend(m for m in body.get("members") or [] if isinstance(m, dict))
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return out


def user_email(token: str, slack_user_id: str) -> str:
    return str((user_info(token, slack_user_id).get("profile") or {}).get("email") or "")


def granted_scopes(token: str) -> list[str] | None:
    """The scopes this token holds, from the `X-OAuth-Scopes` header Slack puts
    on every answer (here, `auth.test`'s). None when Slack did not say."""
    from collections.abc import Mapping

    resp = requests.post(API + "auth.test", headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    resp.raise_for_status()
    if not resp.json().get("ok"):
        return None
    headers = getattr(resp, "headers", None)
    raw = headers.get("X-OAuth-Scopes") if isinstance(headers, Mapping) else None
    return [s.strip() for s in raw.split(",") if s.strip()] if isinstance(raw, str) else None


def oauth_access(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    return call("oauth.v2.access", data={
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "redirect_uri": redirect_uri,
    })
