"""Site sign-offs: a connected site asks canopy for a person's passkey approval of ONE
action, and canopy hands back a signed "yes" (spec canopy-web
``2026-10-27-site-sign-offs-design``).

A site that wants a person behind something (labs, before it mints a full-access MCP
token) does not build passkeys of its own. It asks canopy, canopy asks the person for
their passkey on its own page, and the site checks canopy's signed approval before it
acts. The approval is not a credential: it completes exactly one pending action the site
itself created, named by its ``site_ref``.

No new secrets. The trust anchors are the site agents API's (``canopy_sdk.site_agents``):

* the site asks with a JWT signed by ITS key (the one at its ``jwks_url``);
* canopy answers with a JWS signed by ITS client key (the ``Canopy-Execution`` key,
  found through canopy's Client ID Metadata Document).

::

    from canopy_sdk import sign_offs
    from canopy_sdk.host import ClientKeyResolver

    asked = sign_offs.request_sign_off(
        "https://canopy.dimagi.com", "connect-labs", signing_key,
        subject="someone@example.org", action_key="labs.mcp_token.full",
        title="Create a full-access labs MCP token", site_ref=pending.ref,
        return_url="https://labs.example.org/labs/mcp/tokens/approved/", workspace="connect")
    # send the person to asked.approve_url; canopy sends their browser back to
    # return_url?sign_off=<jws>&id=<id> (or ?declined=<id>)

    approval = sign_offs.verify_approval(
        jws, client_id=canopy_client_id, site_name="connect-labs", client_keys=ClientKeyResolver(),
        expect_action="labs.mcp_token.full", expect_site_ref=pending.ref)
    # spend approval.jti once, check approval.subject is who you are acting for, then act

When the approver is not the person at the site (an agent's owner approves for it),
``fetch_status`` reads the outcome, approval included, with a signed GET.

Both halves live here so canopy and every site read ONE definition:
``request_sign_off`` / ``fetch_status`` / ``verify_approval`` are the site's,
``verify_request`` / ``sign_approval`` canopy's.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from . import contract
from .contract import ContractError, constant_time_equal, normalize_url
from .jose import decode, new_jti, sign, unverified_header
from .keys import load_private_key, public_jwk, public_key_for

#: Where a site asks, under canopy's base URL; a request's status is ``PATH/<id>``.
PATH = "/api/sites/sign-offs"
#: ``typ`` of the site's request (asking, and reading a status), and of canopy's approval.
#: Distinct from every other token either side signs, so neither replays as another.
REQUEST_TYP = "canopy-sign-off-request+jwt"
APPROVAL_TYP = "canopy-sign-off+jwt"
#: A request is used once, at the moment it is sent.
REQUEST_MAX_LIFETIME = 60
#: How long an approval lives: long enough to carry back to the site, no longer.
APPROVAL_LIFETIME = 600
REQUEST_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "sub", "aud", "iat", "exp", "jti")
APPROVAL_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "aud", "sub", "iat", "exp", "jti", "sign_off")
#: The claim a request to ASK carries.
ASK_CLAIM = "sign_off_request"
#: Bounds on what a site sends: the approver reads ``title`` and ``detail`` as text.
MAX_ACTION_KEY = 100
MAX_TITLE = 200
MAX_DETAIL = 1000
MAX_SITE_REF = 128
MAX_SUBJECT = 254
MAX_RETURN_URL = 2000

#: Who may approve, as canopy answers it.
SUBJECT = "subject"
AGENT_ADMINS = "agent_admins"
#: A request's states.
OPEN, APPROVED, DECLINED, EXPIRED = "open", "approved", "declined", "expired"
STATES = (OPEN, APPROVED, DECLINED, EXPIRED)
#: How the approver proved it was them, now. The only value there is.
STEP_UP_PASSKEY = "passkey"

#: Codes ``SignOffError`` carries that a site acts on (others are the verifier's):
#: ``no_approver`` — nobody in canopy may approve for this subject (the site decides what
#: that means for it); ``bad_return_url`` — not https on an origin the site registered;
#: ``not_configured``, ``unreachable``, ``refused``, and canopy's site codes
#: (``unknown_issuer``, ``ambiguous_issuer``, ``replayed``, ``rate_limited``...).
NO_APPROVER = "no_approver"
BAD_RETURN_URL = "bad_return_url"
NOT_CONFIGURED = "not_configured"
UNREACHABLE = "unreachable"
REFUSED = "refused"


class SignOffError(Exception):
    """A sign-off could not be asked for, read, or believed. ``code`` names why; the
    message never contains a credential. ``status`` is canopy's HTTP status when it
    answered with a refusal."""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def create_url(canopy_url: str) -> str:
    """Where a site asks — the ask request's ``aud``."""
    return normalize_url(canopy_url) + PATH


