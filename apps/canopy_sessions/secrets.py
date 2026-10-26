"""Secrets a person hands to ONE chat. See `models.SessionSecret`.

The person shares a secret from the chat's session menu; nothing is posted into
the chat. They just mention it by name ("put GH_TOKEN in canopy's Actions
secrets"), and the agent running that chat finds and spends it with
`canopy secret list` / `canopy secret exec NAME -- <command>`.

The rules, each load-bearing:

* **canopy-web cannot read it.** The person's browser seals the value (`zks1`,
  zk-v1 §6) to the device keys of the machine running the chat — the runner
  the chat is bound to, each ACTIVE device key with an approved ECDH key
  (`recipients`). canopy-web hands out those public keys, stores the sealed
  value, and refuses plaintext. Only that machine's `canopy secret exec` opens
  it, with its own ECDH key. Bound as `session:<id>` where an agent id goes in
  the AAD and HKDF info, at kv 0.
* **The value never enters the chat.** Nothing here writes a `Message`, and
  nothing a browser can reach returns even the sealed value — the list is names
  and times.
* **Only the session driving the chat gets it.** It proves which chat it is
  with the CHAT KEY canopy issued when its runner claimed the chat's turn
  (`models.ChatKey`, `/api/session-secrets/key`): a permission canopy hands
  out, rather than one inferred from who is calling. Another session of the
  same agent, or the same agent in another chat, holds a different key and
  gets a 404.
* **Thirty minutes.** A hand-off, not a store — see `TTL`.

Honest limits: canopy-web names the keys a browser seals to, so a canopy-web
that was itself compromised AT THE MOMENT of sharing could name a key of its
own; what it stores can never be read by it afterwards. And processes of one
OS user can read each other's files, so this cannot stop a hostile local
process on the runner that goes looking for another chat's key file.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

from django.utils import timezone

from apps.agents.credentials.zk import ZkError, b64u_bytes
from apps.agents.credentials.zk_seal import SEALED_PREFIX, check_sealed_value
from apps.events import services as events
from apps.harness.models import RunnerDeviceKey

from .models import Session, SessionSecret

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
#: The sealed value's own ceiling is `zk.MAX_VALUE_ZK_LEN`; the browser caps the plaintext.
VALUE_MAX = 16_384
#: A shared secret's values are sealed at this version: a chat has no vault key.
KV = 0
#: A shared secret dies this long after it was shared (re-sharing the name
#: restarts the clock). Enforced on every read, and the rows are deleted by
#: `purge_expired` on the off-request clock, so the ciphertext does not outlive
#: its use either.
TTL = dt.timedelta(minutes=30)


class ShareRefusedError(Exception):
    """A share canopy-web will not take. `code` is the machine-readable `error`;
    the message is a sentence for the person."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def binding_id(session: Session) -> str:
    """What a chat secret is bound to in the zk-v1 AAD and HKDF info, where an
    agent's id goes for an agent vault (and `vault:<uuid>` for a workspace vault)."""
    return f"session:{session.pk}"


@dataclass
class Recipients:
    """Who a value shared into `session` is sealed to: the machine running it."""

    binding_id: str
    runner: object | None = None
    keys: list = field(default_factory=list)
    #: Why nobody can be sealed to, as a sentence; "" when `keys` is not empty.
    refusal: str = ""
    code: str = ""


def recipients(session: Session) -> Recipients:
    """The ACTIVE device keys with an approved ECDH key on the runner this chat is
    bound to — the one box that will run `canopy secret exec` for it."""
    out = Recipients(binding_id=binding_id(session))
    binding = getattr(session, "runner_binding", None)
    runner = binding.runner if binding is not None else None
    if runner is None:
        out.code = "no_runner"
        out.refusal = ("This chat is not running on a machine yet, so there is nothing to seal the "
                       "secret to. Send it a message first, then share the secret.")
        return out
    out.runner = runner
    out.keys = list(RunnerDeviceKey.objects.filter(
        runner=runner, status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False,
    ).exclude(ecdh_jkt="").exclude(ecdh_public_jwk={}).order_by("created_at"))
    if not out.keys:
        out.code = "runner_has_no_ecdh_key"
        out.refusal = (f"The machine running this chat ({runner.name}) has no approved encryption "
                       "key, so canopy cannot hand it a secret it alone can open. Its owner approves "
                       "one on the runner's page (Device keys); until then, nothing was shared.")
    return out


