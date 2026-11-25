"""Model calls for app pages: `canopy.ai.complete(request)` (docs/architecture/app-ai.md).

A page's frame has no network, so an app that wants a model asks canopy, which calls it with
canopy's OWN credential — never a key the viewer pasted. The rules:

* **Declared.** Only an app whose manifest says `"ai": {"purpose": …}` may ask (`manifest.py`),
  so installing it, and any version that adds or drops it, shows that its pages send what they
  show to a model (`template_diff`). Member pages only: never a public or private-link page.
* **Aliases, never model ids.** A page asks for `fast` or `smart`; `settings.APP_AI_MODELS` is
  the one place an alias becomes a model, so switching provider is a settings change and no app
  pins a model that is later retired.
* **One shape, any provider.** The request and answer are provider-neutral (system, messages
  of text / tool_use / tool_result blocks, tools, a JSON schema, web search) and translated here:
  `_gemini_*` for Gemini on Vertex, `_claude_*` for Claude on Vertex (Anthropic's Messages
  shape). A tool call's `signature` is opaque: Gemini's thought signature, carried back by the
  page on its next turn and never read.
* **canopy's identity, memory's gate.** The call goes out on memory's Vertex session (workload
  identity from the task role, no stored key) and only once the project is verified to keep
  nothing (`embeddings.check_retention`, cached `RETENTION_TTL`).
* **Attributed, logged, limited.** Every call is an `AppAiCall` row — viewer, app, alias,
  model, tokens, time, outcome; never the prompt or the answer — and those rows are the
  per-viewer limit (`APP_AI_PER_HOUR`, `APP_AI_PER_DAY`, across every app).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.contacts import embeddings

from .models import AppAiCall, AppInstallation

logger = logging.getLogger(__name__)

ALIASES = ("fast", "smart")
MAX_TOKENS, DEFAULT_MAX_TOKENS = 8192, 2048
#: What a request may carry, counted as its JSON: big enough for an email thread plus a
#: record's history, small enough that one call cannot be a bulk export.
MAX_REQUEST_CHARS = 400_000
MAX_MESSAGES, MAX_TOOLS = 100, 32
#: Gemini counts its thinking against maxOutputTokens; this much on top keeps a short
#: `max_tokens` from being spent before any visible text.
THINKING_HEADROOM = 4096
TIMEOUT = (5, 120)
RETENTION_TTL = embeddings.RETENTION_TTL
ANTHROPIC_PREFIX = "anthropic/"

_retention: dict[str, tuple[bool, str, float]] = {}
_lock = threading.Lock()


class Refused(Exception):
    """A request canopy will not send: the status and the sentence a page shows."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class RateLimited(Exception):
    pass


# --- the request ---------------------------------------------------------------------------


def model_for(alias: str) -> str:
    """The model an alias names now, or "" when it names none."""
    return str((getattr(settings, "APP_AI_MODELS", {}) or {}).get(alias) or "").strip()


def enabled() -> bool:
    return bool(getattr(settings, "HCP_VERTEX_PROJECT", "")) and any(model_for(a) for a in ALIASES)


def _text(value, where: str, limit: int = MAX_REQUEST_CHARS) -> str:
    if not isinstance(value, str):
        raise Refused(422, f"{where} must be text")
    return value[:limit]


def _block(raw, where: str, role: str) -> dict:
    if not isinstance(raw, dict):
        raise Refused(422, f"{where} must be an object")
    kind = raw.get("type")
    if kind == "text":
        out = {"type": "text", "text": _text(raw.get("text", ""), f"{where}.text")}
        if raw.get("signature"):
            out["signature"] = _text(raw["signature"], f"{where}.signature", 20_000)
        return out
    if kind == "tool_use" and role == "assistant":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise Refused(422, f"{where}.name is required")
        out = {"type": "tool_use", "id": str(raw.get("id") or "")[:200], "name": name[:64],
               "input": raw.get("input") if isinstance(raw.get("input"), dict) else {}}
        if raw.get("signature"):
            out["signature"] = _text(raw["signature"], f"{where}.signature", 20_000)
        return out
    if kind == "tool_result" and role == "user":
        content = raw.get("content", "")
        return {"type": "tool_result", "tool_use_id": str(raw.get("tool_use_id") or "")[:200],
                "content": content if isinstance(content, (str, dict, list)) else str(content)}
    raise Refused(422, f"{where}.type must be text, tool_use (assistant) or tool_result (user)")


