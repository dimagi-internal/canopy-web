"""A host site as the system of record for the people it brings to canopy.

A host (Connect Labs, HQ, ace-web…) owns its people. canopy keeps, per person, an
anchor (its ``Contact``, keyed by the host's own id), a POINTER to where the host's
record lives (``record_uri``, under the scheme the host declares in ``site.json`` as
``site.person_scheme``) and a small CACHE the host keeps current (``profile``, a few
labels an agent may see on every turn). The host writes those three fields and nothing
else, and it can make canopy forget a person.

Both halves of the wire live here so canopy and every host read ONE definition:

* "here is what I know about these people": the host signs ``update_token`` with its own
  key (the one at its ``jwks_url``) and POSTs it, as the body (``Content-Type:
  application/jwt``), to canopy's ``UPDATE_PATH``; canopy checks it with
  ``verify_update``. Each item is ``{sub, display_name?, record_uri?, profile?,
  version}``; canopy creates the contact if it is missing and overwrites the host's
  fields only when ``version`` is newer than the one it holds (else ``stale``).
* "forget these people": ``erase_token`` → ``ERASE_PATH``, checked by ``verify_erase``.
  Each ``sub`` is ``erased`` or ``not_found``; erasing twice is ``not_found`` the second
  time and changes nothing.

``canopy_sdk.host.contacts`` (``update_contacts`` / ``erase_contacts``) sends them with a
host's ``HostConfig``.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence

from . import contract
from .contract import ContractError, normalize_url
from .jose import new_jti, sign, unverified_header
from .keys import load_private_key, public_jwk

#: Where canopy takes a host's contact updates, and its erasures, under its base URL.
UPDATE_PATH = "/api/sites/contacts"
ERASE_PATH = "/api/sites/contacts/erase"
#: The body's media type: the compact JWS itself.
MEDIA_TYPE = "application/jwt"
#: ``typ`` of each: distinct from every other token either side signs, so none can be
#: replayed as another.
UPDATE_TYP = "canopy-contact-update+jwt"
ERASE_TYP = "canopy-contact-erase+jwt"
#: Each is used once, at the moment of the POST.
MAX_LIFETIME = 60
#: The most people one request names.
MAX_CONTACTS = 100
#: A person's id at the host (canopy's ``Contact.external_id``).
MAX_SUB_LENGTH = 200
MAX_DISPLAY_NAME_LENGTH = 200
#: The host's cache of a person: labels an agent may see every turn (stage, language,
#: programme, consent), never history. Bounded so it cannot become a place for history.
MAX_PROFILE_BYTES = 4096
MAX_PROFILE_KEYS = 40
#: The pointer to the host's record: ``<person_scheme>://<id>``, the id url-safe.
MAX_RECORD_URI_LENGTH = 256
_RECORD_ID = re.compile(r"^[A-Za-z0-9_-]+$")
#: A version is a whole number the host increases with each change it sends.
MAX_VERSION = 2 ** 63 - 1

UPDATE_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "aud", "iat", "exp", "jti", "contacts")
ERASE_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "aud", "iat", "exp", "jti", "subs")

#: What canopy answers for each item.
CREATED, UPDATED, STALE, REFUSED = "created", "updated", "stale", "refused"
ERASED, NOT_FOUND = "erased", "not_found"


class ContactRecordError(Exception):
    """A request, an item or a field that is not one. ``code`` names why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- fields -------------------------------------------------------------------------


def check_sub(value) -> str:
    """``value`` as a person's id at the host: a non-empty string of at most
    ``MAX_SUB_LENGTH`` characters with no control characters or surrounding space."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_SUB_LENGTH \
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ContactRecordError("bad_sub", f"sub must be the host's id for the person (1-{MAX_SUB_LENGTH} "
                                            "characters, no control characters or surrounding space)")
    return value


def check_record_uri(uri, scheme: str) -> str:
    """``uri`` if it points at a person under ``scheme`` (the site's ``person_scheme``):
    ``<scheme>://<id>``, the id url-safe (letters, digits, ``-``, ``_``), at most
    ``MAX_RECORD_URI_LENGTH`` characters in all. Raises ``ContactRecordError``."""
    from .resources import ResourceError, check_scheme

    if not scheme:
        raise ContactRecordError("no_person_scheme", "the site declares no person_scheme in its site.json, "
                                                     "so canopy keeps no record_uri for it")
    try:
        scheme = check_scheme(scheme)
    except ResourceError as exc:
        raise ContactRecordError("no_person_scheme", f"the site's person_scheme is not one: {exc.message}") from exc
    if not isinstance(uri, str) or not uri or len(uri) > MAX_RECORD_URI_LENGTH:
        raise ContactRecordError("bad_record_uri", f"record_uri must be at most {MAX_RECORD_URI_LENGTH} characters")
    prefix = f"{scheme}://"
    if not uri.lower().startswith(prefix):
        raise ContactRecordError("wrong_scheme", f"record_uri must be under the site's person_scheme ({prefix})")
    rid = uri[len(prefix):]
    if not _RECORD_ID.match(rid):
        raise ContactRecordError("bad_record_uri", "record_uri's id must be letters, digits, '-' or '_' only")
    return prefix + rid


def check_profile(value) -> dict:
    """``value`` as a host profile: a JSON object of at most ``MAX_PROFILE_KEYS`` top-level
    keys and ``MAX_PROFILE_BYTES`` bytes serialized. Raises ``ContactRecordError``."""
    if not isinstance(value, Mapping):
        raise ContactRecordError("bad_profile", "profile must be a JSON object")
    if len(value) > MAX_PROFILE_KEYS:
        raise ContactRecordError("profile_too_large", f"profile has more than {MAX_PROFILE_KEYS} keys")
    if not all(isinstance(k, str) and k for k in value):
        raise ContactRecordError("bad_profile", "every profile key must be a non-empty string")
    try:
        size = len(profile_bytes(value))
    except (TypeError, ValueError) as exc:
        raise ContactRecordError("bad_profile", "profile must be plain JSON") from exc
    if size > MAX_PROFILE_BYTES:
        raise ContactRecordError("profile_too_large", f"profile is {size} bytes serialized; at most "
                                                      f"{MAX_PROFILE_BYTES}")
    return dict(value)


def profile_bytes(value: Mapping) -> bytes:
    """How a profile is measured: compact JSON, UTF-8, keys sorted."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
                      allow_nan=False).encode()


