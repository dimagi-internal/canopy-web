"""What a screen may ask a connection to do: the allowlist, and where a call goes.

A view or app page calls a person's Salesforce through canopy-web, which attaches
the token. This module is the boundary: `match` says whether a method and path is
on the list (and whether it is a read or a write), and `build_url` makes the one
URL a call goes to. Both refuse anything that is not plainly a short path of
ordinary names, before any token is read, so a path can never move the call to
another host or another part of the API.

`path` is relative to the API root (`services/data/<SF_API_VERSION>/`), for
example `query` or `sobjects/Lead/00Q…`. The host never comes from `path`: it is
the connection's own sealed instance URL, checked by the provider's host rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote, urlencode

from .providers.salesforce import _instance_url

# The API version the org uses (the extension's SF_API_VERSION). One place.
SF_API_VERSION = "v65.0"

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

# Placeholders in a pattern: a segment that must fit this expression.
_PLACEHOLDERS = {"{type}": _SF_TYPE, "{id}": _SF_ID, "{name}": _SF_NAME}
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

_RULES = {"salesforce": SALESFORCE_RULES}


# Objects a screen may read but never write, whatever a view declares: who people
# are, what they may do, and code. Compared lower-case.
GUARDED_OBJECTS = frozenset(n.lower() for n in (
    "User", "UserRole", "Profile", "PermissionSet", "PermissionSetAssignment", "PermissionSetGroup",
    "PermissionSetLicenseAssign", "Group", "GroupMember", "AuthSession", "ConnectedApplication",
    "ApexClass", "ApexTrigger", "ApexPage", "ApexComponent", "Organization", "SetupEntityAccess",
    "ObjectPermissions", "FieldPermissions", "LoginIp", "UserLogin",
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


def check_query(query: object) -> dict[str, str | list[str]]:
    """The query as a plain dict of str -> str | list[str], or NotAllowed.
    `_HttpMethod` is refused (any case): Salesforce honours it as a method
    override, which would turn a declared POST into a PATCH or DELETE."""
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
    return dict(query)


def _fits(pattern: str, segments: list[str]) -> bool:
    want = pattern.split("/")
    rest = want[-1] == _REST
    if rest:
        want = want[:-1]
    if len(segments) < len(want) or (not rest and len(segments) != len(want)):
        return False
    for token, seg in zip(want, segments):
        expr = _PLACEHOLDERS.get(token)
        if not (expr.fullmatch(seg) if expr else token == seg):
            return False
    return True


def _rules_for(provider: object) -> tuple[Rule, ...]:
    if not isinstance(provider, str):
        raise NotSupported("That kind of connection cannot be called from a screen.")
    if provider == "google":
        raise NotSupported("Google calls from screens are not available yet.")
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
    for rule in rules:
        if _fits(rule.pattern, segments):
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
    rule; the decoded path is only ever appended, as quoted plain segments, and
    `query` is checked and encoded."""
    allowed = match(provider, method, path)
    params = check_query(query)
    base, _host = _instance_url(instance_url if isinstance(instance_url, str) else "")
    if not base:
        raise NotAllowed("This connection's Salesforce address cannot be used.")
    segments = allowed.path.split("/")
    quoted = "/".join(quote(s, safe="") for s in segments)
    if segments[0] == "apexrest":
        url = f"{base}/services/{quoted}"
    else:
        url = f"{base}/services/data/{SF_API_VERSION}/{quoted}"
    if params:
        url += "?" + urlencode(params, doseq=True)
    return url
