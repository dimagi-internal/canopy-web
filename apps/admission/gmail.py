"""Reply on a sender's Gmail thread AS the agent, from canopy-web.

The transport for a rejected turn's sender notice (canopy-web#1400, option a).
canopy-web already holds the agent's `gog-token` credential (`AgentCredential`)
when the mailbox was connected from the browser, and that token is bound to
canopy-web's own Google client (`google_oauth.MINTING_CLIENT`) — the one client
this process has the secret for. So the server can refresh it and send with
the `gmail.modify` scope it was granted. A token minted at a terminal on the
fleet's desktop `canopy` client cannot be refreshed here; such an agent gets
`Unavailable`, and the notice is recorded as not sent, saying why.

The chosen design over a runner-sent notice (option b): the rejection happens
on the server, before any runner holds the turn, so a durable "pending notice"
would wait on the next mailbox poll of a box that may be the very cloud runner
that cannot help. Off unless `CANOPY_ADMISSION_SENDER_NOTICES` is set.

Every message carries `Auto-Submitted: auto-replied` (RFC 3834), so a
well-behaved auto-responder on the other end does not answer it.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from email.message import EmailMessage

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_REF = "gog-token"

#: Headers that say a machine wrote the message we would answer.
_AUTOMATION_HEADERS = ("Auto-Submitted", "Precedence", "X-Autoreply", "List-Id", "List-Unsubscribe")


class Unavailable(Exception):
    """The agent's mailbox cannot be sent from by canopy-web."""


class Automated(Exception):
    """The message we would answer was written by a machine; never reply."""


def _json(req: urllib.request.Request, timeout: int = 20) -> dict:
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 - fixed https endpoints
        body = res.read().decode()
    return json.loads(body) if body else {}


def access_token(agent) -> tuple[str, str]:
    """(access token, mailbox address) for the agent's `gog-token`, or Unavailable."""
    from apps.agents import google_oauth
    from apps.agents.models import AgentCredential
    from apps.common.encryption import decrypt_secret

    row = AgentCredential.objects.filter(agent=agent, name=TOKEN_REF).first()
    if row is None:
        raise Unavailable("canopy-web holds no gog-token for this agent")
    try:
        token = json.loads(decrypt_secret(row.value_enc))
    except Exception as exc:  # noqa: BLE001
        raise Unavailable("the stored gog-token could not be read") from exc
    if token.get("client") != google_oauth.MINTING_CLIENT or not token.get("refresh_token"):
        raise Unavailable(
            f"the gog-token is bound to the {token.get('client') or 'unknown'!r} client, which "
            "canopy-web cannot refresh; reconnect the mailbox from the agent's Credentials")
    if not any(s.startswith("https://www.googleapis.com/auth/gmail.modify") for s in token.get("scopes") or []):
        raise Unavailable("the gog-token was not granted gmail.modify")
    client_id, client_secret = google_oauth.google_client_credentials()
    if not client_id or not client_secret:
        raise Unavailable("this deployment has no Google OAuth client configured")
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    res = _json(urllib.request.Request(google_oauth.TOKEN_ENDPOINT, data=data, method="POST"))
    if not res.get("access_token"):
        raise Unavailable("Google refused to refresh the gog-token")
    return res["access_token"], token.get("email", "")


def _call(token: str, path: str, *, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}", method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
    )
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return _json(req)


def _headers(message: dict) -> dict[str, str]:
    return {h["name"].lower(): h.get("value", "")
            for h in (message.get("payload") or {}).get("headers") or []}


def _is_automated(headers: dict[str, str]) -> bool:
    auto = headers.get("auto-submitted", "").strip().lower()
    precedence = headers.get("precedence", "").strip().lower()
    autoreply = headers.get("x-autoreply", "").strip().lower()
    return bool(
        (auto and auto != "no")
        or precedence in {"bulk", "list", "junk", "auto_reply"}
        or (autoreply and autoreply != "no")
        or headers.get("list-id")
    )


def send_reply(agent, *, thread_id: str, message_id: str, to: str, subject: str, body: str) -> str:
    """Reply on the thread, threaded by In-Reply-To/References, then mark the
    thread read. Returns Gmail's id for the sent message. Raises Unavailable or
    Automated (nothing was sent), or anything urllib raises."""
    token, mailbox = access_token(agent)
    if not message_id:
        thread = _call(token, f"/threads/{urllib.parse.quote(thread_id)}?format=minimal")
        messages = thread.get("messages") or []
        if not messages:
            raise Unavailable("the thread has no messages")
        message_id = messages[-1]["id"]
    params = "&".join(f"metadataHeaders={h}" for h in ("Message-ID", "References", "Subject",
                                                        *_AUTOMATION_HEADERS))
    newest = _call(token, f"/messages/{urllib.parse.quote(message_id)}?format=metadata&{params}")
    headers = _headers(newest)
    if _is_automated(headers):
        raise Automated("the message carries automation headers")
    rfc_id = headers.get("message-id", "")
    msg = EmailMessage()
    if mailbox:
        msg["From"] = f"{agent.name} <{mailbox}>"
    msg["To"] = to
    original = headers.get("subject") or subject
    msg["Subject"] = original if re.match(r"(?i)^re:", original or "") else f"Re: {original}".strip()
    if rfc_id:
        msg["In-Reply-To"] = rfc_id
        msg["References"] = f"{headers.get('references', '')} {rfc_id}".strip()
    msg["Auto-Submitted"] = "auto-replied"
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = _call(token, "/messages/send", body={"raw": raw, "threadId": thread_id})
    _call(token, f"/threads/{urllib.parse.quote(thread_id)}/modify", body={"removeLabelIds": ["UNREAD"]})
    return sent.get("id", "")
