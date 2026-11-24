"""What a screen may ask a connection to do: the allowlist, and where a call goes.

A view or app page calls a person's Salesforce (or the viewer's own Google: Drive, Gmail,
Calendar, Sheets, Docs) through canopy-web, which attaches the token. This module is the
boundary: `match` says whether a method and path is on the list (and whether it is a read or
a write), and `build_url` makes the one URL a call goes to. Both refuse anything that is not plainly a short path of
ordinary names, before any token is read, so a path can never move the call to
another host or another part of the API.

`path` is relative to the API root: Salesforce's `services/data/<SF_API_VERSION>/`
(for example `query` or `sobjects/Lead/00Q…`). For Google the first segment names the
API and the rest is relative to that API's fixed root (`GOOGLE_API_ROOTS`): `gmail/messages`
is `https://gmail.googleapis.com/gmail/v1/users/me/messages`, `calendar/calendars/primary/events`,
`sheets/<id>/values:batchGet`, `docs/<id>`; a path naming no API is Drive's, at `DRIVE_API_ROOT`
(`files`, `files/<id>/listLabels`), as it was before the other APIs. The host never comes from
`path`: for Salesforce it is the connection's own sealed instance URL, checked by the provider's
host rule; for Google it is fixed here. A Google custom method (`:batchGet`) is the one place a
`:` may appear, at the end of a segment a rule spells out literally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, unquote, urlencode

from .providers.salesforce import _instance_url

# The API version the org uses (the extension's SF_API_VERSION). One place.
SF_API_VERSION = "v65.0"
# Google has one host per API for every account: the only roots a screen's Google call can reach.
DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"
#: The Google APIs a path may name in its first segment, and the fixed root each is relative to.
#: Gmail's is the viewer's own mailbox (`users/me`): a screen can never name another one.
GOOGLE_API_ROOTS = {
    "gmail": "https://gmail.googleapis.com/gmail/v1/users/me",
    "calendar": "https://www.googleapis.com/calendar/v3",
    "sheets": "https://sheets.googleapis.com/v4/spreadsheets",
    "docs": "https://docs.googleapis.com/v1/documents",
}

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
    #: The declared path that covers this rule, when it is not its own: a query's next page is
    #: covered by the screen's `GET query` (`declarations.allows`), and can't be declared alone.
    declared_as: str | None = None


# A segment is ordinary name characters only. Everything that could change the
# meaning of a URL (`/ . ? # % \\ @ : ;` spaces, control characters, non-ASCII) is
# outside it, so a decoded path is safe to join and to match segment by segment.
_SEGMENT = re.compile(r"[A-Za-z0-9_\-]+")
# A Google segment may end in a custom method (`values:batchGet`, `<id>:batchUpdate`), and a Sheets
# range is A1 notation (`'Pathway Types'!A2:D`), so a Google segment may also hold a space and
# `' ! : $`. Still no `/ . ? # % \\ @`, so nothing in it can move the host or the path; only a rule's
# literal or its `{range}` placeholder can match one of these, and it is quoted when sent.
_GOOGLE_SEGMENT = re.compile(r"[A-Za-z0-9_\-' !:$]{1,200}")

_SF_ID = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
_SF_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SF_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
# A query's next page: the query's id, a dash and the row it starts at (`01gD0000002HU6KIAW-2000`).
_SF_LOCATOR = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?-[0-9]{1,9}")
# A Drive file, folder or shared drive id (shared drives are 19 characters, files up to 44). The
# lower bound also keeps Drive's own words under `files/` (`generateIds`, `trash`) from fitting.
_DRIVE_ID = re.compile(r"[A-Za-z0-9_\-]{19,64}")
# A Gmail message or thread id: lowercase hex (16 today). Gmail's own words under `messages/` and
# `threads/` (`send`, `import`, `batchDelete`, `trash`) are not hex, so they never fit it.
_GMAIL_ID = re.compile(r"[0-9a-f]{8,32}")
# A Sheets range in A1 notation (`Pathways!A:K`, `'Pathway Types'!A2:D100`).
_A1_RANGE = re.compile(r"[A-Za-z0-9_\-' !:$]{1,200}")

# Placeholders in a pattern, per provider: a segment that must fit this expression.
_PLACEHOLDERS = {
    "salesforce": {"{type}": _SF_TYPE, "{id}": _SF_ID, "{name}": _SF_NAME, "{locator}": _SF_LOCATOR},
    "google": {"{id}": _DRIVE_ID, "{gid}": _GMAIL_ID, "{range}": _A1_RANGE},
}
# `{x}:verb` in a pattern: placeholder `{x}` followed by Google's custom method `:verb` (`{id}:batchUpdate`).
# `...` as the last segment: zero or more further ordinary segments.
_REST = "..."


def _rules(*rows: tuple) -> tuple[Rule, ...]:
    return tuple(Rule(*row) for row in rows)


# Salesforce, the list in the projects spec §3, plus a query's next page and the two composite
# calls (`expand`). A POST to analytics/reports can save a report as well as run one, so it is a
# write. A composite rule's kind is only where it starts: what it is, a read or how many writes,
# is the calls inside it (`Composite.writes`), and it is never declared itself.
SALESFORCE_RULES = _rules(
    ("GET", "query", "read", "query"),
    ("GET", "query/{locator}", "read", "query", "query"),
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
    ("POST", "composite/batch", "write", "composite"),
    ("GET", "composite/sobjects/{type}", "read", "composite"),
    ("POST", "composite/sobjects", "write", "composite"),
    ("PATCH", "composite/sobjects", "write", "composite"),
    ("DELETE", "composite/sobjects", "write", "composite"),
)

# Google, the viewer's own account only (`VIEWER_ONLY`), on its short-lived access (`TEMPORARY_ONLY`):
# Drive v3 file metadata and labels (no content, no sharing); reads of Gmail, Calendar, Sheets and
# Docs; and the writes the Sales app's screens make (the chrome-sales port): a Gmail draft (a new
# message or a reply; never a send), Sheets values written or appended and a spreadsheet's own
# batchUpdate, a Doc's batchUpdate, and a new Drive file (Compose's outreach Doc). Every write is a
# click and a lease, as any. Nothing sends mail, shares a file, deletes one or touches a calendar.
# Each API's paths are relative to its root in `GOOGLE_API_ROOTS`.
GOOGLE_RULES = _rules(
    ("GET", "files", "read", "files"),
    ("GET", "files/{id}", "read", "files"),
    ("GET", "files/{id}/listLabels", "read", "labels"),
    ("POST", "files/{id}/modifyLabels", "write", "labels"),
    # A new file's metadata (an empty Doc: Compose's outreach drafts). Never content: no upload root.
    ("POST", "files", "write", "files"),
    # Gmail: the viewer's address, a search, one message, one thread.
    ("GET", "gmail/profile", "read", "gmail"),
    ("GET", "gmail/messages", "read", "gmail"),
    ("GET", "gmail/messages/{gid}", "read", "gmail"),
    ("GET", "gmail/threads/{gid}", "read", "gmail"),
    # A draft (a reply when its message names a threadId). There is no rule to send one.
    ("POST", "gmail/drafts", "write", "gmail"),
    # Calendar: the viewer's primary calendar and its events, nobody else's.
    ("GET", "calendar/calendars/primary", "read", "calendar"),
    ("GET", "calendar/calendars/primary/events", "read", "calendar"),
    # Sheets: a spreadsheet's properties and its values (ranges as a query, so no A1 notation is ever
    # a path segment).
    ("GET", "sheets/{id}", "read", "sheets"),
    ("GET", "sheets/{id}/values:batchGet", "read", "sheets"),
    ("POST", "sheets/{id}/values:batchUpdate", "write", "sheets"),
    ("POST", "sheets/{id}/values/{range}:append", "write", "sheets"),
    ("POST", "sheets/{id}:batchUpdate", "write", "sheets"),
    # Docs: one document.
    ("GET", "docs/{id}", "read", "docs"),
    ("POST", "docs/{id}:batchUpdate", "write", "docs"),
)

_RULES = {"salesforce": SALESFORCE_RULES, "google": GOOGLE_RULES}

# Providers whose screen calls may use only the viewer's own connection, never an agent's shared one.
VIEWER_ONLY = frozenset({"google"})
# Providers whose screen calls run only on the viewer's short-lived access for screens
# (`temporary.for_screens`: no refresh token, about an hour), never on a stored connection.
TEMPORARY_ONLY = frozenset({"google"})

# The query names a Google call may carry, per API, exactly as spelled. Everything else is refused:
# `alt` (`alt=media` turns a metadata read into a download of the file), `uploadType`, `access_token`,
# `oauth_token`, `key`, `callback`, `$.xgafv` and the like. `GOOGLE_QUERY_NAMES` is Drive's.
GOOGLE_QUERY_NAMES = frozenset({
    "q", "pageSize", "pageToken", "fields", "orderBy", "spaces", "corpora", "driveId",
    "supportsAllDrives", "includeItemsFromAllDrives", "includeLabels", "maxResults",
})
GOOGLE_QUERY_NAMES_BY_API = {
    "drive": GOOGLE_QUERY_NAMES,
    "gmail": frozenset({"q", "maxResults", "pageToken", "labelIds", "includeSpamTrash", "format",
                        "metadataHeaders", "fields"}),
    "calendar": frozenset({"q", "timeMin", "timeMax", "singleEvents", "orderBy", "maxResults", "pageToken",
                           "timeZone", "showDeleted", "updatedMin", "iCalUID", "fields"}),
    "sheets": frozenset({"ranges", "majorDimension", "valueRenderOption", "dateTimeRenderOption", "fields",
                         "valueInputOption", "insertDataOption", "includeValuesInResponse",
                         "responseValueRenderOption", "responseDateTimeRenderOption"}),
    "docs": frozenset({"fields", "suggestionsViewMode"}),
}


def google_api(path: object) -> str:
    """Which Google API a path is for: its first segment when that names one, else Drive."""
    first = path.split("/", 1)[0] if isinstance(path, str) else ""
    return first if first in GOOGLE_API_ROOTS else "drive"


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


def _segments(path: object, provider: str = "salesforce") -> list[str]:
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
    plain = _GOOGLE_SEGMENT if provider == "google" else _SEGMENT
    if len(segments) > MAX_SEGMENTS or "%" in decoded or not all(plain.fullmatch(s) for s in segments):
        raise NotAllowed("That is not a path this screen may call: paths are plain names "
                         "separated by slashes, with no dots, queries or encoded characters.")
    return segments


def check_query(query: object, provider: str = "salesforce", path: str | None = None) -> dict[str, str | list[str]]:
    """The query as a plain dict of str -> str | list[str], or NotAllowed.
    `_HttpMethod` is refused (any case): Salesforce honours it as a method
    override, which would turn a declared POST into a PATCH or DELETE. A Google
    query may only use the names its API takes (`GOOGLE_QUERY_NAMES_BY_API`, by
    `path`'s API; Drive's without one); its values are any text (a Drive `q` such as
    `'<id>' in parents and trashed = false`), encoded into the query only."""
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
        if provider == "google" and key not in GOOGLE_QUERY_NAMES_BY_API[google_api(path)]:
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
        if not _fits_one(token, seg, placeholders):
            return False
    return True


def _fits_one(token: str, seg: str, placeholders: dict[str, re.Pattern]) -> bool:
    expr = placeholders.get(token)
    if expr is not None:
        return bool(expr.fullmatch(seg))
    name, colon, verb = token.partition("}:")
    if colon and name.startswith("{"):  # `{id}:batchUpdate`: the placeholder, then exactly `:verb`
        expr = placeholders.get(name + "}")
        suffix = ":" + verb
        return expr is not None and seg.endswith(suffix) and bool(expr.fullmatch(seg[:-len(suffix)]))
    return token == seg


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
    segments = _segments(path, provider)
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


def _quote_google(seg: str, token: str) -> str:
    """One Google segment, quoted. A custom method the rule spells (`values:batchGet`, `{id}:batchUpdate`,
    `{range}:append`) keeps its own `:`; every other `:` (one inside an A1 range) is encoded, as
    Google's own clients send it."""
    _, colon, verb = token.rpartition(":")
    if colon and "/" not in verb and seg.endswith(":" + verb):
        return quote(seg[:-len(verb) - 1], safe="") + ":" + verb
    return quote(seg, safe="")