def clean(raw) -> dict:
    """The request a page sent, checked and normalised, or Refused(422) saying what is wrong."""
    if not isinstance(raw, dict):
        raise Refused(422, "the request must be an object")
    if len(json.dumps(raw, default=str)) > MAX_REQUEST_CHARS:
        raise Refused(413, f"the request is larger than {MAX_REQUEST_CHARS} characters")
    alias = raw.get("model") or "fast"
    if alias not in ALIASES:
        raise Refused(422, f"model must be one of {', '.join(ALIASES)} (an alias, never a model id)")
    msgs = raw.get("messages")
    if not isinstance(msgs, list) or not msgs or len(msgs) > MAX_MESSAGES:
        raise Refused(422, f"messages must be a list of 1 to {MAX_MESSAGES}")
    messages = []
    for i, m in enumerate(msgs):
        where = f"messages[{i}]"
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise Refused(422, f"{where}.role must be user or assistant")
        content = m.get("content")
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list) and content:
            blocks = [_block(b, f"{where}.content[{j}]", m["role"]) for j, b in enumerate(content)]
        else:
            raise Refused(422, f"{where}.content must be text or a list of blocks")
        messages.append({"role": m["role"], "content": blocks})
    if messages[0]["role"] != "user":
        raise Refused(422, "the first message must be the user's")
    tools = []
    for i, t in enumerate(raw.get("tools") or []):
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"]:
            raise Refused(422, f"tools[{i}] needs a name")
        schema = t.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise Refused(422, f"tools[{i}].input_schema must be a JSON schema object")
        tools.append({"name": t["name"][:64], "description": str(t.get("description") or "")[:2000],
                      "input_schema": schema})
    if len(tools) > MAX_TOOLS:
        raise Refused(422, f"at most {MAX_TOOLS} tools")
    schema = raw.get("json_schema")
    if schema is not None and not isinstance(schema, dict):
        raise Refused(422, "json_schema must be a JSON schema object")
    if schema is not None and tools:
        raise Refused(422, "ask for json_schema or tools, not both")
    try:
        max_tokens = int(raw.get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (TypeError, ValueError):
        raise Refused(422, "max_tokens must be a number") from None
    if not 1 <= max_tokens <= MAX_TOKENS:
        raise Refused(422, f"max_tokens must be 1 to {MAX_TOKENS}")
    system = raw.get("system") or ""
    return {"model": alias, "system": _text(system, "system"), "messages": messages, "tools": tools,
            "json_schema": schema, "max_tokens": max_tokens, "web_search": bool(raw.get("web_search")),
            "feature": str(raw.get("feature") or "")[:60]}


# --- Gemini on Vertex ----------------------------------------------------------------------


def _gemini_body(req: dict) -> dict:
    names: dict[str, str] = {}
    for m in req["messages"]:
        for b in m["content"]:
            if b["type"] == "tool_use":
                names[b["id"]] = b["name"]
    contents = []
    for m in req["messages"]:
        parts = []
        for b in m["content"]:
            if b["type"] == "text":
                if b["text"]:
                    parts.append({"text": b["text"], **({"thoughtSignature": b["signature"]} if b.get("signature") else {})})
            elif b["type"] == "tool_use":
                parts.append({"functionCall": {"name": b["name"], "args": b["input"]},
                              **({"thoughtSignature": b["signature"]} if b.get("signature") else {})})
            else:
                parts.append({"functionResponse": {"name": names.get(b["tool_use_id"], "unknown"),
                                                   "response": {"result": b["content"]}}})
        if parts:
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})
    body: dict = {"contents": contents,
                  "generationConfig": {"maxOutputTokens": req["max_tokens"] + THINKING_HEADROOM}}
    if req["system"]:
        body["systemInstruction"] = {"parts": [{"text": req["system"]}]}
    tools = []
    if req["tools"]:
        tools.append({"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                                "parameters": t["input_schema"]} for t in req["tools"]]})
    if req["web_search"]:
        tools.append({"googleSearch": {}})
    if tools:
        body["tools"] = tools
    if req["json_schema"] is not None:
        body["generationConfig"].update(responseMimeType="application/json", responseJsonSchema=req["json_schema"])
    if req["model"] == "fast":
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
    return body


def _gemini_answer(data: dict) -> dict:
    cand = (data.get("candidates") or [{}])[0]
    content = []
    for p in (cand.get("content") or {}).get("parts") or []:
        if p.get("thought"):
            continue
        sig = {"signature": p["thoughtSignature"]} if p.get("thoughtSignature") else {}
        if "functionCall" in p:
            fc = p["functionCall"] or {}
            content.append({"type": "tool_use", "id": f"call_{uuid.uuid4().hex[:16]}", "name": fc.get("name", ""),
                            "input": fc.get("args") or {}, **sig})
        elif p.get("text"):
            content.append({"type": "text", "text": p["text"], **sig})
    finish = cand.get("finishReason") or ""
    if any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    elif finish == "MAX_TOKENS":
        stop = "max_tokens"
    elif finish in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
        stop = "refusal"
    else:
        stop = "end_turn"
    usage = data.get("usageMetadata") or {}
    return {"content": content, "stop_reason": stop,
            "usage": {"input_tokens": int(usage.get("promptTokenCount") or 0),
                      "output_tokens": int(usage.get("candidatesTokenCount") or 0)
                      + int(usage.get("thoughtsTokenCount") or 0)}}


# --- Claude on Vertex ----------------------------------------------------------------------

#: The one tool a JSON-schema request becomes on Claude, forced, so its input IS the answer.
_JSON_TOOL = "respond"


def _claude_body(req: dict) -> dict:
    messages = []
    for m in req["messages"]:
        blocks = []
        for b in m["content"]:
            if b["type"] == "text":
                if b["text"]:
                    blocks.append({"type": "text", "text": b["text"]})
            elif b["type"] == "tool_use":
                blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]})
            else:
                c = b["content"]
                blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"],
                               "content": c if isinstance(c, str) else json.dumps(c)})
        if blocks:
            messages.append({"role": m["role"], "content": blocks})
    body: dict = {"anthropic_version": "vertex-2023-10-16", "max_tokens": req["max_tokens"], "messages": messages}
    if req["system"]:
        body["system"] = req["system"]
    if req["tools"]:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                         for t in req["tools"]]
    if req["json_schema"] is not None:
        body["tools"] = [{"name": _JSON_TOOL, "description": "Give the answer.", "input_schema": req["json_schema"]}]
        body["tool_choice"] = {"type": "tool", "name": _JSON_TOOL}
    return body