def check_version(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_VERSION:
        raise ContactRecordError("bad_version", "version must be a whole number of at least 1 that the host "
                                                "increases with every change")
    return value


def check_item(item, *, person_scheme: str | None = None) -> dict:
    """One update item, normalized: ``{sub, version, display_name?, record_uri?, profile?}``.
    A field left out (or null) is left as canopy holds it; ``record_uri: ""`` and
    ``profile: {}`` clear one. ``person_scheme`` None skips the ``record_uri`` scheme check (the host's own builder,
    which may not know what its site.json says); canopy always passes it."""
    if not isinstance(item, Mapping):
        raise ContactRecordError("malformed", "each contact must be a mapping")
    extra = set(item) - {"sub", "display_name", "record_uri", "profile", "version"}
    if extra:
        raise ContactRecordError("malformed", f"unknown key(s) {sorted(extra)}: a host writes only "
                                              "display_name, record_uri and profile")
    out = {"sub": check_sub(item.get("sub")), "version": check_version(item.get("version"))}
    if "display_name" in item and item["display_name"] is not None:
        name = item["display_name"]
        if not isinstance(name, str) or len(name) > MAX_DISPLAY_NAME_LENGTH \
                or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ContactRecordError("bad_display_name", f"display_name must be one line of at most "
                                                         f"{MAX_DISPLAY_NAME_LENGTH} characters")
        out["display_name"] = name.strip()
    if item.get("record_uri") == "":
        out["record_uri"] = ""   # the host no longer names a record: cleared
    elif "record_uri" in item and item["record_uri"] is not None:
        if person_scheme is None:
            uri = item["record_uri"]
            if not isinstance(uri, str) or not uri or len(uri) > MAX_RECORD_URI_LENGTH or "://" not in uri:
                raise ContactRecordError("bad_record_uri", "record_uri must be <person_scheme>://<id>")
            out["record_uri"] = uri
        else:
            out["record_uri"] = check_record_uri(item["record_uri"], person_scheme)
    if "profile" in item and item["profile"] is not None:
        out["profile"] = check_profile(item["profile"])
    return out


def _items(value) -> list:
    if not isinstance(value, (list, tuple)) or not value:
        raise ContactRecordError("malformed", "contacts must be a non-empty list")
    if len(value) > MAX_CONTACTS:
        raise ContactRecordError("too_many", f"at most {MAX_CONTACTS} contacts per request")
    return list(value)


def _subs(value) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ContactRecordError("malformed", "subs must be a non-empty list")
    if len(value) > MAX_CONTACTS:
        raise ContactRecordError("too_many", f"at most {MAX_CONTACTS} subs per request")
    out: list[str] = []
    for sub in value:
        sub = check_sub(sub)
        if sub not in out:
            out.append(sub)
    return out


# --- the host's half: build the signed request ----------------------------------------


def update_url(canopy_url: str) -> str:
    """Where the canopy at ``canopy_url`` takes contact updates — their ``aud``."""
    return normalize_url(canopy_url) + UPDATE_PATH


def erase_url(canopy_url: str) -> str:
    return normalize_url(canopy_url) + ERASE_PATH


def _sign(site_name: str, site_key, audience: str, typ: str, body: dict, *, workspace: str,
          now: float | None) -> str:
    name = str(site_name or "").strip()
    if not name:
        raise ContactRecordError("not_configured", "the request needs the site's host-site name")
    key = load_private_key(site_key, what="the site signing key")
    issued = int(time.time() if now is None else now)
    claims = {"iss": name, "aud": audience, "iat": issued, "exp": issued + MAX_LIFETIME,
              "jti": new_jti(), **body}
    workspace = contract.parse_workspace_claim(workspace or None)
    if workspace:
        claims[contract.WORKSPACE_CLAIM] = workspace
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"], "typ": typ})


def update_token(site_name: str, site_key, audience: str, contacts: Sequence[Mapping], *,
                 workspace: str = "", now: float | None = None) -> str:
    """The host's signed contact update: ``iss`` = its host-site name, ``aud`` =
    ``update_url``, 60 seconds, single-use ``jti``, ``contacts`` (1-100 items, each checked
    by ``check_item``). ``workspace`` names the tenant as ``canopy_workspace``."""
    items = [check_item(i) for i in _items(list(contacts))]
    return _sign(site_name, site_key, audience, UPDATE_TYP, {"contacts": items}, workspace=workspace, now=now)


def erase_token(site_name: str, site_key, audience: str, subs: Iterable[str], *, workspace: str = "",
                now: float | None = None) -> str:
    """The host's signed erasure: as ``update_token``, with ``subs`` (1-100 ids)."""
    return _sign(site_name, site_key, audience, ERASE_TYP, {"subs": _subs(list(subs))},
                 workspace=workspace, now=now)


# --- canopy's half: verify it ---------------------------------------------------------


def _verify(token: str, keys: Sequence, typ: str, required, *, audiences: Iterable[str],
            algorithms, leeway: int) -> dict:
    import jwt

    try:
        header = unverified_header(token, algorithms=algorithms)
    except ContractError as exc:
        raise ContactRecordError(exc.code, exc.message) from exc
    if header.get("typ") != typ:
        raise ContactRecordError("bad_typ", f"this request must have typ {typ}")
    wanted = [a for a in (normalize_url(x) for x in audiences) if a]
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(token, key, algorithms=list(algorithms), audience=wanted, leeway=leeway,
                                options={"require": list(required), "verify_aud": True, "verify_exp": True})
            break
        except jwt.InvalidAudienceError as exc:
            raise ContactRecordError("wrong_audience", "the request is addressed to a different canopy") from exc
        except jwt.ExpiredSignatureError as exc:
            raise ContactRecordError("expired", "the request has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise ContactRecordError("incomplete", f"the request is missing {exc.claim!r}") from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise ContactRecordError("bad_signature", f"no key registered for this site verifies the request ({last})")
    try:
        contract.check_lifetime(claims, MAX_LIFETIME, leeway=leeway, what="a contact request")
        contract.check_jti(claims)
    except ContractError as exc:
        raise ContactRecordError(exc.code, exc.message) from exc
    return claims


def verify_update(token: str, keys: Sequence, *, audiences: Iterable[str],
                  spend_jti: Callable[[dict], None] | None = None,
                  algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS,
                  leeway: int = contract.LEEWAY_SECONDS) -> dict:
    """canopy's check of a host's contact update against that host's ``keys``: OUR
    algorithm list, ``typ``, ``aud`` one of ``audiences``, the life capped at 60s, a
    single-use ``jti`` (``spend_jti``) and 1-100 ``contacts``. Each item is left for the
    caller to check with ``check_item`` (against the site's ``person_scheme``), so one bad
    item is refused alone rather than the whole request. Returns the claims or raises
    ``ContactRecordError``."""
    claims = _verify(token, keys, UPDATE_TYP, UPDATE_REQUIRED_CLAIMS, audiences=audiences,
                     algorithms=algorithms, leeway=leeway)
    claims["contacts"] = _items(claims.get("contacts"))
    if spend_jti is not None:
        spend_jti(claims)
    return claims


def verify_erase(token: str, keys: Sequence, *, audiences: Iterable[str],
                 spend_jti: Callable[[dict], None] | None = None,
                 algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS,
                 leeway: int = contract.LEEWAY_SECONDS) -> dict:
    """As ``verify_update``, for an erasure: ``subs`` (1-100 ids, de-duplicated)."""
    claims = _verify(token, keys, ERASE_TYP, ERASE_REQUIRED_CLAIMS, audiences=audiences,
                     algorithms=algorithms, leeway=leeway)
    claims["subs"] = _subs(claims.get("subs"))
    if spend_jti is not None:
        spend_jti(claims)
    return claims


# --- sending --------------------------------------------------------------------------


def post(url: str, token: str, *, sender: Callable[..., tuple] | None = None) -> dict:
    """POST ``token`` as the body to ``url``; canopy's JSON answer, or ``ContactRecordError``
    (``unreachable``, or canopy's own refusal code)."""
    from . import fetch

    send = sender or fetch.post_bytes
    try:
        status, raw, _headers = send(url, token.encode("ascii"), content_type=MEDIA_TYPE)
    except fetch.FetchError as exc:
        raise ContactRecordError("unreachable", str(exc)) from exc
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    if status >= 400:
        detail = str((body or {}).get("detail") or f"HTTP {status}")
        code, sep, _ = detail.partition(":")
        raise ContactRecordError(code.strip() if sep and code.strip().isidentifier() else "refused", detail)
    return body if isinstance(body, dict) else {}
