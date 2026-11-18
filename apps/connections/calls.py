"""What a screen may ask a connection to do: the allowlist, and where a call goes.

A view or app page calls a person's Salesforce (or the viewer's own Google Drive)
through canopy-web, which attaches the token. This module is the boundary: `match` says whether a method and path is
on the list (and whether it is a read or a write), and `build_url` makes the one
URL a call goes to. Both refuse anything that is not plainly a short path of
ordinary names, before any token is read, so a path can never move the call to
another host or another part of the API.

`path` is relative to the API root: Salesforce's `services/data/<SF_API_VERSION>/`
(for example `query` or `sobjects/Lead/00Q…`), Google's `DRIVE_API_ROOT` (for
example `files` or `files/<id>/listLabels`). The host never comes from `path`:
for Salesforce it is the connection's own sealed instance URL, checked by the
provider's host rule; for Google it is fixed here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote, urlencode

from .providers.salesforce import _instance_url

# The API version the org uses (the extension's SF_API_VERSION). One place.
SF_API_VERSION = "v65.0"
# Google has one host for every account: the only root a screen's Google call can reach.
DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"

Kind = Literal["read", "write"]

MAX_PATH_CHARS = 512
MAX_SEGMENTS = 12


class NotAllowed(Exception):  # noqa: N818 (the name the plan and callers use)
    """The call is not one a screen may make. The message is a plain sentence."""


class NotSupported(NotAllowed):
    """This provider has no screen calls yet."""


@dataclass(frozen=True)
class Rule:
    method: str
    pattern: str
    kind: Kind
    name: str


# A segment is ordinary name characters only. Everything that could change the
# meaning of a URL (`/ . ? # % \\ @ : ;` spaces, control characters, non-ASCII) is
# outside it, so a decoded path is safe to join and to match segment by segment.
_SEGMENT = re.compile(r"[A-Za-z0-9_\-]+")

_SF_ID = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
_SF_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SF_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
# A Drive file, folder or shared drive id (shared drives are 19 characters, files up to 44). The
# lower bound also keeps Drive's own words under `files/` (`generateIds`, `trash`) from fitting.
_DRIVE_ID = re.compile(r"[A-Za-z0-9_\-]{19,64}")

# Placeholders in a pattern, per provider: a segment that must fit this expression.
_PLACEHOLDERS = {
    "salesforce": {"{type}": _SF_TYPE, "{id}": _SF_ID, "{name}": _SF_NAME},
    "google": {"{id}": _DRIVE_ID},
}
# `...` as the last segment: zero or more further ordinary segments.
_REST = "..."


def _rules(*rows: tuple[str, str, Kind, str]) -> tuple[Rule, ...]:
    return tuple(Rule(*row) for row in rows)


# Salesforce, exactly the list in the projects spec §3. Composite is out. A POST
# to analytics/reports can save a report as well as run one, so it is a write.
SALESFORCE_RULES = _rules(
    ("GET", "query", "read", "query"),
    ("GET", "search", "read", "search"),
    ("GET", "sobjects/{type}/describe", "read", "describe"),
    ("GET", "sobjects/{type}", "read", "sobject"),
    ("GET", "sobjects/{type}/{id}", "read", "sobject"),
    ("POST", "sobjects/{type}", "write", "sobject"),
    ("POST", "sobjects/{type}/{id}", "write", "sobject"),
    ("PATCH", "sobjects/{type}/{id}", "write", "sobject"),
    ("DELETE", "sobjects/{type}/{id}", "write", "sobject"),
    ("GET", "analytics/reports/...", "read", "reports"),
    # Running a report (by id) or its async instances is a read; creating a report
    # is a write. Any other POST under a report id is refused, not a read.
    ("POST", "analytics/reports/{id}", "read", "reports"),
    ("POST", "analytics/reports/{id}/instances", "read", "reports"),
    ("POST", "analytics/reports", "write", "reports"),
    ("GET", "analytics/dashboards/...", "read", "dashboards"),
    ("POST", "actions/custom/flow/{name}", "write", "flow"),
    ("POST", "apexrest/{name}", "write", "apexrest"),
    ("GET", "chatter/users/me", "read", "me"),
    ("GET", "limits", "read", "limits"),
    ("GET", "ui-api/...", "read", "ui-api"),
)

# Google: Drive v3 file metadata and labels, nothing else (no content, no sharing, no Gmail or
# Calendar). Only the viewer's own account (`VIEWER_ONLY`), and only its short-lived access
# (`TEMPORARY_ONLY`).
GOOGLE_RULES = _rules(
    ("GET", "files", "read", "files"),
    ("GET", "files/{id}", "read", "files"),
    ("GET", "files/{id}/listLabels", "read", "labels"),
    ("POST", "files/{id}/modifyLabels", "write", "labels"),
)

_RULES = {"salesforce": SALESFORCE_RULES, "google": GOOGLE_RULES}

# Providers whose screen calls may use only the viewer's own connection, never an agent's shared one.
VIEWER_ONLY = frozenset({"google"})
# Providers whose screen calls run only on the viewer's short-lived access for screens
# (`temporary.for_screens`: no refresh token, about an hour), never on a stored connection.
TEMPORARY_ONLY = frozenset({"google"})

# The query names a Google call may carry, exactly as spelled. Everything else is refused: `alt`
# (`alt=media` turns a metadata read into a download of the file), `uploadType`, `access_token`,
# `oauth_token`, `key`, `callback`, `$.xgafv` and the like.
GOOGLE_QUERY_NAMES = frozenset({
    "q", "pageSize", "pageToken", "fields", "orderBy", "spaces", "corpora", "driveId",
    "supportsAllDrives", "includeItemsFromAllDrives", "includeLabels", "maxResults",
})


# Objects a screen may read but never write, whatever a view declares: who people
# are, what they may do, and code. Compared lower-case.
GUARDED_OBJECTS = frozenset(n.lower() for n in (
    "User", "UserRole", "Profile", "PermissionSet", "PermissionSetAssignment", "PermissionSetGroup",
    "PermissionSetLicenseAssign", "Group", "GroupMember", "AuthSession", "ConnectedApplication",
    "ApexClass", "ApexTrigger", "ApexPage", "ApexComponent", "Organization", "SetupEntityAccess",
    "MutingPermissionSet", "PermissionSetGroupComponent", "ObjectPermissions", "FieldPermissions", "LoginIp", "UserLogin",
))

_REFUSED_PATH = "That is not a path this screen may call."


@dataclass(frozen=True)
class Match:
    """An allowed call: the rule, and the decoded path to compare and to call.
    Send `rule.method`, never the caller's own spelling of it."""
    rule: Rule
    path: str


