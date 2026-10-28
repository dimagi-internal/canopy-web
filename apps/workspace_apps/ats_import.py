"""An outgoing ATS → the Hiring app: extract everything over the export API, then load it.

Two steps, run separately, because they fail differently and the first must not
wait on the second:

* **extract** (`Extractor`) crawls the export API and saves every page exactly as
  the old ATS returned it. That raw archive is the PERMANENT record of the
  old ATS's years — keep it after cutover; the load is only one reading of it.
  It also downloads every attachment the moment a page lists it, because
  The old ATS's signed file URLs expire after 7 days: a crawl that saved URLs and
  fetched them later would lose the résumés. Resumable: a finished endpoint is
  skipped and an interrupted one continues from its saved cursor.
* **load** (`Loader`) maps the archive into the installation's collections. It
  is idempotent — an old-ATS key → record id table (`idmap.json`) beside the
  archive finds the record a previous load wrote, because a record's `data` may
  only hold the fields its collection declares, so the old ATS's id cannot live
  on the record. It can be re-run during the parallel-run weeks to pick up what
  changed in the old ATS, and never overwrites a record someone has since edited
  (or retention has anonymized) in canopy.

UNCONFIRMED — check against the live API with a real key before trusting a run.
The old ATS's v3 reference was not reachable when this was written, so the
endpoint paths in `ENDPOINTS`, the page-size parameter (`PAGE_PARAMS`), the
token request (client id + secret as HTTP Basic, `grant_type=client_credentials`
in the form body), and the record shapes the loader reads are from the v1/v2
reference and third-party v3 migration notes. What IS from v3 sources: OAuth
client credentials at the token URL, cursor pagination through a
`Link: <…>; rel="next"` header whose URL must be followed exactly as given
(adding parameters to it is a 422), un-embedded child objects, and a ~75
requests / 30 s window. The extractor is built to make a wrong guess cheap:
paths are one dict (overridable with `--endpoints file.json`), a 404 or 403 on
an endpoint is recorded as `missing`/`forbidden` and the crawl moves on, and
the loader reads both v1 (embedded objects) and v3 (`*_id`) shapes where they
differ. Run `--extract --only users` first and read `raw/users/page-00001.json`.

The output directory:

    raw/<endpoint>/page-00001.json   every page, verbatim
    raw/<endpoint>/_state.json       {"status", "next", "pages", "records"}
    raw/<endpoint>/<parent>/…        the same, per parent, for a per-record endpoint
    files/<key><ext>                 downloaded attachments
    attachments.json                 {key: {filename, type, sha256, size, path, status, owner ids}}
    idmap.json                       {installation, maps: {collection: {key: {id, sha}}}, files}
    reconciliation.json              the last load's report
"""
from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from django.db import transaction

from . import services
from .models import LIVE, AppFile, AppInstallation, AppRecord, AppRecordEvent

DEFAULT_BASE_URL = ""  # ATS_BASE_URL
DEFAULT_TOKEN_URL = ""  # ATS_TOKEN_URL
ACTION = "import:ats"

#: the export API list endpoints, by the name the archive and loader use. A value is
#: a path under the base URL, or {"path": "/jobs/{id}/stages", "per": "jobs"} for
#: an endpoint listed once per record of an endpoint crawled before it. The
#: names are NOT confirmed against v3 (see the module docstring).
ENDPOINTS: dict[str, str | dict] = {
    "users": "/users",
    "departments": "/departments",
    "offices": "/offices",
    "sources": "/sources",
    "rejection_reasons": "/rejection_reasons",
    "custom_fields": "/custom_fields",
    "jobs": "/jobs",
    "openings": "/openings",
    "hiring_team": "/hiring_team",
    "job_stages": "/job_stages",
    "job_posts": "/job_posts",
    "prospect_pools": "/prospect_pools",
    "candidates": "/candidates",
    "applications": "/applications",
    "stage_history": "/application_stages",
    "scorecards": "/scorecards",
    "scheduled_interviews": "/scheduled_interviews",
    "offers": "/offers",
    "notes": "/notes",
    "activities": "/activities",
    "eeoc": "/eeoc",
    "email_templates": "/email_templates",
    "attachments": "/attachments",
}
#: Sent on an endpoint's FIRST request only; a next link carries its own cursor.
PAGE_PARAMS = {"per_page": 500}
ATTACHMENT_TYPES = {"resume", "cover_letter", "other", "take_home_test", "offer_letter", "signed_offer_letter",
                    "offer_packet"}



