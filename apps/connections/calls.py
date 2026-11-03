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
from collections.abc import Mapping
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
# meaning of a URL (`/ ? # % \ @ : ;` spaces, control characters, non-ASCII) is
# outside it, so a decoded path is safe to join and to match segment by segment.
_SEGMENT = re.compile(r"[A-Za-z0-9_\-.]+")

_SF_ID = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
_SF_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SF_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")

# Placeholders in a pattern: a segment that must fit this expression.
_PLACEHOLDERS = {"{type}": _SF_TYPE, "{id}": _SF_ID, "{name}": _SF_NAME, "*": _SEGMENT}
# `...` as the last segment: zero or more further ordinary segments.
_REST = "..."


def _rules(*rows: tuple[str, str, Kind, str]) -> tuple[Rule, ...]:
    return tuple(Rule(*row) for row in rows)


# Salesforce, exactly the list in the projects spec §3. Composite is out. A POST
# to analytics/reports can save a report as well as run one, so it is a write.
SALESFORCE_RULES = _rules(
    ("GET", "query", "read", "query"),
    ("GET", "search", "read", "search"),
    ("GET", "sobjects/*/describe", "read", "describe"),
    ("GET", "sobjects/{type}", "read", "sobject"),
    ("GET", "sobjects/{type}/{id}", "read", "sobject"),
    ("POST", "sobjects/{type}", "write", "sobject"),
    ("POST", "sobjects/{type}/{id}", "write", "sobject"),
    ("PATCH", "sobjects/{type}/{id}", "write", "sobject"),
    ("DELETE", "sobjects/{type}/{id}", "write", "sobject"),
    ("GET", "analytics/reports/...", "read", "reports"),
    ("POST", "analytics/reports/...", "write", "reports"),
    ("GET", "analytics/dashboards/...", "read", "dashboards"),
    ("POST", "actions/custom/flow/{name}", "write", "flow"),
    ("POST", "apexrest/{name}", "write", "apexrest"),
    ("GET", "chatter/users/me", "read", "me"),
    ("GET", "limits", "read", "limits"),
    ("GET", "ui-api/...", "read", "ui-api"),
)

_RULES = {"salesforce": SALESFORCE_RULES}


def _segments(path: object) -> list[str]:
    """The path's decoded segments, or NotAllowed. Decoded once; what is left must
    be plain: no `.`/`..`, no empty segment (so no leading, trailing or double
    slash, no `https://`), no `%` left over (a double encoding), and only ordinary
    name characters (so no `?`, `#`, `\\`, control character, `@` or `:`)."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise NotAllowed("That is not a path this screen may call.")
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeDecodeError:
        raise NotAllowed("That is not a path this screen may call.") from None
    segments = decoded.split("/")
    if (len(segments) > MAX_SEGMENTS or "%" in decoded
            or any(s in ("", ".", "..") or not _SEGMENT.fullmatch(s) for s in segments)):
        raise NotAllowed("That is not a path this screen may call: paths are plain names "
                         "separated by slashes, with no dots, queries or encoded characters.")
    return segments


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


def _rules_for(provider: str) -> tuple[Rule, ...]:
    if provider == "google":
        raise NotSupported("Google calls from screens are not available yet.")
    try:
        return _RULES[provider]
    except KeyError:
        raise NotSupported("That kind of connection cannot be called from a screen.") from None


def match(provider: str, method: str, path: str) -> Rule:
    """The rule that allows this call, or NotAllowed with a sentence. `path` is
    refused unless it is plain, before any rule is looked at."""
    rules = _rules_for(provider)
    segments = _segments(path)
    verb = method.upper() if isinstance(method, str) else ""
    shape_known = False
    for rule in rules:
        if _fits(rule.pattern, segments):
            shape_known = True
            if rule.method == verb:
                return rule
    if shape_known:
        raise NotAllowed("Screens may not use that method on this path.")
    raise NotAllowed("Screens may not call that path.")


def build_url(provider: str, instance_url: str, path: str, query: Mapping[str, object] | None) -> str:
    """The one URL the call goes to. The host is the connection's own sealed
    instance URL, which must pass the provider's host rule; `path` is only ever
    appended, as quoted plain segments, and `query` is encoded."""
    _rules_for(provider)
    segments = _segments(path)
    base, _host = _instance_url(instance_url if isinstance(instance_url, str) else "")
    if not base:
        raise NotAllowed("This connection's Salesforce address cannot be used.")
    quoted = "/".join(quote(s, safe="") for s in segments)
    if segments[0] == "apexrest":
        url = f"{base}/services/{quoted}"
    else:
        url = f"{base}/services/data/{SF_API_VERSION}/{quoted}"
    if query:
        url += "?" + urlencode(query, doseq=True)
    return url