def expires_at(row: SessionSecret) -> dt.datetime:
    return row.updated_at + TTL


def purge_expired(now: dt.datetime | None = None) -> int:
    """Delete every secret past its TTL. One DELETE; safe to call on any read."""
    cutoff = (now or timezone.now()) - TTL
    deleted, _ = SessionSecret.objects.filter(updated_at__lte=cutoff).delete()
    return deleted


def normalize_name(name: str) -> str:
    """`gh token` → `GH_TOKEN`. Rejects anything that still is not env-var shaped."""
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (name or "").strip()).strip("_").upper()
    if not NAME_RE.match(cleaned):
        raise ValueError("name must start with a letter and use only letters, digits and _")
    return cleaned


def _sealed_body(value_zk: str) -> dict:
    return json.loads(b64u_bytes(value_zk[len(SEALED_PREFIX):], "value_zk"))


def set_secret(session: Session, name: str, value_zk: str, *, user=None) -> SessionSecret:
    """Keep a value the caller sealed to this chat's machine. The name must be in
    its final shape: the seal binds it, and canopy-web cannot re-seal."""
    if (name or "") != normalize_name(name):
        raise ValueError(f"seal it under the name {normalize_name(name)}: the seal binds the name")
    try:
        check_sealed_value(value_zk, "value_zk")
    except ZkError as exc:
        raise ShareRefusedError(422, "sealed_only", f"{exc} — canopy-web takes a chat secret only sealed "
                      "in the sharer's browser, never its value") from exc
    body = _sealed_body(value_zk)
    if body["kv"] != KV:
        raise ShareRefusedError(422, "sealed_only", f"a chat secret is sealed at kv {KV}")
    who = recipients(session)
    if not who.keys:
        raise ShareRefusedError(409, who.code, who.refusal)
    allowed = {k.ecdh_jkt for k in who.keys}
    targets = {(r["kind"], r["target"]) for r in body["r"]}
    if any(kind != "device" or target not in allowed for kind, target in targets):
        raise ShareRefusedError(409, "stale_recipients",
                      f"The machine running this chat changed while you were sharing, so the secret "
                      f"was sealed to a key {who.runner.name} does not hold. Share it again.")
    row, _ = SessionSecret.objects.update_or_create(
        session=session, name=name,
        defaults={
            "value_zk": value_zk,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "last_used_at": None,
        },
    )
    return row


def live_secrets(session: Session):
    purge_expired()  # an expired secret is invisible even if no tick swept it yet
    # A row with no sealed value predates sealing (its value was dropped with the
    # column) and can never be opened: it is not there.
    return session.secrets.exclude(value_zk="").select_related("created_by")


def resolve_secret(session: Session, name: str, *, user) -> SessionSecret | None:
    """The row, carrying its sealed value, or None when there is no such live
    secret. Callers gate first. Audited by name, never value."""
    row = live_secrets(session).filter(name=name).first()
    if row is None:
        return None
    SessionSecret.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
    try:
        events.record([{
            "source": "canopy_sessions.secrets",
            "kind": "session.secret.resolved",
            "level": "info",
            "key": f"{session.pk}:{name}:{user.pk}",
            "summary": f"{name} resolved for session {str(session.pk)[:8]} by {user.email or user.pk}",
            "payload": {"session": str(session.pk), "name": name, "user": user.pk},
        }], workspace=session.workspace)
    except Exception:  # noqa: BLE001 — an audit hiccup must not deny the hand-off
        pass
    return row