def build_url(provider: str, instance_url: str, path: str, query: object, *, method: str) -> str:
    """The one URL the call goes to. The call must pass `match`; the host is the
    connection's own sealed instance URL, which must pass the provider's host
    rule (Google: the named API's root in `GOOGLE_API_ROOTS`, else `DRIVE_API_ROOT`,
    and the connection names none); the decoded path is only ever appended, as
    quoted plain segments, and `query` is checked and encoded."""
    allowed = match(provider, method, path)
    params = check_query(query, provider, allowed.path)
    segments = allowed.path.split("/")
    quoted = "/".join(quote(s, safe="") for s in segments)
    if provider == "google":
        # Google's hosts are fixed; a connection naming one of its own is not a Google connection.
        if instance_url not in (None, ""):
            raise NotAllowed("This connection's Google address cannot be used.")
        api = google_api(allowed.path)
        root, rest = (DRIVE_API_ROOT, segments) if api == "drive" else (GOOGLE_API_ROOTS[api], segments[1:])
        tokens = allowed.rule.pattern.split("/")[len(segments) - len(rest):]
        url = f"{root}/" + "/".join(_quote_google(seg, tok) for seg, tok in zip(rest, tokens))
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


# ---- composite calls: many calls in one request -----------------------------------------------
#
# `composite/batch` (up to 25 calls) and `composite/sobjects` (sObject Collections, up to 200
# records) are taken apart here into the calls they hold, each matched exactly as if it were made
# alone, and put back together from what was matched: what is checked is what is sent. The caller
# (`call_service`) checks each part against the screen's declaration and spends one write of the
# lease on each write part, so a composite call can do nothing its parts could not do one by one.