def _segments(path: object) -> list[str]:
    """The path's decoded segments, or NotAllowed. An encoded slash is refused
    outright; the rest is decoded once, and what is left must be plain: no empty
    segment (so no leading, trailing or double slash, no `https://`), no `%` left
    over (a double encoding), and only ordinary name characters (so no `.`/`..`,
    `?`, `#`, `\\`, control character, `@` or `:`)."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise NotAllowed(_REFUSED_PATH)
    if re.search(r"%(?:2f|5c)", path, re.IGNORECASE):
        raise NotAllowed("That is not a path this screen may call: encoded slashes are not allowed.")
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeDecodeError:
        raise NotAllowed(_REFUSED_PATH) from None
    segments = decoded.split("/")
    if len(segments) > MAX_SEGMENTS or "%" in decoded or not all(_SEGMENT.fullmatch(s) for s in segments):
        raise NotAllowed("That is not a path this screen may call: paths are plain names "
                         "separated by slashes, with no dots, queries or encoded characters.")
    return segments


def check_query(query: object, provider: str = "salesforce") -> dict[str, str | list[str]]:
    """The query as a plain dict of str -> str | list[str], or NotAllowed.
    `_HttpMethod` is refused (any case): Salesforce honours it as a method
    override, which would turn a declared POST into a PATCH or DELETE. A Google
    query may only use `GOOGLE_QUERY_NAMES`; its values are any text (a Drive `q`
    such as `'<id>' in parents and trashed = false`), encoded into the query only."""
    if query is None:
        return {}
    if not isinstance(query, dict):
        raise NotAllowed("The query must be a set of names and text values.")
    for key, value in query.items():
        if not isinstance(key, str) or not (isinstance(value, str) or (
                isinstance(value, list) and all(isinstance(v, str) for v in value))):
            raise NotAllowed("The query must be a set of names and text values.")
        if key.strip().lower() == "_httpmethod":
            raise NotAllowed("Screens may not change the method of a call.")
        if provider == "google" and key not in GOOGLE_QUERY_NAMES:
            raise NotAllowed(f"Screens may not send {key[:40]!r} to Google.")
    return dict(query)


def _fits(pattern: str, segments: list[str], placeholders: dict[str, re.Pattern]) -> bool:
    want = pattern.split("/")
    rest = want[-1] == _REST
    if rest:
        want = want[:-1]
    if len(segments) < len(want) or (not rest and len(segments) != len(want)):
        return False
    for token, seg in zip(want, segments):
        expr = placeholders.get(token)
        if not (expr.fullmatch(seg) if expr else token == seg):
            return False
    return True


def _rules_for(provider: object) -> tuple[Rule, ...]:
    if not isinstance(provider, str):
        raise NotSupported("That kind of connection cannot be called from a screen.")
    try:
        return _RULES[provider]
    except KeyError:
        raise NotSupported("That kind of connection cannot be called from a screen.") from None


def match(provider: str, method: str, path: str) -> Match:
    """The allowed call (rule and decoded path), or NotAllowed with a sentence.
    `path` is refused unless it is plain, before any rule is looked at."""
    rules = _rules_for(provider)
    segments = _segments(path)
    if not (isinstance(method, str) and method.isascii()):
        raise NotAllowed("Screens may not use that method.")
    verb = method.upper()
    shape_known = False
    placeholders = _PLACEHOLDERS[provider]
    for rule in rules:
        if _fits(rule.pattern, segments, placeholders):
            shape_known = True
            if rule.method == verb:
                if rule.kind == "write" and rule.name == "sobject" and segments[1].lower() in GUARDED_OBJECTS:
                    raise NotAllowed("Screens may not change people, permissions or code in Salesforce.")
                return Match(rule, "/".join(segments))
    if shape_known:
        raise NotAllowed("Screens may not use that method on this path.")
    raise NotAllowed("Screens may not call that path.")


def build_url(provider: str, instance_url: str, path: str, query: object, *, method: str) -> str:
    """The one URL the call goes to. The call must pass `match`; the host is the
    connection's own sealed instance URL, which must pass the provider's host
    rule (Google: always `DRIVE_API_ROOT`, and the connection names none); the
    decoded path is only ever appended, as quoted plain segments, and
    `query` is checked and encoded."""
    allowed = match(provider, method, path)
    params = check_query(query, provider)
    segments = allowed.path.split("/")
    quoted = "/".join(quote(s, safe="") for s in segments)
    if provider == "google":
        # Google's host is fixed; a connection naming one of its own is not a Google connection.
        if instance_url not in (None, ""):
            raise NotAllowed("This connection's Google address cannot be used.")
        url = f"{DRIVE_API_ROOT}/{quoted}"
        return url + ("?" + urlencode(params, doseq=True) if params else "")
    base, _host = _instance_url(instance_url if isinstance(instance_url, str) else "")
    if not base:
        raise NotAllowed("This connection's Salesforce address cannot be used.")
    if segments[0] == "apexrest":
        url = f"{base}/services/{quoted}"
    else:
        url = f"{base}/services/data/{SF_API_VERSION}/{quoted}"
    if params:
        url += "?" + urlencode(params, doseq=True)
    return url