def _is_linkedin(url: str) -> bool:
    """A LinkedIn profile URL by its HOST, not by "linkedin.com" appearing
    anywhere in it (https://evil.example/?linkedin.com would pass that)."""
    host = (urlparse(str(url)).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")

class ExportError(RuntimeError):
    """The old ATS answered in a way retrying will not fix. Never carries a secret."""


def _write_json(path: Path, obj) -> None:
    """Write-then-rename, so an interrupted run leaves the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str))
    os.replace(tmp, path)


def _read_json(path: Path, default):
    return json.loads(path.read_text()) if path.is_file() else default


def records_of(body) -> list:
    """The records on one page. v1 answered a bare list; a v3 envelope is
    tolerated as its first list (`data`, `results`, or whatever it is called)."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "results", "items"):
            if isinstance(body.get(k), list):
                return body[k]
        for v in body.values():
            if isinstance(v, list):
                return v
    return []


# --- extract -----------------------------------------------------------------------------


def _retry_after(r, default: float) -> float:
    raw = (r.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return float(raw)
    try:
        return max(0.0, (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return default


class ExportClient:
    """The export API over OAuth client credentials, inside the old ATS's rate window.

    The budget is spent BEFORE each call (a sliding window of call times), so a
    crawl stays under the limit instead of discovering it by 429s; a 429 that
    comes anyway is waited out for exactly its `Retry-After`. 5xx and dropped
    connections back off exponentially; a 401 refreshes the token once."""

    def __init__(self, *, client_id: str, client_secret: str, base_url: str = DEFAULT_BASE_URL,
                 token_url: str = DEFAULT_TOKEN_URL, session=None, budget: int = 75, window: float = 30.0,
                 max_retries: int = 6, max_throttles: int = 20, timeout: float = 60.0,
                 sleep=time.sleep, clock=time.monotonic):
        if not client_id or not client_secret:
            raise ExportError("ATS_CLIENT_ID and ATS_CLIENT_SECRET must both be set")
        if not base_url or not token_url:
            raise ExportError("ATS_BASE_URL and ATS_TOKEN_URL (or --base-url / --token-url) must both be set")
        self.client_id, self._secret = client_id, client_secret
        self.base_url, self.token_url = base_url.rstrip("/"), token_url
        self.session = session or requests.Session()
        self.budget, self.window = budget, window
        self.max_retries, self.max_throttles, self.timeout = max_retries, max_throttles, timeout
        self.sleep, self.clock = sleep, clock
        self._calls: deque[float] = deque()
        self._access: str | None = None
        self._expires = 0.0
        self.requests = 0

    def _token(self) -> str:
        if self._access and self.clock() < self._expires - 60:
            return self._access
        r = self.session.request("POST", self.token_url, data={"grant_type": "client_credentials"},
                                 auth=(self.client_id, self._secret), timeout=self.timeout)
        if r.status_code != 200:
            raise ExportError(f"token request to {self.token_url} answered {r.status_code}")
        body = r.json()
        self._access = body["access_token"]
        self._expires = self.clock() + float(body.get("expires_in") or 3600)
        return self._access

    def _spend(self) -> None:
        now = self.clock()
        while self._calls and self._calls[0] <= now - self.window:
            self._calls.popleft()
        if len(self._calls) >= self.budget:
            self.sleep(self._calls[0] + self.window - now)
            now = self.clock()
            while self._calls and self._calls[0] <= now - self.window:
                self._calls.popleft()
        self._calls.append(self.clock())

    def get(self, url: str, params: dict | None = None, *, api: bool = True):
        """One GET with the retry rules. `api=False` is a file download from
        a signed URL on another host: no bearer (it must not leave the old ATS's
        own origin) and no API budget."""
        failures = throttles = 0
        refreshed = False
        while True:
            headers = {}
            if api:
                self._spend()
                headers["Authorization"] = f"Bearer {self._token()}"
            self.requests += 1
            try:
                r = self.session.request("GET", url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                failures += 1
                if failures > self.max_retries:
                    raise ExportError(f"GET {url} failed {failures} times: {type(e).__name__}") from None
                self.sleep(min(60.0, 2.0 ** failures))
                continue
            if r.status_code == 429:
                throttles += 1
                if throttles > self.max_throttles:
                    raise ExportError(f"GET {url}: still rate-limited after {throttles} waits")
                self.sleep(_retry_after(r, self.window))
                continue
            if r.status_code == 401 and api and not refreshed:
                self._access, refreshed = None, True
                continue
            if r.status_code >= 500:
                failures += 1
                if failures > self.max_retries:
                    raise ExportError(f"GET {url} answered {r.status_code} {failures} times")
                self.sleep(min(60.0, 2.0 ** failures))
                continue
            limit = r.headers.get("X-RateLimit-Limit", "")
            if api and limit.isdigit() and 0 < int(limit) < self.budget:
                self.budget = int(limit)
            return r

    def url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")


class AttachmentStore:
    """Every attachment any page lists, downloaded once, with its checksum."""

    def __init__(self, out: Path, client: ExportClient):
        self.out, self.client = out, client
        self.path = out / "attachments.json"
        self.manifest: dict[str, dict] = _read_json(self.path, {})

    def collect(self, endpoint: str, records: list) -> None:
        for rec in records:
            for att, owner in _attachments_in(endpoint, rec):
                self._fetch(att, owner)
        _write_json(self.path, self.manifest)

    def _fetch(self, att: dict, owner: dict) -> None:
        key = attachment_key(att)
        have = self.manifest.get(key)
        if have and have.get("status") == "ok" and (self.out / have["path"]).is_file():
            return
        ext = Path(att.get("filename") or "").suffix.lower()[:10]
        entry = {"filename": att.get("filename") or key, "type": att.get("type"), "created_at": att.get("created_at"),
                 **owner, "status": "pending"}
        url = att["url"]
        api = urlparse(url).netloc == urlparse(self.client.base_url).netloc
        try:
            r = self.client.get(url, api=api)
        except ExportError as e:
            entry["status"] = f"failed: {e}"
        else:
            if r.status_code != 200:
                entry["status"] = f"failed: HTTP {r.status_code}"
            else:
                rel = f"files/{key}{ext}"
                body = r.content
                (self.out / "files").mkdir(parents=True, exist_ok=True)
                (self.out / rel).write_bytes(body)
                entry.update(status="ok", path=rel, size=len(body), sha256=hashlib.sha256(body).hexdigest(),
                             content_type=r.headers.get("Content-Type", ""))
        self.manifest[key] = entry


def attachment_key(att: dict) -> str:
    """Stable across crawls: the old ATS's id when it gives one, else the URL
    without its (rotating) signature."""
    if att.get("id") is not None:
        return f"a{att['id']}"
    base = att["url"].split("?", 1)[0] + "|" + str(att.get("filename") or "")
    return "u" + hashlib.sha1(base.encode()).hexdigest()[:20]


def _is_attachment(d: dict) -> bool:
    return (isinstance(d.get("url"), str) and d["url"].startswith(("https://", "http://"))
            and ("filename" in d or d.get("type") in ATTACHMENT_TYPES))


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _attachments_in(endpoint: str, rec) -> list[tuple[dict, dict]]:
    """(attachment, owner ids) for every attachment in a record, wherever it is
    nested — v1 embedded them in candidates/applications/offers, v3 lists them."""
    if not isinstance(rec, dict):
        return []
    owner = {}
    if endpoint == "candidates":
        owner["candidate_id"] = rec.get("id")
    elif endpoint == "applications":
        owner.update(application_id=rec.get("id"), candidate_id=rec.get("candidate_id"))
    elif endpoint == "offers":
        owner.update(offer_id=rec.get("id"), application_id=rec.get("application_id"),
                     candidate_id=rec.get("candidate_id"))
    out = []
    for d in _walk(rec):
        if _is_attachment(d):
            own = {**owner, **{k: d[k] for k in ("candidate_id", "application_id", "offer_id") if d.get(k)}}
            out.append((d, {k: v for k, v in own.items() if v is not None}))
    return out


class Extractor:
    """Crawl every endpoint into `out/raw`, resuming where a previous run stopped."""

    def __init__(self, client: ExportClient, out: Path, *, endpoints: dict | None = None,
                 only: list[str] | None = None, log=print):
        self.client, self.out, self.log = client, Path(out), log
        self.endpoints = endpoints or ENDPOINTS
        unknown = sorted(set(only or []) - set(self.endpoints))
        if unknown:
            raise ValueError(f"no endpoint named {', '.join(unknown)}")
        self.only = only
        self.files = AttachmentStore(self.out, client)

    def run(self) -> dict:
        summary = {}
        for name, spec in self.endpoints.items():
            if self.only and name not in self.only:
                continue
            summary[name] = self._crawl(name, spec if isinstance(spec, dict) else {"path": spec})
            self.log(f"{name}: {summary[name]['status']}, {summary[name]['records']} records")
        return summary

    def _crawl(self, name: str, spec: dict) -> dict:
        root = self.out / "raw" / name
        state_path = root / "_state.json"
        state = _read_json(state_path, {})
        if state.get("status") == "complete":
            return state
        if not spec.get("per"):
            state = self._stream(root, self.client.url(spec["path"]))
        else:
            parents = [r.get("id") for r in Archive(self.out).records(spec["per"]) if isinstance(r, dict)]
            subs = [self._stream(root / str(pid), self.client.url(spec["path"].format(id=pid)))
                    for pid in parents if pid is not None]
            statuses = {s["status"] for s in subs}
            state = {"status": "complete" if statuses <= {"complete", "missing"} else "partial",
                     "streams": len(subs), "records": sum(s["records"] for s in subs),
                     "pages": sum(s["pages"] for s in subs)}
        _write_json(state_path, state)
        return state

    def _stream(self, root: Path, first: str) -> dict:
        """One cursor chain. The page is written before the cursor moves past it,
        so a crash re-fetches at most the page it was on."""
        state_path = root / "_stream.json"
        state =_read_json(state_path, {"status": "new", "next": None, "pages": 0, "records": 0})
        if state["status"] in ("complete", "missing", "forbidden"):
            return state
        url, params = (state["next"], None) if state.get("next") else (first, dict(PAGE_PARAMS))
        if not state.get("next"):
            state.update(pages=0, records=0)
        while url:
            r = self.client.get(url, params=params)
            if r.status_code in (403, 404) and state["pages"] == 0:
                state.update(status="missing" if r.status_code == 404 else "forbidden", next=None)
                break
            if r.status_code != 200:
                raise ExportError(f"GET {url} answered {r.status_code}: {r.text[:300]}")
            body = r.json()
            recs = records_of(body)
            state["pages"] += 1
            _write_json(root / f"page-{state['pages']:05d}.json", body)
            self.files.collect(root.relative_to(self.out / "raw").parts[0], recs)
            nxt = (r.links or {}).get("next", {}).get("url")
            url, params = (urljoin(r.url or url, nxt) if nxt else None), None
            state.update(records=state["records"] + len(recs), next=url, status="partial" if url else "complete")
            _write_json(state_path, state)
        _write_json(state_path, state)
        return state


class Archive:
    """Read side of `out/raw`."""

    def __init__(self, out: Path):
        self.root = Path(out) / "raw"

    def status(self, name: str) -> dict:
        return _read_json(self.root / name / "_state.json", {"status": "not extracted", "records": 0})

    def records(self, name: str) -> list:
        d = self.root / name
        if not d.is_dir():
            return []
        out = []
        for page in sorted(d.glob("page-*.json")):
            out.extend(records_of(json.loads(page.read_text())))
        for sub in sorted(p for p in d.iterdir() if p.is_dir()):
            for page in sorted(sub.glob("page-*.json")):
                for rec in records_of(json.loads(page.read_text())):
                    out.append({**rec, "_parent": sub.name} if isinstance(rec, dict) else rec)
        return [r for r in out if isinstance(r, dict)]


# --- load: reading the old ATS's values -------------------------------------------------------


def _id(v):
    return v.get("id") if isinstance(v, dict) else v


def _name(v) -> str:
    if isinstance(v, dict):
        v = v.get("name") or v.get("public_name") or v.get("description") or v.get("value")
    return str(v).strip() if v not in (None, "") else ""


def _day(v) -> str | None:
    s = str(v or "")
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else None


def _ts(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _text(raw) -> str:
    """The old ATS's HTML (posts, kits, templates, question text) as plain text."""
    s = str(raw or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in s.splitlines())).strip()


def _first_value(obj: dict, *keys) -> str:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list) and v:
            v = v[0]
        if isinstance(v, dict):
            v = v.get("value")
        if v:
            return str(v).strip()
    return ""


def _fold(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (s or "").casefold()).strip()


_EU_UK = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "czech republic", "denmark", "estonia",
    "finland", "france", "germany", "greece", "hungary", "ireland", "italy", "latvia", "lithuania", "luxembourg",
    "malta", "netherlands", "the netherlands", "poland", "portugal", "romania", "slovakia", "slovenia", "spain",
    "sweden", "norway", "iceland", "liechtenstein", "switzerland", "united kingdom", "uk", "u.k.", "great britain",
    "england", "scotland", "wales", "northern ireland",
}
_US = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}
_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky",
    "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh",
    "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "massachusetts",
    "california", "new york", "texas", "washington", "illinois", "florida",
}
_REGION_OF = {"india": "India", "kenya": "Kenya", "south africa": "South Africa"}


def region_of(address: str) -> tuple[str, str]:
    """(privacy region, country) from a free-text address. Retention runs by
    region, so a guess that is not clearly one of the named regions is "Other".
    The last comma-separated part is tried first: it is where a country goes."""
    parts = [p.strip().strip(".") for p in re.split(r"[,\n]", address or "") if p.strip()]
    for raw in reversed(parts):
        p = re.sub(r"\s+\d[\d\- ]*$", "", raw).strip()  # "MA 02139" -> "MA"
        f = p.casefold()
        if f in _US:
            return "US", "United States"
        if f in _EU_UK:
            return "EU/UK", p
        if f in _REGION_OF:
            return _REGION_OF[f], _REGION_OF[f]
        if f in _US_STATES:
            return "US", "United States"
    return "Other", parts[-1] if parts else ""


# The old ATS's EEOC answers → the Hiring app's options. Anything else is left empty.
_EEO = {
    "eeo_gender": {"female": "Female", "male": "Male", "non binary": "Non-binary", "nonbinary": "Non-binary",
                   "decline to self identify": "Decline to answer", "decline to answer": "Decline to answer",
                   "i don't wish to answer": "Decline to answer"},
    "eeo_race": {"american indian or alaskan native": "American Indian or Alaska Native",
                 "american indian or alaska native": "American Indian or Alaska Native", "asian": "Asian",
                 "black or african american": "Black or African American",
                 "hispanic or latino": "Hispanic or Latino",
                 "native hawaiian or other pacific islander": "Native Hawaiian or Other Pacific Islander",
                 "white": "White", "two or more races": "Two or more races",
                 "decline to self identify": "Decline to answer", "decline to answer": "Decline to answer"},
    "eeo_veteran": {"i am not a protected veteran": "I am not a protected veteran",
                    "i identify as one or more of the classifications of protected veteran":
                        "I am a protected veteran",
                    "i am a protected veteran": "I am a protected veteran",
                    "i don't wish to answer": "Decline to answer", "decline to self identify": "Decline to answer"},
    "eeo_disability": {"yes, i have a disability (or previously had a disability)": "Yes", "yes": "Yes",
                       "no, i don't have a disability": "No", "no": "No",
                       "i don't wish to answer": "Decline to answer", "decline to self identify": "Decline to answer"},
}
_EEO_SOURCE = {"eeo_gender": "gender", "eeo_race": "race", "eeo_veteran": "veteran_status",
               "eeo_disability": "disability_status"}


def eeo_answers(eeoc: dict | None) -> dict:
    out = {}
    for field, src in _EEO_SOURCE.items():
        raw = _name((eeoc or {}).get(src)).casefold().replace("’", "'").strip()
        if raw in _EEO[field]:
            out[field] = _EEO[field][raw]
    return out


_SOURCE_HINTS = [("referral", "Referral"), ("linkedin", "LinkedIn"), ("indeed", "Indeed"),
                 ("idealist", "Idealist"), ("devex", "Devex"), ("reliefweb", "ReliefWeb"), ("agenc", "Agency"),
                 ("careers", "Careers page"), ("jobs page", "Careers page"), ("company website", "Careers page"),
                 ("prospecting", "Sourced"), ("sourced", "Sourced")]
_REJECTION_ALIASES = {"lacking skill(s)/qualification(s)": "Skills mismatch",
                      "hired another candidate": "Position filled", "position filled": "Position filled",
                      "location": "Location or work authorization", "compensation": "Compensation",
                      "withdrew": "Withdrew", "unresponsive": "Unresponsive",
                      "spam": "Spam or fraud", "accepted another offer": "Hired elsewhere",
                      "not enough experience": "Not enough experience", "underqualified": "Not enough experience"}
_STAGE_HINTS = [("application review", "Applied"), ("applied", "Applied"), ("hiring manager", "Hiring manager review"),
                ("screen", "Screen"), ("offer", "Offer"), ("interview", "Interview"), ("face to face", "Interview"),
                ("onsite", "Interview"), ("on site", "Interview"), ("technical", "Interview"), ("panel", "Interview"),
                ("assessment", "Interview"), ("take home", "Interview"), ("reference", "Hiring manager review")]
#: An interview whose name matches nothing: a generic slot, counted in the
#: report so the workspace can add its own kinds (overlay) and re-load.
_INTERVIEW_FALLBACK = "Panel"
_INTERVIEW_HINTS = [("phone", "Phone screen"), ("screen", "Phone screen"), ("hiring manager", "Hiring manager"),
                    ("technical", "Technical"), ("coding", "Technical"), ("case", "Technical"), ("panel", "Panel"),
                    ("final", "Final")]
_RECOMMENDATION = {"definitely not": "Strong no", "strong no": "Strong no", "no": "No", "yes": "Yes",
                   "strong yes": "Strong yes", "no decision": "No decision", "mixed": "Mixed"}
_TEMPLATE_KIND = {"candidate_rejection": "Rejection", "candidate_availability_request": "Availability",
                  "candidate_availability_confirmation": "Availability", "interviewer_invite": "Interview",
                  "extending_offer": "Offer"}
_PLACEHOLDERS = {"CANDIDATE_NAME": "name", "CANDIDATE_FIRST_NAME": "name", "JOB_NAME": "job.title",
                 "COMPANY": "settings.company_name", "COMPANY_NAME": "settings.company_name"}
#: Fields whose manifest default would ASSERT something the old ATS never said
#: (that a job is remote, that a salary is in USD per year).
_NO_DEFAULT = {("job", "workplace"), ("job", "employment_type"), ("job", "pay_currency"), ("job", "pay_period"),
               ("offer", "currency"), ("offer", "pay_period"), ("offer", "employment_type"),
               ("interview", "timezone"), ("interview", "duration"), ("interview_step", "duration")}


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


# --- load ---------------------------------------------------------------------------------


class _RollbackError(Exception):
    pass


class Loader:
    """Map the archive into one installation's live records. `dry_run` does the
    whole load — every validation, every reference — then rolls it back."""

    def __init__(self, inst: AppInstallation, out: Path, *, dry_run: bool = False, log=print):
        self.inst, self.out, self.dry_run, self.log = inst, Path(out), dry_run, log
        self.m = inst.manifest
        self.archive = Archive(self.out)
        self.attachments: dict[str, dict] = _read_json(self.out / "attachments.json", {})
        idmap = _read_json(self.out / "idmap.json", {"installation": inst.pk, "maps": {}, "files": {}})
        if idmap.get("installation") != inst.pk:
            raise ValueError(f"{self.out / 'idmap.json'} belongs to installation {idmap.get('installation')}, "
                             f"not {inst.pk}: load into a fresh output directory, or the one this was loaded from")
        self.ids: dict[str, dict] = defaultdict(dict, idmap.get("maps") or {})
        self.files: dict[str, int] = dict(idmap.get("files") or {})
        self.counts: dict[str, Counter] = defaultdict(Counter)
        self.skipped: dict[str, Counter] = defaultdict(Counter)
        self.dropped: Counter = Counter()
        self.unmapped: dict[str, Counter] = defaultdict(Counter)
        self.sources: dict[str, int] = {}
        self.att_stats: Counter = Counter()

    # --- the run

    def run(self) -> dict:
        try:
            with transaction.atomic():
                self._lookups()
                self._jobs()
                self._interview_steps()
                self._pools()
                self._applications()
                self._openings()
                self._stage_history()
                self._interviews()
                self._scorecards()
                self._offers()
                self._notes()
                self._email_templates()
                if self.dry_run:
                    raise _RollbackError()
        except _RollbackError:
            pass
        else:
            _write_json(self.out / "idmap.json", {"installation": self.inst.pk, "maps": dict(self.ids),
                                                  "files": self.files})
        report = self.report()
        if not self.dry_run:
            _write_json(self.out / "reconciliation.json", report)
        return report

    def _lookups(self) -> None:
        a = self.archive
        self.users = {}
        for u in a.records("users"):
            email = _first_value(u, "primary_email_address", "email", "emails", "email_addresses")
            if email:
                self.users[u.get("id")] = email.lower()
        self.depts = {d.get("id"): _name(d) for d in a.records("departments")}
        self.offices = {o.get("id"): o for o in a.records("offices")}
        self.source_names = {s.get("id"): _name(s) for s in a.records("sources")}
        self.reasons = {r.get("id"): _name(r) for r in a.records("rejection_reasons")}
        self.candidates = {c.get("id"): c for c in a.records("candidates")}
        self.eeoc = {e.get("application_id"): e for e in a.records("eeoc")}
        self.posts = defaultdict(list)
        for p in a.records("job_posts"):
            self.posts[p.get("job_id") or p.get("_parent")].append(p)
        self.stages = a.records("job_stages")
        self.stage_names = {s.get("id"): _name(s) for s in self.stages}
        self.teams = defaultdict(lambda: defaultdict(list))
        for t in a.records("hiring_team"):
            job = t.get("job_id") or t.get("_parent")
            if t.get("user_id") is not None:  # one row per member
                self.teams[job][str(t.get("role") or "hiring_managers")].append(t)
            else:
                for role, members in t.items():
                    if isinstance(members, list):
                        self.teams[job][role].extend(members)
        self.jobs = {j.get("id"): j for j in a.records("jobs")}
        for j in self.jobs.values():
            if isinstance(j.get("hiring_team"), dict) and j.get("id") not in self.teams:
                for role, members in j["hiring_team"].items():
                    self.teams[j.get("id")][role].extend(members or [])
        self.applications = a.records("applications")
        self.app_by_id = {ap.get("id"): ap for ap in self.applications}
        self.apps_of = defaultdict(list)
        for ap in sorted(self.applications, key=lambda x: str(x.get("applied_at") or "")):
            self.apps_of[ap.get("candidate_id")].append(ap)

    def _email(self, person) -> str:
        if isinstance(person, dict):
            return (person.get("email") or self.users.get(person.get("id")) or
                    self.users.get(person.get("user_id")) or "").lower()
        return self.users.get(person, "")

    # --- writing

    def _option(self, coll: str, field: str, value: str) -> str | None:
        """`value` if the installation's own options have it (casefolded):
        a workspace that added a stage or reason through its overlay gets it."""
        for o in self.m["collections"][coll]["fields"][field].get("options", []):
            if _fold(o) == _fold(value):
                return o
        return None

    def _valid(self, coll: str, data: dict) -> dict:
        fields = self.m["collections"][coll]["fields"]
        out = {}
        for k, v in data.items():
            spec = fields[k]
            if v in (None, "") or (spec["type"] == "file" and self.dry_run):
                continue
            if spec["type"] == "file":
                if AppFile.objects.filter(installation=self.inst, space=LIVE, pk=v).exists():
                    out[k] = v
                continue
            if spec["type"] in ("text", "longtext"):
                v = str(v)
                if len(v) > spec["max"]:
                    self.dropped[(coll, k, "truncated")] += 1
                    v = v[: spec["max"]]
            try:
                v = services._coerce(k, spec, v, self.inst, LIVE)
            except ValueError:
                self.dropped[(coll, k, "invalid")] += 1
                continue
            if v is not None:
                out[k] = v
        for k, f in fields.items():
            if out.get(k) is None and "default" in f and (coll, k) not in _NO_DEFAULT:
                out[k] = f["default"]
        return out

    def _put(self, coll: str, key, data: dict, *, created=None, updated=None) -> int | None:
        """Create or update the record an old-ATS key maps to. A record whose
        data no longer matches what the last load wrote was changed in canopy
        (an edit, an anonymization) and is left alone."""
        key = str(key)
        data = self._valid(coll, data)
        fields = self.m["collections"][coll]["fields"]
        missing = [k for k, f in fields.items() if f["required"] and data.get(k) is None]
        if missing:
            self.skipped[coll][f"missing {', '.join(missing)}"] += 1
            return None
        entry = self.ids[coll].get(key)
        rec = (AppRecord.objects.filter(installation=self.inst, space=LIVE, collection=coll, pk=entry["id"]).first()
               if entry else None)
        digest = _digest(data)
        if rec is None:
            rec = AppRecord.objects.create(installation=self.inst, space=LIVE, collection=coll, data=data,
                                           created_by_label=ACTION)
            self._event(rec, "create", data)
            self.counts[coll]["created"] += 1
        elif rec.data == data:
            self.counts[coll]["unchanged"] += 1
            self.ids[coll][key] = {"id": rec.pk, "sha": digest}
            return rec.pk
        elif entry.get("sha") and _digest(rec.data) != entry["sha"]:
            self.counts[coll]["kept (changed in canopy)"] += 1
            return rec.pk
        else:
            changes = {k: [rec.data.get(k), data.get(k)] for k in set(rec.data) | set(data)
                       if rec.data.get(k) != data.get(k)}
            rec.data = data
            rec.save()
            self._event(rec, "update", changes)
            self.counts[coll]["updated"] += 1
        stamps = {}
        if _ts(created):
            stamps["created_at"] = _ts(created)
        if _ts(updated or created):
            stamps["updated_at"] = _ts(updated or created)
        if stamps:  # retention counts from updated_at: an old rejection must not look new
            AppRecord.objects.filter(pk=rec.pk).update(**stamps)
        file_ids = [v for k, v in data.items() if fields[k]["type"] == "file"]
        if file_ids:
            AppFile.objects.filter(installation=self.inst, pk__in=file_ids).update(claimed=True)
        self.ids[coll][key] = {"id": rec.pk, "sha": digest}
        return rec.pk

    def _event(self, rec: AppRecord, op: str, changes: dict) -> None:
        AppRecordEvent.objects.create(installation=self.inst, space=LIVE, record=rec, record_ref=rec.pk,
                                      collection=rec.collection, action=ACTION, op=op, changes=changes,
                                      actor_label=ACTION)

    def _ref(self, coll: str, key) -> int | None:
        if key is None:
            return None
        entry = self.ids[coll].get(str(key))
        return entry["id"] if entry else None

    def _file(self, coll: str, field: str, key: str | None) -> int | None:
        """The AppFile for a downloaded attachment, stored once however often it is loaded."""
        if key is None:
            return None
        self.att_stats["referenced"] += 1
        att = self.attachments.get(key) or {}
        if att.get("status") != "ok" or not (self.out / att.get("path", "")).is_file():
            self.att_stats["referenced, not downloaded"] += 1
            return None
        if self.dry_run:
            self.att_stats["would attach"] += 1
            return None
        fid = self.files.get(key)
        if fid and AppFile.objects.filter(installation=self.inst, pk=fid, sha256=att["sha256"]).exists():
            self.att_stats["attached"] += 1
            return fid
        spec = self.m["collections"][coll]["fields"][field]
        body = (self.out / att["path"]).read_bytes()
        ctype = att.get("content_type") or mimetypes.guess_type(att["filename"])[0] or "application/octet-stream"
        try:
            f = services.upload(self.inst, space=LIVE, name=att["filename"], content_type=ctype, body=body,
                                label=ACTION, client_ip=None, accept=spec["accept"],
                                max_mb=max(spec["max_mb"], 25))
        except ValueError as e:
            self.att_stats[f"not attachable ({e})"] += 1
            return None
        self.files[key] = f.pk
        self.att_stats["attached"] += 1
        return f.pk

    def _attachment(self, types: set[str], *, candidate=None, application=None, offer=None) -> str | None:
        """The newest attachment of these types on the most specific owner."""
        best = []
        for key, a in self.attachments.items():
            if a.get("type") not in types or (a.get("offer_id") and a.get("offer_id") != offer):
                continue  # another offer's letter is never this one's
            if offer is not None and a.get("offer_id") == offer:
                best.append((2, str(a.get("created_at") or ""), key))
            elif application is not None and a.get("application_id") == application:
                best.append((1, str(a.get("created_at") or ""), key))
            elif candidate is not None and a.get("candidate_id") == candidate and not a.get("application_id"):
                best.append((0, str(a.get("created_at") or ""), key))
        return max(best)[2] if best else None

    # --- the collections, in reference order

    def _team(self, job_id) -> dict[str, list[str]]:
        out = {}
        for role, members in (self.teams.get(job_id) or {}).items():
            members = sorted(members, key=lambda m: not (isinstance(m, dict) and m.get("responsible")))
            out[role] = [e for e in (self._email(m) for m in members) if e]
        return out

    def _pay(self, job: dict) -> dict:
        custom = [*(job.get("keyed_custom_fields") or {}).values(), *(job.get("custom_fields") or {}).values()]
        for cf in custom:
            v = cf.get("value") if isinstance(cf, dict) and "value" in cf else cf
            if isinstance(v, dict) and ("min_value" in v or "max_value" in v):
                return {"pay_min": v.get("min_value"), "pay_max": v.get("max_value"), "pay_currency": v.get("unit")}
        for post in self.posts.get(job.get("id"), []):
            for pr in post.get("pay_input_ranges") or []:
                lo, hi = pr.get("min_cents"), pr.get("max_cents")
                return {"pay_min": lo / 100 if lo is not None else None,
                        "pay_max": hi / 100 if hi is not None else None, "pay_currency": pr.get("currency_type")}
        return {}

    def _jobs(self) -> None:
        for job in sorted(self.jobs.values(), key=lambda j: (str(j.get("created_at") or ""), str(j.get("id")))):
            posts = self.posts.get(job.get("id"), [])
            post = next((p for p in posts if p.get("external") or p.get("live")), posts[0] if posts else {})
            depts = [self.depts.get(_id(d)) or _name(d) for d in job.get("departments") or []] or \
                [self.depts.get(i, "") for i in job.get("department_ids") or ([job["department_id"]]
                                                                              if job.get("department_id") else [])]
            offices = [self.offices.get(_id(o)) or o for o in job.get("offices") or []] or \
                [self.offices.get(i, {}) for i in job.get("office_ids") or []]
            team = self._team(job.get("id"))
            openings = job.get("openings") or []
            pay = self._pay(job)
            if pay and not self._option("job", "pay_currency", str(pay.get("pay_currency") or "")):
                self.unmapped["pay currency"][str(pay.get("pay_currency"))] += 1
                pay = {}
            elif pay:
                pay["pay_currency"] = self._option("job", "pay_currency", pay["pay_currency"])
            status = str(job.get("status") or "").lower()
            location = _name(post.get("location") or {}) or next(
                (_name(o.get("location")) for o in offices if isinstance(o, dict) and o.get("location")), "")
            everyone = list(dict.fromkeys(e for role in team.values() for e in role))
            data = {
                "title": job.get("name") or job.get("title"),
                "department": ", ".join(d for d in depts if d),
                "office": ", ".join(_name(o) for o in offices if _name(o)),
                "location": location,
                "description": _text(post.get("content")) or _text(job.get("notes")) or
                "Imported from the old ATS, which had no job post for this job.",
                "status": status if status in ("open", "closed", "draft") else "closed",
                "post_status": "live" if any(p.get("live") for p in posts) else "offline",
                "requisition_id": job.get("requisition_id"),
                "openings": len(openings) or None,
                "confidential": bool(job.get("confidential")),
                "is_template": bool(job.get("is_template")),
                "copied_from": self._ref("job", job.get("copied_from_id")),
                "hiring_manager": (team.get("hiring_managers") or [None])[0],
                "recruiter": (team.get("recruiters") or [None])[0],
                "coordinator": (team.get("coordinators") or [None])[0],
                "hiring_team": ", ".join(everyone),
                "opened_on": _day(job.get("opened_at")),
                "closed_on": _day(job.get("closed_at")),
                **pay,
            }
            self._put("job", job.get("id"), data, created=job.get("created_at"), updated=job.get("updated_at"))

    def _interview_steps(self) -> None:
        self.step_of: dict = {}
        for stage in self.stages:
            job = self._ref("job", stage.get("job_id") or stage.get("_parent"))
            for iv in stage.get("interviews") or []:
                kit = iv.get("interview_kit") or {}
                key = f"{stage.get('id')}:{iv.get('id')}"
                pk = self._put("interview_step", key, {
                    "job": job, "position": min(100, max(0, int(stage.get("priority") or 0) + 1)),
                    "name": _name(iv), "stage": _name(stage),
                    "duration": min(480, max(5, int(iv["estimated_minutes"]))) if iv.get("estimated_minutes") else None,
                    "interviewers": ", ".join(e for e in (self._email(u) for u in iv.get("default_interviewer_users")
                                                         or []) if e),
                    "prep": _text(kit.get("content")),
                    "questions": "\n".join(_text(q.get("question")) for q in kit.get("questions") or []),
                })
                if pk:
                    self.step_of[iv.get("id")] = pk

    def _pools(self) -> None:
        pools = {p.get("id"): p for p in self.archive.records("prospect_pools")}
        for ap in self.applications:
            pool = (ap.get("prospect_detail") or {}).get("prospect_pool")
            if isinstance(pool, dict) and pool.get("id") is not None:
                pools.setdefault(pool["id"], pool)
        for pid, pool in pools.items():
            self._put("pool", pid, {"name": _name(pool), "description": _text(pool.get("description"))})

    def _stage(self, gh_name: str, status: str) -> str:
        if status in ("hired", "rejected"):
            return status.capitalize()
        exact = self._option("application", "stage", gh_name)
        if exact:
            return exact
        f = _fold(gh_name)
        for hint, stage in _STAGE_HINTS:
            if hint in f and self._option("application", "stage", stage):
                return stage
        self.unmapped["stage"][gh_name or "(none)"] += 1
        return "Interview"

    def _source(self, src) -> tuple[str, str | None]:
        """(source option, tracking tag). An unknown source is Other, keeping
        the old ATS's name in the tag; so is a fuzzy match, so nothing is lost."""
        name = _name(src) or self.source_names.get(src if not isinstance(src, dict) else src.get("id"), "")
        if not name:
            return "Other", None
        exact = self._option("application", "source", name)
        if exact:
            return exact, None
        f = _fold(name + " " + _name((src or {}).get("type") if isinstance(src, dict) else ""))
        for hint, option in _SOURCE_HINTS:
            if hint in f:
                return option, name[:80]
        self.unmapped["source"][name] += 1
        return "Other", name[:80]

    def _rejection(self, ap: dict) -> tuple[str | None, str | None]:
        raw = ap.get("rejection_reason")
        name = _name(raw) if isinstance(raw, dict) else self.reasons.get(ap.get("rejection_reason_id") or raw, "")
        notes = []
        details = ap.get("rejection_details") or {}
        for v in (details.get("custom_fields") or {}).values() if isinstance(details, dict) else []:
            if isinstance(v, str) and v.strip():
                notes.append(v.strip())
        if not name:
            return (("Other" if ap.get("status") == "rejected" else None), "\n".join(notes) or None)
        option = self._option("application", "rejection_reason", name) or \
            self._option("application", "rejection_reason", _REJECTION_ALIASES.get(name.casefold(), ""))
        if option is None:
            self.unmapped["rejection reason"][name] += 1
            option = "Other"
        if _fold(option) != _fold(name):
            notes.insert(0, f"Previous ATS reason: {name}")
        return option, "\n".join(notes) or None

    def _applications(self) -> None:
        for ap in self.applications:
            cand = self.candidates.get(ap.get("candidate_id")) or {}
            jobs = ap.get("jobs") or []
            job_gh = _id(jobs[0]) if jobs else ap.get("job_id")
            status = str(ap.get("status") or "active").lower()
            stage_raw = ap.get("current_stage")
            stage_name = _name(stage_raw) if isinstance(stage_raw, dict) else self.stage_names.get(
                ap.get("current_stage_id") or stage_raw, "")
            address = _name((ap.get("location") or {}).get("address") if isinstance(ap.get("location"), dict)
                            else ap.get("location")) or _first_value(cand, "addresses")
            region, country = region_of(address)
            source, tag = self._source(ap.get("source") or ap.get("source_id"))
            reason, note = self._rejection(ap)
            sites = [str(_first_value({"v": [w]}, "v")) for w in (cand.get("website_addresses") or []) +
                     (cand.get("social_media_addresses") or [])]
            referrer = ap.get("referrer") or cand.get("referrer")
            ref_email = ""
            if isinstance(referrer, dict):
                ref_email = referrer.get("value") if referrer.get("type") == "email" else self.users.get(
                    referrer.get("value") if referrer.get("type") == "id" else referrer.get("id"), "")
            tags = [_name(t) for t in cand.get("tags") or []]
            if status == "converted":
                tags.append("ats:converted")
            pool = (ap.get("prospect_detail") or {}).get("prospect_pool")
            data = {
                "job": self._ref("job", job_gh),
                "name": " ".join(p for p in (cand.get("first_name"), cand.get("last_name")) if p) or cand.get("name"),
                "email": _first_value(cand, "email_addresses", "emails", "email"),
                "phone": _first_value(cand, "phone_numbers", "phones"),
                "location": address,
                "country": country,
                "region": region,
                "linkedin": next((s for s in sites if _is_linkedin(s)), None),
                "resume": self._file("application", "resume", self._attachment(
                    {"resume"}, candidate=ap.get("candidate_id"), application=ap.get("id"))),
                "source": source,
                "source_tag": tag,
                "referrer": ref_email,
                "credited_to": self._email(ap.get("credited_to")),
                "kind": "prospect" if ap.get("prospect") else "applicant",
                "pool": self._ref("pool", _id(pool)) if pool else None,
                "stage": self._stage(stage_name, status),
                "status": status if status in ("active", "rejected", "hired") else "active",
                "applied_on": _day(ap.get("applied_at")),
                "rejected_on": _day(ap.get("rejected_at")),
                "hired_on": _day(ap.get("hired_at")),
                "rejection_reason": reason,
                "rejection_note": note,
                "tags": ", ".join(t for t in tags if t),
                "do_not_email": cand.get("can_email") is False,
                **eeo_answers(self.eeoc.get(ap.get("id"))),
            }
            self._put("application", ap.get("id"), data, created=ap.get("applied_at") or ap.get("created_at"),
                      updated=ap.get("last_activity_at") or ap.get("updated_at"))

    def _openings(self) -> None:
        rows = [{**o, "job_id": o.get("job_id") or o.get("_parent")} for o in self.archive.records("openings")]
        seen = {r.get("id") for r in rows}
        for job in self.jobs.values():
            rows.extend({**o, "job_id": job.get("id")} for o in job.get("openings") or [] if o.get("id") not in seen)
        self.opening_rows = len(rows)
        for o in rows:
            reason = _name(o.get("close_reason"))
            f = reason.casefold()
            close = (self._option("opening", "close_reason", reason) or
                     ("Hired" if "hired" in f else "Cancelled" if "cancel" in f else
                      "Filled internally" if "internal" in f else "Other")) if reason else None
            self._put("opening", o.get("id"), {
                "job": self._ref("job", o.get("job_id")),
                "opening_id": o.get("opening_id") or f"GH-{o.get('id')}",
                "status": "closed" if str(o.get("status")).lower() == "closed" else "open",
                "close_reason": close,
                "opened_on": _day(o.get("opened_at")), "closed_on": _day(o.get("closed_at")),
                "hired_application": self._ref("application", o.get("application_id")),
            })

    def _stage_history(self) -> None:
        self.history_rows = 0
        for h in self.archive.records("stage_history"):
            self.history_rows += 1
            app_gh = h.get("application_id") or h.get("_parent")
            stage = h.get("stage") or h.get("stage_name") or self.stage_names.get(h.get("stage_id"), "")
            at = h.get("entered_at") or h.get("created_at") or h.get("moved_at")
            name = _name(stage)
            self._put("stage_change", h.get("id") or f"{app_gh}:{name}:{at}", {
                "application": self._ref("application", app_gh),
                "job": self._ref("job", (self.app_by_id.get(app_gh) or {}).get("job_id") or
                                 _id(((self.app_by_id.get(app_gh) or {}).get("jobs") or [None])[0])),
                "to_stage": name, "at": str(at) if at else None, "day": _day(at),
                "by": self._email(h.get("user") or h.get("user_id")) or _name(h.get("user")),
            }, created=at)

    def _app_job(self, app_gh):
        ap = self.app_by_id.get(app_gh) or {}
        jobs = ap.get("jobs") or []
        return self._ref("job", _id(jobs[0]) if jobs else ap.get("job_id"))

    def _interviews(self) -> None:
        self.interview_of_scorecard = {}
        for iv in self.archive.records("scheduled_interviews"):
            name = _name(iv.get("interview"))
            kind = self._option("interview", "kind", name)
            if not kind:
                f = _fold(name)
                kind = next((k for hint, k in _INTERVIEW_HINTS if hint in f), None)
            if not kind:
                self.unmapped["interview kind"][name or "(none)"] += 1
                kind = _INTERVIEW_FALLBACK
            start, end = _ts((iv.get("start") or {}).get("date_time")), _ts((iv.get("end") or {}).get("date_time"))
            day = start.astimezone(UTC).date().isoformat() if start else _day(
                (iv.get("start") or {}).get("date"))
            minutes = int((end - start).total_seconds() // 60) if start and end else None
            status = str(iv.get("status") or "").lower()
            link = iv.get("video_conferencing_url") or iv.get("location")
            people = iv.get("interviewers") or []
            pk = self._put("interview", iv.get("id"), {
                "application": self._ref("application", iv.get("application_id")),
                "job": self._app_job(iv.get("application_id")),
                "step": self.step_of.get(_id(iv.get("interview"))),
                "kind": kind, "date": day,
                "time": start.astimezone(UTC).strftime("%H:%M") if start else None,
                "duration": min(480, max(5, minutes)) if minutes else None,
                "timezone": "UTC" if start else None,
                "interviewers": ", ".join(e for e in (self._email(p) for p in people) if e),
                "meeting_link": link if str(link or "").startswith(("https://", "http://")) else None,
                "status": {"complete": "Done", "awaiting_feedback": "Done", "cancelled": "Cancelled",
                           "canceled": "Cancelled"}.get(status, "Scheduled"),
            }, created=iv.get("created_at"), updated=iv.get("updated_at"))
            for p in people:
                if isinstance(p, dict) and p.get("scorecard_id") and pk:
                    self.interview_of_scorecard[p["scorecard_id"]] = pk

    def _scorecards(self) -> None:
        for sc in self.archive.records("scorecards"):
            lines, notes, takeaways, private = [], [], [], []
            attrs = sc.get("attributes") or []
            if not attrs and isinstance(sc.get("ratings"), dict):
                attrs = [{"name": n, "rating": r} for r, names in sc["ratings"].items() for n in names or []]
            for a in attrs:
                rating = _RECOMMENDATION.get(_fold(str(a.get("rating") or "")))
                if rating and rating != "No decision":
                    lines.append(f"{_name(a)}: {rating}")
                if a.get("note"):
                    notes.append(f"{_name(a)} — {_text(a['note'])}")
            for q in sc.get("questions") or []:
                question, answer = _text(q.get("question")), _text(q.get("answer"))
                if not answer:
                    continue
                folded = _fold(question)
                if "take away" in folded or "takeaway" in folded:
                    takeaways.append(answer)
                elif folded.startswith("private note"):
                    private.append(answer)
                else:
                    notes.append(f"{question}\n{answer}")
            interviewer = self._email(sc.get("interviewer")) or self._email(sc.get("submitted_by"))
            self._put("scorecard", sc.get("id"), {
                "application": self._ref("application", sc.get("application_id")),
                "interview": self.interview_of_scorecard.get(sc.get("id")),
                "interviewer": interviewer,
                "rating": _RECOMMENDATION.get(_fold(str(sc.get("overall_recommendation") or "")), "No decision"),
                "attribute_ratings": "\n".join(lines), "key_takeaways": "\n\n".join(takeaways),
                "notes": "\n\n".join(notes), "private_notes": "\n\n".join(private),
            }, created=sc.get("submitted_at") or sc.get("created_at"), updated=sc.get("updated_at"))

    def _offers(self) -> None:
        for of in self.archive.records("offers"):
            app_gh = of.get("application_id")
            ap = self.app_by_id.get(app_gh) or {}
            job_gh = of.get("job_id") or _id((ap.get("jobs") or [None])[0]) or ap.get("job_id")
            job = self.jobs.get(job_gh) or {}
            status = str(of.get("status") or "").lower()
            mapped = {"accepted": "hired" if str(ap.get("status")).lower() == "hired" else "accepted",
                      "rejected": "declined", "deprecated": "withdrawn"}.get(status)
            if mapped is None:
                mapped = self._option("offer", "status", status) or ("sent" if of.get("sent_at") else "draft")
            salary, currency = None, None
            for cf in (of.get("keyed_custom_fields") or {}).values():
                v = cf.get("value") if isinstance(cf, dict) else None
                if isinstance(cf, dict) and str(cf.get("type")) == "currency" and isinstance(v, dict):
                    salary, currency = v.get("amount"), v.get("unit")
                    break
            currency = self._option("offer", "currency", str(currency or "")) if currency else None
            cand, opening = of.get("candidate_id") or ap.get("candidate_id"), of.get("opening")
            self._put("offer", of.get("id"), {
                "application": self._ref("application", app_gh), "job": self._ref("job", job_gh),
                "opening": self._ref("opening", _id(opening) if opening else of.get("opening_id")),
                "title": job.get("name") or job.get("title") or "Offer",
                "start_date": _day(of.get("starts_at")), "salary": salary if currency else None,
                "currency": currency, "status": mapped, "sent_on": _day(of.get("sent_at")),
                "responded_at": str(of["resolved_at"]) if of.get("resolved_at") else None,
                "letter": self._file("offer", "letter", self._attachment(
                    {"offer_letter", "offer_packet"}, offer=of.get("id"), application=app_gh, candidate=cand)),
                "signed_letter": self._file("offer", "signed_letter", self._attachment(
                    {"signed_offer_letter"}, offer=of.get("id"), application=app_gh, candidate=cand)),
            }, created=of.get("created_at"), updated=of.get("updated_at") or of.get("resolved_at"))

    def _notes(self) -> None:
        for n in self.archive.records("notes"):
            app_gh = n.get("application_id")
            if app_gh is None:  # a note on the person: hang it on their latest application
                apps = self.apps_of.get(n.get("candidate_id") or n.get("_parent")) or []
                app_gh = apps[-1].get("id") if apps else None
            visibility = str(n.get("visibility") or n.get("visiblity") or "").lower()
            private = n.get("private") is True or visibility in ("private", "admin_only")
            body = _text(n.get("body"))
            self._put("note", n.get("id"), {
                "application": self._ref("application", app_gh),
                "body": None if private else body, "private_body": body if private else None,
                "author": self._email(n.get("user") or n.get("user_id")),
            }, created=n.get("created_at"))

    def _email_templates(self) -> None:
        for t in self.archive.records("email_templates"):
            body = t.get("body") or _text(t.get("html_body"))
            for gh, ours in _PLACEHOLDERS.items():
                body = re.sub(r"\{\{\s*" + gh + r"\s*\}\}", "{{" + ours + "}}", body or "")
            self._put("email_template", t.get("id"), {
                "name": _name(t), "kind": _TEMPLATE_KIND.get(str(t.get("type")), "General"),
                "subject": t.get("subject") or _name(t), "body": body,
            }, created=t.get("created_at"), updated=t.get("updated_at"))

    # --- the report

    def report(self) -> dict:
        def present(coll):
            return sum(self.counts[coll].values())

        pairs = [("jobs", "job", len(self.jobs)), ("openings", "opening", getattr(self, "opening_rows", 0)),
                 ("job_stages", "interview_step", sum(len(s.get("interviews") or []) for s in self.stages)),
                 ("applications", "application", len(self.applications)),
                 ("stage_history", "stage_change", getattr(self, "history_rows", 0))]
        for ep, coll in (("scheduled_interviews", "interview"), ("scorecards", "scorecard"), ("offers", "offer"),
                         ("notes", "note"), ("email_templates", "email_template")):
            pairs.append((ep, coll, len(self.archive.records(ep))))
        downloaded = sum(1 for a in self.attachments.values() if a.get("status") == "ok")
        return {
            "dry_run": self.dry_run,
            "endpoints": {name: {k: v for k, v in self.archive.status(name).items() if k in ("status", "records")}
                          for name in ENDPOINTS},
            "collections": {c: dict(v) for c, v in self.counts.items()},
            "reconcile": [{"endpoint": ep, "in_archive": n, "collection": c, "written": present(c),
                           "skipped": sum(self.skipped[c].values())} for ep, c, n in pairs],
            "skipped": {c: dict(v) for c, v in self.skipped.items() if v},
            # `referenced`/`attached` count record fields (a candidate's résumé is on each
            # of their applications); `stored` counts distinct files.
            "attachments": {"listed": len(self.attachments), "downloaded": downloaded,
                            "failed": len(self.attachments) - downloaded, **dict(self.att_stats),
                            "stored": len(set(self.files.values()))},
            "unmapped": {k: dict(v) for k, v in self.unmapped.items() if v},
            "dropped_fields": {f"{c}.{f} ({why})": n for (c, f, why), n in self.dropped.items()},
        }


def format_report(r: dict) -> str:
    out = [("DRY RUN — nothing was written" if r["dry_run"] else "Loaded") + "\n", "Records per collection:"]
    for c, counts in sorted(r["collections"].items()):
        out.append(f"  {c:16} " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    out.append("\nReconciliation (archive → records):")
    for row in r["reconcile"]:
        flag = "" if row["in_archive"] == row["written"] else "   <-- differs"
        out.append(f"  {row['endpoint']:22} {row['in_archive']:>7} → {row['collection']:16} {row['written']:>7}"
                   f" (skipped {row['skipped']}){flag}")
    out.append("\nArchive endpoints:")
    for name, st in r["endpoints"].items():
        out.append(f"  {name:22} {st.get('status', '?'):14} {st.get('records', 0):>7}")
    out.append("\nAttachments: " + ", ".join(f"{k} {v}" for k, v in r["attachments"].items()))
    for title, key in (("Skipped", "skipped"), ("Not mapped (went to a fallback)", "unmapped")):
        if r[key]:
            out.append(f"\n{title}:")
            for c, reasons in r[key].items():
                out.append(f"  {c}: " + "; ".join(f"{k} ×{v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    if r["dropped_fields"]:
        out.append("\nField values dropped or cut: " + "; ".join(f"{k} ×{v}" for k, v in r["dropped_fields"].items()))
    return "\n".join(out)