MAX_BATCH_REQUESTS = 25
MAX_COLLECTION_RECORDS = 200
MAX_SUB_URL_CHARS = 4096
_BATCH_KEYS = frozenset({"batchRequests", "haltOnError"})
_SUBREQUEST_KEYS = frozenset({"method", "url", "richInput"})
_COLLECTION_KEYS = frozenset({"allOrNone", "records"})
# A subrequest's url is this API version, written either way Salesforce documents it.
_SUB_URL_PREFIXES = (f"/services/data/{SF_API_VERSION}/", f"{SF_API_VERSION}/")

# The first three characters of a record id say what it is. These are the same in every org, so a
# collection's update or delete (whose ids alone say what is changed) can be checked against the
# declaration. An id whose prefix isn't here (a custom object's) is refused in a collection; it can
# still be changed alone, where Salesforce reads the object from the path.
KEY_PREFIXES = {
    "001": "Account", "002": "Note", "003": "Contact", "006": "Opportunity", "00K": "OpportunityContactRole",
    "00P": "Attachment", "00Q": "Lead", "00T": "Task", "00U": "Event", "00k": "OpportunityLineItem",
    "00v": "CampaignMember", "02Z": "AccountContactRole", "500": "Case", "701": "Campaign",
    # Guarded, so refused wherever they appear (`GUARDED_OBJECTS`).
    "005": "User", "00D": "Organization", "00E": "UserRole", "00G": "Group", "00e": "Profile",
    "011": "GroupMember", "01p": "ApexClass", "01q": "ApexTrigger", "066": "ApexPage", "099": "ApexComponent",
    "0PS": "PermissionSet", "0Pa": "PermissionSetAssignment",
}


