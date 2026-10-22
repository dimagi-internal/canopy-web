"""Best-effort secret scrubbing for shared transcripts.

Transcripts routinely contain API keys, tokens, ``.env`` values, and
``Authorization`` headers. Before a session is persisted (and therefore
shareable), every turn's ``plaintext`` and every string leaf of its
structured ``content`` is run through this scrub.

This is deliberately conservative: it targets well-known *shaped* secrets
(provider key prefixes, JWTs, private-key blocks) plus assignment-style
``KEY = secret`` lines for sensitive-looking names. It does NOT do generic
high-entropy detection — that mangles ordinary code and prose and erodes
trust in the output. It is best-effort, not a guarantee; the docs and the
visibility gate say so.

Each redaction substitutes a ``‹redacted:<kind>›`` marker and is counted.
"""
from __future__ import annotations

import re
from typing import Any, Callable

_MARK = "‹redacted:{kind}›"


def _whole(kind: str) -> Callable[[re.Match], str]:
    marker = _MARK.format(kind=kind)
    return lambda _m: marker


def _keep_prefix(kind: str, group: int = 1) -> Callable[[re.Match], str]:
    """Redact the secret but keep the labelling prefix group intact."""
    marker = _MARK.format(kind=kind)

    def _sub(m: re.Match) -> str:
        return f"{m.group(group)}{marker}"

    return _sub


# (compiled pattern, substitution) — applied IN ORDER.
#
# Ordering matters: the multi-line private-key block is greediest and runs
# first; the single-line ``KEY = value`` assignment runs next so a secret that
# lives in an assignment (``TOKEN=ghp_…``) is redacted as one unit and the
# shaped-secret patterns below don't fire a *second* time on the same value.
_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match], str]]] = [
    # PEM private-key blocks (any flavour). Match first — greediest.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        _whole("private-key"),
    ),
    # Sensitive KEY = value / KEY: value assignments. Keep the name + operator.
    (
        re.compile(
            r"((?:[A-Za-z0-9_]*"
            r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|"
            r"ACCESS[_-]?KEY|CLIENT[_-]?SECRET)"
            r"[A-Za-z0-9_]*)\s*[:=]\s*)"
            r"(['\"]?)([^\s'\"]{6,})(\2)",
            re.IGNORECASE,
        ),
        lambda m: f"{m.group(1)}{m.group(2)}{_MARK.format(kind='secret')}{m.group(4)}",
    ),
    # JWTs (three base64url segments).
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        _whole("jwt"),
    ),
    # OpenAI / Anthropic-style keys.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), _whole("api-key")),
    # canopy's own bearer tokens: personal (cpat_), delegated (cdt_), contact
    # (ccon_), caller (cct_), runner (crt_), chat key (chk_), HCP (hcpat_/hcprt_).
    (re.compile(r"\b(?:cpat|cdt|ccon|cct|crt|chk|hcpat|hcprt)_[A-Za-z0-9_-]{20,}"),
     _whole("canopy-token")),
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_).
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), _whole("github-token")),
    # GitHub fine-grained PAT.
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), _whole("github-token")),
    # AWS access key id.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _whole("aws-key")),
    # Google API key.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), _whole("google-key")),
    # Slack tokens.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), _whole("slack-token")),
    # Authorization: Bearer <token> (keep the "Authorization: Bearer " prefix).
    (
        re.compile(
            r"(\b[Aa]uthorization\b\s*[:=]\s*[Bb]earer\s+)[A-Za-z0-9._~+/=-]{8,}"
        ),
        _keep_prefix("bearer"),
    ),
]


def redact_text(text: str) -> tuple[str, int]:
    """Return (scrubbed_text, redaction_count).

    Also strips NUL bytes (``\\x00``): Postgres ``jsonb`` rejects the ``\\u0000``
    code point and ``text`` columns reject raw NULs, so a transcript carrying one
    (common in tool output) would 500 the whole upload at ``bulk_create``. NUL
    removal is not counted as a redaction — it's data sanitation, not a secret.
    """
    if not text:
        return text, 0
    out = text.replace("\x00", "")
    total = 0
    for pattern, sub in _PATTERNS:
        out, n = pattern.subn(sub, out)
        total += n
    return out, total


def _redact_value(value: Any) -> tuple[Any, int]:
    """Recursively scrub string leaves of a JSON-ish value."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        total = 0
        new_list = []
        for item in value:
            scrubbed, n = _redact_value(item)
            new_list.append(scrubbed)
            total += n
        return new_list, total
    if isinstance(value, dict):
        total = 0
        new_dict = {}
        for key, item in value.items():
            scrubbed, n = _redact_value(item)
            new_dict[key] = scrubbed
            total += n
        return new_dict, total
    return value, 0


def redact_turn(plaintext: str, content: Any) -> tuple[str, Any, int]:
    """Scrub a single turn. Returns (plaintext, content, redaction_count).

    Both ``plaintext`` and ``content`` are scrubbed, but only ``content``'s
    redactions are counted: the parser derives ``plaintext`` from ``content``
    (it's a truncated/flattened copy), so ``content`` is a superset. Counting
    both would double-count every secret that appears in an assistant/user
    text turn.
    """
    new_plaintext, _ = redact_text(plaintext)
    new_content, n_content = _redact_value(content)
    return new_plaintext, new_content, n_content