def status_url(canopy_url: str, sign_off_id: str) -> str:
    """Where a site reads one request — that read's ``aud``."""
    return f"{create_url(canopy_url)}/{quote(str(sign_off_id), safe='')}"


def _text(value, name: str, limit: int, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SignOffError("malformed", f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise SignOffError("malformed", f"{name} is required")
    if len(value) > limit:
        raise SignOffError("malformed", f"{name} is at most {limit} characters")
    return value


def ask_payload(*, subject: str, action_key: str, title: str, site_ref: str, return_url: str,
                detail: str = "") -> dict:
    """The ``sign_off_request`` claim, checked: what is asked, of whom, and where the
    approver's browser goes after deciding."""
    out = {
        "subject": _text(subject, "subject", MAX_SUBJECT).lower(),
        "action": {"key": _text(action_key, "action.key", MAX_ACTION_KEY),
                   "title": _text(title, "action.title", MAX_TITLE),
                   "detail": _text(detail, "action.detail", MAX_DETAIL, required=False)},
        "site_ref": _text(site_ref, "site_ref", MAX_SITE_REF),
        "return_url": _text(return_url, "return_url", MAX_RETURN_URL),
    }
    if "@" not in out["subject"]:
        raise SignOffError("malformed", "subject must be an email address")
    if not out["return_url"].startswith("https://"):
        raise SignOffError(BAD_RETURN_URL, "return_url must be https")
    return out


# --- the site asks -----------------------------------------------------------------


def request_assertion(site_name: str, site_key, audience: str, *, ask: Mapping | None = None,
                      workspace: str = "", agent: str = "", now: float | None = None) -> str:
    """The site's signed request: to ASK (``ask``, from ``ask_payload``, at
    ``create_url``) or to read a status (no ``ask``, at ``status_url``).

    ``iss`` = ``sub`` = the site's Connected-site name, ``aud`` = the exact URL, 60
    seconds, single-use ``jti``. ``workspace`` / ``agent`` name the tenant exactly as
    ``canopy_sdk.site_agents.request_assertion`` does."""
    name = str(site_name or "").strip()
    if not name:
        raise SignOffError(NOT_CONFIGURED, "the request needs the site's Connected-site name")
    key = load_private_key(site_key, what="the site signing key")
    issued = int(time.time() if now is None else now)
    claims = {"iss": name, "sub": name, "aud": audience, "iat": issued,
              "exp": issued + REQUEST_MAX_LIFETIME, "jti": new_jti()}
    if ask is not None:
        claims[ASK_CLAIM] = dict(ask)
    agent = contract.parse_agent_claim(agent or None)
    if agent:
        claims[contract.AGENT_CLAIM] = agent
    workspace = contract.parse_workspace_claim(workspace or None)
    if workspace:
        claims[contract.WORKSPACE_CLAIM] = workspace
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"], "typ": REQUEST_TYP})


@dataclass(frozen=True)
class SignOffRequest:
    """canopy's answer to an ask: where to send the approver, and until when."""

    id: str
    approve_url: str
    expires_at: str
    #: ``SUBJECT`` (the subject approves their own) or ``AGENT_ADMINS``.
    approvers: str


@dataclass(frozen=True)
class SignOffStatus:
    id: str
    #: One of ``STATES``.
    state: str
    expires_at: str
    #: The approval (a compact JWS) while ``state`` is approved and it has not expired.
    approval: str | None = None


def _refusal(status: int, raw: bytes) -> SignOffError:
    try:
        detail = str((json.loads(raw or b"{}") or {}).get("detail") or "")[:300]
    except ValueError:
        detail = ""
    code, sep, _ = detail.partition(":")
    code = code.strip()
    known = sep and code and len(code) <= 64 and all(c.islower() or c.isdigit() or c == "_" for c in code)
    return SignOffError(code if known else REFUSED, detail or f"HTTP {status}", status=status)


def request_sign_off(canopy_url: str, site_name: str, site_key, *, subject: str, action_key: str,
                     title: str, site_ref: str, return_url: str, detail: str = "",
                     workspace: str = "", agent: str = "",
                     post: Callable[..., tuple] | None = None) -> SignOffRequest:
    """Ask canopy for ``subject``'s approval of one action. Raises ``SignOffError``:
    ``no_approver`` when canopy knows nobody who may approve for that subject."""
    from . import fetch

    url = create_url(canopy_url)
    ask = ask_payload(subject=subject, action_key=action_key, title=title, site_ref=site_ref,
                      return_url=return_url, detail=detail)
    try:
        token = request_assertion(site_name, site_key, url, ask=ask, workspace=workspace, agent=agent)
    except (ValueError, ContractError) as exc:
        raise SignOffError(NOT_CONFIGURED, str(exc)) from exc
    sender = post or fetch.post_json
    try:
        status, raw, _headers = sender(url, {}, headers={"Authorization": f"Bearer {token}"})
    except fetch.FetchError as exc:
        raise SignOffError(UNREACHABLE, str(exc)) from exc
    if status >= 400:
        raise _refusal(status, raw)
    try:
        body = json.loads(raw or b"{}")
        return SignOffRequest(id=str(body["id"]), approve_url=str(body["approve_url"]),
                              expires_at=str(body["expires_at"]), approvers=str(body["approvers"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise SignOffError("malformed", "canopy's answer is not a sign-off request") from exc


def fetch_status(canopy_url: str, site_name: str, site_key, sign_off_id: str, *,
                 workspace: str = "", agent: str = "",
                 get: Callable[..., str] | None = None) -> SignOffStatus:
    """Read one request this site made: its state and, once approved, the approval
    (still to be checked with ``verify_approval``)."""
    from . import fetch

    url = status_url(canopy_url, sign_off_id)
    try:
        token = request_assertion(site_name, site_key, url, workspace=workspace, agent=agent)
    except (ValueError, ContractError) as exc:
        raise SignOffError(NOT_CONFIGURED, str(exc)) from exc
    getter = get or fetch.get_text
    try:
        raw = getter(url, headers={"Authorization": f"Bearer {token}"}, accept="application/json")
    except fetch.FetchError as exc:
        status = getattr(exc, "status", None)
        if status is None:
            raise SignOffError(UNREACHABLE, str(exc)) from exc
        detail = getattr(exc, "detail", "") or ""
        raise _refusal(status, json.dumps({"detail": detail}).encode()) from exc
    try:
        body = json.loads(raw)
        state = str(body["state"])
        if state not in STATES:
            raise ValueError(state)
        return SignOffStatus(id=str(body["id"]), state=state, expires_at=str(body.get("expires_at") or ""),
                             approval=body.get("approval") or None)
    except (ValueError, KeyError, TypeError) as exc:
        raise SignOffError("malformed", "canopy's answer is not a sign-off status") from exc


# --- canopy checks the request, and signs the approval ---------------------------


def verify_request(token: str, keys: Sequence, *, audiences: Iterable[str], asking: bool,
                   spend_jti: Callable[[dict], None] | None = None,
                   algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS,
                   leeway: int = contract.LEEWAY_SECONDS) -> dict:
    """canopy's check of a site's request against that site's ``keys``: OUR algorithm
    list, ``typ``, ``aud`` one of ``audiences`` (the exact URL asked), the life capped
    at 60s, ``sub`` = ``iss``, a single-use ``jti`` (``spend_jti``), and — when
    ``asking`` — a well-formed ``sign_off_request`` (normalized in the returned claims).
    Raises ``SignOffError``."""
    import jwt

    try:
        header = unverified_header(token, algorithms=algorithms)
    except ContractError as exc:
        raise SignOffError(exc.code, exc.message) from exc
    if header.get("typ") != REQUEST_TYP:
        raise SignOffError("bad_typ", f"a sign-off request must have typ {REQUEST_TYP}")
    wanted = [a for a in (normalize_url(x) for x in audiences) if a]
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(token, key, algorithms=list(algorithms), audience=wanted, leeway=leeway,
                                options={"require": list(REQUEST_REQUIRED_CLAIMS),
                                         "verify_aud": True, "verify_exp": True})
            break
        except jwt.InvalidAudienceError as exc:
            raise SignOffError("wrong_audience", "the request is addressed to a different URL") from exc
        except jwt.ExpiredSignatureError as exc:
            raise SignOffError("expired", "the request has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise SignOffError("incomplete", f"the request is missing {exc.claim!r}") from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise SignOffError("bad_signature", f"no key registered for this site verifies the request ({last})")
    try:
        contract.check_lifetime(claims, REQUEST_MAX_LIFETIME, leeway=leeway, what="a sign-off request")
        contract.check_jti(claims)
    except ContractError as exc:
        raise SignOffError(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("sub") or ""), str(claims.get("iss") or "")):
        raise SignOffError("bad_subject", "a site speaks only for itself (sub must equal iss)")
    if asking:
        ask = claims.get(ASK_CLAIM)
        if not isinstance(ask, Mapping) or not isinstance(ask.get("action"), Mapping):
            raise SignOffError("malformed", f"the request carries no {ASK_CLAIM}")
        action = ask["action"]
        claims[ASK_CLAIM] = ask_payload(subject=ask.get("subject"), action_key=action.get("key"),
                                        title=action.get("title"), detail=action.get("detail"),
                                        site_ref=ask.get("site_ref"), return_url=ask.get("return_url"))
    elif ASK_CLAIM in claims:
        raise SignOffError("malformed", f"a status read carries no {ASK_CLAIM}")
    if spend_jti is not None:
        spend_jti(claims)
    return claims


def sign_approval(key, client_id: str, site_name: str, *, subject: str, sign_off: Mapping,
                  now: float | None = None, lifetime: int = APPROVAL_LIFETIME, jti: str = "") -> str:
    """canopy's approval: ``iss`` = its client_id, ``aud`` = the site, ``sub`` = the
    subject, a ``jti``, and ``sign_off`` (``id``, ``action_key``, ``site_ref``,
    ``approver`` ``{email, name}``, ``approved_at``, ``step_up``), signed by canopy's
    client key. ``jti`` (and ``now``) fixed by the caller make every copy of one
    approval the same approval, so a site that spends the jti spends them all."""
    if not 0 < lifetime <= APPROVAL_LIFETIME:
        raise ValueError(f"an approval lives 1..{APPROVAL_LIFETIME}s")
    issued = int(time.time() if now is None else now)
    claims = {"iss": client_id, "aud": site_name, "sub": subject, "iat": issued,
              "exp": issued + lifetime, "jti": jti or new_jti(), "sign_off": dict(sign_off)}
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"], "typ": APPROVAL_TYP})


# --- the site checks the approval --------------------------------------------------


@dataclass(frozen=True)
class Approval:
    id: str
    action_key: str
    site_ref: str
    #: Whose approval was asked for: the account the site acts for.
    subject: str
    approver_email: str
    approver_name: str
    approved_at: str
    step_up: str
    #: Spend it once: an approval completes one action.
    jti: str
    expires_at: int


def verify_approval(token: str, *, client_id: str, site_name: str, client_keys,
                    expect_action: str, expect_site_ref: str, expect_subject: str | None = None) -> Approval:
    """Check canopy's approval and return it; ``SignOffError`` otherwise.

    ``client_keys`` is a ``canopy_sdk.host.ClientKeyResolver``. Checked, all
    fail-closed: ``typ``, the signature against the key canopy's client metadata
    names, ``iss`` = ``client_id``, ``aud`` = this site, ``exp`` (and a life of at most
    ten minutes), the action, the ``site_ref``, a passkey step-up, and — when given —
    the subject. The caller still spends ``jti`` once."""
    from .host.client_keys import MetadataError

    try:
        header = unverified_header(token)
        if header.get("typ") != APPROVAL_TYP:
            raise ContractError("bad_typ", f"not a canopy sign-off (typ {APPROVAL_TYP})")
        jwk = client_keys.key_for(client_id, header.get("kid"))
        key = public_key_for(jwk, header["alg"])
        claims = decode(token, key, algorithms=[header["alg"]], audience=[site_name],
                        required=APPROVAL_REQUIRED_CLAIMS, leeway=contract.LEEWAY_SECONDS)
        contract.check_lifetime(claims, APPROVAL_LIFETIME, what="a sign-off")
        contract.check_jti(claims)
    except MetadataError as exc:
        raise SignOffError("client_metadata", f"canopy's keys could not be read: {exc}") from exc
    except ContractError as exc:
        raise SignOffError(exc.code, exc.message) from exc
    if not constant_time_equal(str(claims.get("iss") or ""), client_id):
        raise SignOffError("bad_issuer", "the sign-off is not from the canopy this site trusts")
    body = claims.get("sign_off")
    if not isinstance(body, Mapping):
        raise SignOffError("malformed", "the sign-off carries no sign_off")
    if not constant_time_equal(str(body.get("action_key") or ""), expect_action):
        raise SignOffError("wrong_action", "the sign-off approves a different action")
    if not constant_time_equal(str(body.get("site_ref") or ""), expect_site_ref):
        raise SignOffError("wrong_site_ref", "the sign-off is for a different pending action")
    if body.get("step_up") != STEP_UP_PASSKEY:
        raise SignOffError("no_step_up", "the sign-off does not say a passkey confirmed it")
    subject = str(claims.get("sub") or "")
    if expect_subject is not None and not constant_time_equal(subject.lower(), expect_subject.strip().lower()):
        raise SignOffError("wrong_subject", "the sign-off is for a different account")
    approver = body.get("approver") if isinstance(body.get("approver"), Mapping) else {}
    return Approval(id=str(body.get("id") or ""), action_key=str(body["action_key"]),
                    site_ref=str(body["site_ref"]), subject=subject,
                    approver_email=str(approver.get("email") or ""),
                    approver_name=str(approver.get("name") or ""),
                    approved_at=str(body.get("approved_at") or ""), step_up=STEP_UP_PASSKEY,
                    jti=str(claims["jti"]), expires_at=int(claims["exp"]))