@dataclass(frozen=True)
class Composite:
    """A composite call taken apart. `parts` are the calls inside it, each as `match` answers for it
    alone (a collection's records as `sobjects/<type>[/<id>]`); `query` and `body` are exactly what is
    sent, rebuilt from what was matched."""
    kind: Literal["batch", "collection"]
    parts: tuple[Match, ...]
    query: dict
    body: Any

    @property
    def writes(self) -> int:
        """How many writes it is: one for each write inside it."""
        return sum(1 for p in self.parts if p.rule.kind == "write")


def is_composite(found: Match) -> bool:
    return found.rule.name == "composite"


def expand(provider: str, found: Match, query: dict, body: Any) -> Composite:
    """The calls inside a composite call `found` (which passed `match`), or NotAllowed with a sentence
    naming which one. Nothing nests: a composite call inside one is refused, as is `_HttpMethod`
    anywhere (`check_query`) and a guarded object's write (`match`)."""
    if provider != "salesforce" or not is_composite(found):
        raise NotAllowed("That is not a composite call.")
    if found.path == "composite/batch":
        return _batch(query, body)
    return _collection(found, query, body)


def _batch(query: dict, body: Any) -> Composite:
    if query:
        raise NotAllowed("A composite batch takes no query.")
    if not isinstance(body, dict) or not set(body) <= _BATCH_KEYS or "batchRequests" not in body:
        raise NotAllowed("A composite batch is {batchRequests: [...], haltOnError?: true or false}.")
    requests_ = body["batchRequests"]
    if not isinstance(requests_, list) or not 1 <= len(requests_) <= MAX_BATCH_REQUESTS:
        raise NotAllowed(f"A composite batch holds 1 to {MAX_BATCH_REQUESTS} calls.")
    halt = body.get("haltOnError", False)
    if not isinstance(halt, bool):
        raise NotAllowed("haltOnError must be true or false.")
    parts: list[Match] = []
    sent: list[dict] = []
    for i, sub in enumerate(requests_):
        where = f"Call {i + 1} in the batch"
        if not isinstance(sub, dict) or not {"method", "url"} <= set(sub) or not set(sub) <= _SUBREQUEST_KEYS:
            raise NotAllowed(f"{where} must be {{method, url}}, with a richInput for a body and nothing else.")
        path, sub_query = _sub_url(sub["url"], where)
        try:
            part = match("salesforce", sub["method"], path)
            sub_query = check_query(sub_query)
        except NotAllowed as exc:
            raise NotAllowed(f"{where}: {exc}") from None
        if is_composite(part):
            raise NotAllowed(f"{where}: a composite call can't hold another.")
        if part.rule.name == "apexrest":
            raise NotAllowed(f"{where}: Apex REST can't be called in a batch; call it alone.")
        if "richInput" in sub and part.rule.method in ("GET", "DELETE"):
            raise NotAllowed(f"{where}: a {part.rule.method} call can't carry a body.")
        quoted = "/".join(quote(s, safe="") for s in part.path.split("/"))
        item = {"method": part.rule.method,
                "url": f"{SF_API_VERSION}/{quoted}" + ("?" + urlencode(sub_query, doseq=True) if sub_query else "")}
        if "richInput" in sub:
            item["richInput"] = sub["richInput"]
        parts.append(part)
        sent.append(item)
    out: dict = {"batchRequests": sent}
    if "haltOnError" in body:
        out["haltOnError"] = halt
    return Composite("batch", tuple(parts), {}, out)