def _claude_answer(data: dict, *, json_mode: bool) -> dict:
    content = []
    for b in data.get("content") or []:
        if b.get("type") == "text" and b.get("text"):
            content.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "tool_use":
            if json_mode and b.get("name") == _JSON_TOOL:
                content.append({"type": "text", "text": json.dumps(b.get("input") or {})})
            else:
                content.append({"type": "tool_use", "id": b.get("id", ""), "name": b.get("name", ""),
                                "input": b.get("input") or {}})
    stop = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}.get(
        data.get("stop_reason") or "", "end_turn")
    if json_mode and stop == "tool_use":
        stop = "end_turn"
    usage = data.get("usage") or {}
    return {"content": content, "stop_reason": stop,
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0),
                      "output_tokens": int(usage.get("output_tokens") or 0)}}


# --- calling -------------------------------------------------------------------------------


def _url(model: str) -> str:
    """`vertex/<gemini>` → generateContent; `vertex/anthropic/<claude>` → rawPredict."""
    loc = getattr(settings, "APP_AI_LOCATION", "global") or "global"
    host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    base = f"https://{host}/v1/projects/{settings.HCP_VERTEX_PROJECT}/locations/{loc}/publishers"
    name = model.removeprefix(embeddings.VERTEX_PREFIX)
    if name.startswith(ANTHROPIC_PREFIX):
        return f"{base}/anthropic/models/{name.removeprefix(ANTHROPIC_PREFIX)}:rawPredict"
    return f"{base}/google/models/{name}:generateContent"


