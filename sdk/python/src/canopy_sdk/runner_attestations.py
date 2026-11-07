"""Runner attestations: a host site learns which approved runner made a request.

A runner (a cloud box, a laptop) proves itself to canopy with a device key a person
approved behind a passkey. A site cannot check that proof itself, so the runner makes one
FOR THE SITE and the site forwards it to canopy, which answers who holds the key and
whether that runner serves the agent the request speaks for. The site shows the answer
beside what the request claims, and may refuse a request that carries none.

The contract both halves build against:

* the runner proves the exact request it sends. It signs a proof whose ``htu`` is
  ``bound_url(<the site endpoint>, <the request body bytes>)``, so the signature covers a
  digest of the body and the proof is no use for any other request or any other site;
* it sends the proof in the ``X-Canopy-Runner-Proof`` header, ``header_value(kind, proof)``,
  ``kind`` being ``dpop`` (a key the runner holds: RFC 9449, ES256, ``htm`` POST) or
  ``aws-iam`` (an EC2 runner's instance role: ``X-Canopy-Aws-Proof``'s value);
* the site reads it with ``parse_header`` and asks canopy with ``request_attestation``,
  a request signed by the SITE's key, exactly as for a sign-off.

Each proof is single-use and fresh within 120 seconds, and canopy answers 401
``proof_refused`` in one wording whatever the reason.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from . import sign_offs
from .contract import normalize_url

#: Where a site asks, under canopy's base URL.
PATH = "/api/sites/runner-attestations"
HEADER = "X-Canopy-Runner-Proof"
KINDS = ("dpop", "aws-iam")


class AttestationError(Exception):
    """canopy would not vouch for the proof, or could not be asked. ``code`` names why
    (``proof_refused``, ``unreachable``, ``malformed``, the site-request codes of
    ``sign_offs``); the message never contains a credential."""

    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Attestation:
    """Who canopy says made the proof."""

    runner: str
    kind: str
    jkt: str
    #: The agent the site named, as canopy knows it ("" if canopy has no such agent).
    agent: str
    #: True only when the runner holding the key serves ``agent``.
    serves_agent: bool


def create_url(canopy_url: str) -> str:
    return normalize_url(canopy_url) + PATH


def bound_url(site_url: str, content: bytes | str) -> str:
    """The ``htu`` a runner signs for a request to ``site_url`` with this body: the URL
    (no query, no fragment) and a sha256 of the exact bytes sent. Runner and site call
    this one function, so they cannot disagree."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    parts = urlsplit(site_url)
    base = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return f"{base}/attest/{hashlib.sha256(content).hexdigest()}"


def header_value(kind: str, proof: str) -> str:
    if kind not in KINDS:
        raise AttestationError("malformed", f"kind must be one of {', '.join(KINDS)}")
    return f"{kind} {proof}"


def parse_header(value: str) -> tuple[str, str]:
    """``(kind, proof)`` from a header value, or ``AttestationError``."""
    kind, _, proof = (value or "").strip().partition(" ")
    if kind not in KINDS or not proof.strip():
        raise AttestationError("malformed", f"{HEADER} must be '<{'|'.join(KINDS)}> <proof>'")
    return kind, proof.strip()


def request_attestation(canopy_url: str, site_name: str, site_key, *, kind: str, proof: str, agent: str = "",
                        workspace: str = "", post: Callable[..., tuple] | None = None) -> Attestation:
    """Ask canopy who made ``proof``. Raises ``AttestationError``."""
    from . import fetch

    if kind not in KINDS:
        raise AttestationError("malformed", f"kind must be one of {', '.join(KINDS)}")
    url = create_url(canopy_url)
    try:
        token = sign_offs.request_assertion(site_name, site_key, url, workspace=workspace, agent=agent)
    except (ValueError, sign_offs.SignOffError) as exc:
        raise AttestationError(sign_offs.NOT_CONFIGURED, str(exc)) from exc
    sender = post or fetch.post_json
    try:
        status, raw, _headers = sender(url, {"kind": kind, "proof": proof, "agent": agent},
                                       headers={"Authorization": f"Bearer {token}"})
    except fetch.FetchError as exc:
        raise AttestationError(sign_offs.UNREACHABLE, str(exc)) from exc
    if status >= 400:
        refusal = sign_offs._refusal(status, raw)
        raise AttestationError(refusal.code, refusal.message, status=status)
    try:
        body: Mapping = json.loads(raw or b"{}")
        return Attestation(runner=str(body["runner"]), kind=str(body["kind"]), jkt=str(body["jkt"]),
                           agent=str(body.get("agent") or ""), serves_agent=bool(body["serves_agent"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise AttestationError("malformed", "canopy's answer is not a runner attestation") from exc