def _sub_url(url: object, where: str) -> tuple[str, dict]:
    """A subrequest's `url` as `(path relative to the API root, its query)`, or NotAllowed. Only this
    API version on this connection: never another host, version or part of the API."""
    if (not isinstance(url, str) or not url.isascii() or len(url) > MAX_SUB_URL_CHARS
            or any(c in url for c in "#\\") or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise NotAllowed(f"{where}: its url is not one a screen may call.")
    prefix = next((p for p in _SUB_URL_PREFIXES if url.startswith(p)), None)
    if prefix is None:
        raise NotAllowed(f"{where}: its url must start {_SUB_URL_PREFIXES[0]}, this connection's own API.")
    path, _, raw_query = url[len(prefix):].partition("?")
    query: dict[str, str | list[str]] = {}
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, errors="strict") if raw_query else []
    except (ValueError, UnicodeDecodeError):
        raise NotAllowed(f"{where}: its query can't be read.") from None
    for key, value in pairs:
        have = query.get(key)
        query[key] = value if have is None else [*(have if isinstance(have, list) else [have]), value]
    return path, query


def _collection(found: Match, query: dict, body: Any) -> Composite:
    method = found.rule.method
    if method == "GET":
        type_ = found.path.split("/")[2]
        if set(query) - {"ids", "fields"} or not {"ids", "fields"} <= set(query) or not isinstance(
                query["fields"], str):
            raise NotAllowed("Reading a collection takes ids and fields, and nothing else.")
        parts = tuple(_record_match("GET", type_, rid, None) for rid in _ids(query["ids"]))
        return Composite("collection", parts, dict(query), None)
    if method == "DELETE":
        if set(query) - {"ids", "allOrNone"} or "ids" not in query:
            raise NotAllowed("Deleting a collection takes ids, and allOrNone if you like, and nothing else.")
        if query.get("allOrNone", "false") not in ("true", "false"):
            raise NotAllowed("allOrNone must be true or false.")
        parts = []
        for i, rid in enumerate(_ids(query["ids"])):
            type_ = KEY_PREFIXES.get(rid[:3])
            if type_ is None:
                raise NotAllowed(f"Record {i + 1}: canopy can't tell what kind of record {rid} is, so it can't "
                                 "check this screen may delete it. Delete it alone.")
            parts.append(_record_match("DELETE", type_, rid, i))
        return Composite("collection", tuple(parts), dict(query), None)
    if query:
        raise NotAllowed("Creating or changing a collection takes no query.")
    if not isinstance(body, dict) or not set(body) <= _COLLECTION_KEYS or "records" not in body:
        raise NotAllowed("A collection is {records: [...], allOrNone?: true or false}.")
    records = body["records"]
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_COLLECTION_RECORDS:
        raise NotAllowed(f"A collection holds 1 to {MAX_COLLECTION_RECORDS} records.")
    if not isinstance(body.get("allOrNone", False), bool):
        raise NotAllowed("allOrNone must be true or false.")
    parts = []
    for i, record in enumerate(records):
        where = f"Record {i + 1}"
        attributes = record.get("attributes") if isinstance(record, dict) else None
        if not isinstance(attributes, dict) or set(attributes) != {"type"} or not isinstance(attributes["type"], str):
            raise NotAllowed(f"{where} must name its object as attributes: {{type: ...}}, and nothing else there.")
        type_ = attributes["type"]
        rid = None
        if method == "PATCH":
            ids = [record[k] for k in ("Id", "id") if k in record]
            if len(ids) != 1 or not isinstance(ids[0], str) or not _SF_ID.fullmatch(ids[0]):
                raise NotAllowed(f"{where} must carry the Id of the record it changes.")
            rid = ids[0]
            known = KEY_PREFIXES.get(rid[:3])
            if known is None or known.lower() != type_.lower():
                raise NotAllowed(f"{where}: {rid} isn't a record canopy can tell is a {type_[:40]}, so it can't "
                                 "check this screen may change it. Change it alone.")
        parts.append(_record_match(method, type_, rid, i))
    return Composite("collection", tuple(parts), {}, body)


def _ids(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise NotAllowed("ids is one comma-separated list of record ids.")
    ids = raw.split(",")
    if not 1 <= len(ids) <= MAX_COLLECTION_RECORDS or not all(_SF_ID.fullmatch(i) for i in ids):
        raise NotAllowed(f"ids is 1 to {MAX_COLLECTION_RECORDS} record ids, separated by commas.")
    return ids


def _record_match(method: str, type_: str, rid: str | None, index: int | None) -> Match:
    """One record of a collection, as the call that would read, make, change or delete it alone."""
    path = f"sobjects/{type_}" + (f"/{rid}" if rid else "")
    try:
        if not _SF_TYPE.fullmatch(type_):
            raise NotAllowed("That is not an object's name.")
        return match("salesforce", method, path)
    except NotAllowed as exc:
        raise NotAllowed(f"Record {index + 1}: {exc}" if index is not None else str(exc)) from None