def _retention_ok(model: str) -> tuple[bool, str]:
    with _lock:
        hit = _retention.get(model)
        if hit and time.monotonic() - hit[2] < RETENTION_TTL:
            return hit[0], hit[1]
    ok, why = embeddings.check_retention(model)
    if not ok:
        logger.warning("app ai: not sending to %s — %s", model, why)
    with _lock:
        _retention[model] = (ok, why, time.monotonic())
    return ok, why


def _post(model: str, body: dict) -> dict:
    """The provider's JSON answer, or Refused(502) with a sentence a page can show."""
    try:
        resp = embeddings._vertex_session().post(_url(model), json=body, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — any transport failure is the same to the page
        raise Refused(502, f"the AI model could not be reached ({exc.__class__.__name__})") from exc
    if resp.status_code == 429:
        raise Refused(503, "the AI model is busy; try again in a minute")
    if not resp.ok:
        try:
            detail = (resp.json().get("error") or {}).get("message") or ""
        except ValueError:
            detail = ""
        logger.warning("app ai: %s answered %s: %s", model, resp.status_code, detail[:300])
        raise Refused(502, f"the AI model refused the request ({resp.status_code})"
                           + (f": {detail[:200]}" if resp.status_code == 400 and detail else ""))
    return resp.json()


def check_rate(user) -> None:
    now = timezone.now()
    mine = AppAiCall.objects.filter(user=user).exclude(status=AppAiCall.REFUSED)
    if mine.filter(created_at__gte=now - timedelta(hours=1)).count() >= settings.APP_AI_PER_HOUR:
        raise RateLimited(f"you have made {settings.APP_AI_PER_HOUR} AI calls in the last hour; try again later")
    if mine.filter(created_at__gte=now - timedelta(days=1)).count() >= settings.APP_AI_PER_DAY:
        raise RateLimited(f"you have made {settings.APP_AI_PER_DAY} AI calls today; try again tomorrow")


def complete(inst: AppInstallation, user, raw, *, space: str, manifest: dict) -> dict:
    """Answer one page's request as `user`, on `inst`'s behalf. Refused / RateLimited are the
    page's to show; every call that reaches the model, or is refused after the declaration
    check, is logged."""
    if not manifest.get("ai"):
        raise Refused(403, "this app does not declare AI use (its manifest's \"ai\")")
    if not enabled():
        raise Refused(503, "AI isn't set up on this canopy")
    req = clean(raw)
    model = model_for(req["model"])
    log = AppAiCall(installation=inst, user=user, space=space, alias=req["model"], model=model,
                    feature=req["feature"])

    def done(status: str, error: str = "", answer: dict | None = None, started: float | None = None) -> None:
        log.status, log.error = status, error[:300]
        if answer:
            log.input_tokens, log.output_tokens = answer["usage"]["input_tokens"], answer["usage"]["output_tokens"]
        if started is not None:
            log.ms = int((time.monotonic() - started) * 1000)
        log.save()

    if not model:
        raise Refused(503, f"no model is set for '{req['model']}'")
    try:
        check_rate(user)
    except RateLimited as exc:
        done(AppAiCall.REFUSED, str(exc))
        raise
    ok, why = _retention_ok(model)
    if not ok:
        done(AppAiCall.REFUSED, why)
        raise Refused(503, "AI is off: the model's provider is not verified to keep nothing it is sent")
    claude = model.removeprefix(embeddings.VERTEX_PREFIX).startswith(ANTHROPIC_PREFIX)
    started = time.monotonic()
    try:
        data = _post(model, _claude_body(req) if claude else _gemini_body(req))
        answer = _claude_answer(data, json_mode=req["json_schema"] is not None) if claude else _gemini_answer(data)
    except Refused as exc:
        done(AppAiCall.FAILED, str(exc), started=started)
        raise
    done(AppAiCall.OK, answer=answer, started=started)
    text = "".join(b["text"] for b in answer["content"] if b["type"] == "text")
    out = {"text": text, "content": answer["content"], "stop_reason": answer["stop_reason"],
           "model": req["model"], "usage": answer["usage"]}
    if req["json_schema"] is not None:
        try:
            out["json"] = json.loads(text) if text else None
        except ValueError:
            out["json"] = None
    return out
