"""
AI client abstraction — supports two backends:

  AI_BACKEND=api  → Direct Anthropic Python SDK (requires ANTHROPIC_API_KEY)
  AI_BACKEND=cli  → Claude Code CLI (uses subscription login via `claude setup-token`)

Both backends expose the same interface: `call_ai()` for synchronous calls
and `stream_message()` for SSE streaming.
"""
import logging
import os
import subprocess

import anthropic
from django.conf import settings

logger = logging.getLogger(__name__)

#: The model every call here defaults to.
#:
#: One constant, not four default arguments. It was written out four times
#: (`_api_call`, `_api_stream`, `call_ai`, `stream_message`), which is how it
#: came to be a generation stale — moving it meant finding every copy, so nobody
#: did. Overridable per deployment without a code change; a caller that passes
#: `model=` still wins, which is what the per-call argument is for.
#:
#: Read through `_default_model()` rather than captured at import: a default
#: argument binds once when the module loads, so a settings override would never
#: be seen. That exact bug cost this repo a 25-second test file once
#: (DEFAULT_TIMEOUT_SECONDS in page_actions).
DEFAULT_MODEL = "claude-sonnet-5"

_client = None
_consecutive_failures = 0
_CIRCUIT_BREAKER_THRESHOLD = 5


def _default_model() -> str:
    return getattr(settings, "ANTHROPIC_MODEL", None) or DEFAULT_MODEL


def _get_backend():
    return getattr(settings, "AI_BACKEND", "api")


def _get_clean_env():
    """Env without ANTHROPIC_API_KEY so CLI uses subscription login."""
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


# --- Circuit breaker ---

def is_circuit_open():
    return _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD


def record_success():
    global _consecutive_failures
    _consecutive_failures = 0


def record_failure():
    global _consecutive_failures
    _consecutive_failures += 1


# --- Auth status (simple check, no login flow) ---

def cli_auth_status():
    """Check if Claude CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10,
            env=_get_clean_env(),
        )
        output = (result.stdout + result.stderr).strip()
        logged_in = result.returncode == 0 and "not logged in" not in output.lower()
        return {"installed": True, "logged_in": logged_in, "output": output}
    except FileNotFoundError:
        return {"installed": False, "logged_in": False, "output": "claude CLI not found"}
    except Exception as e:
        return {"installed": True, "logged_in": False, "output": str(e)}


# --- API backend ---

def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _api_call(system_prompt, user_message, model=None, max_tokens=4096):
    client = get_client()
    response = client.messages.create(
        model=model or _default_model(), max_tokens=max_tokens, system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def _api_stream(system_prompt, user_message, model=None):
    client = get_client()
    with client.messages.stream(
        model=model or _default_model(), max_tokens=4096, system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            yield text


# --- CLI backend ---

def _cli_call(system_prompt, user_message, max_tokens=4096):
    """Call Claude Code CLI using subscription login."""
    combined_prompt = f"{system_prompt}\n\n{user_message}"
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=combined_prompt,
            capture_output=True, text=True, timeout=120,
            env=_get_clean_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
        return result.stdout
    except FileNotFoundError:
        raise RuntimeError("Claude Code CLI not found. Install it or set AI_BACKEND=api.")


async def _cli_stream(system_prompt, user_message):
    text = _cli_call(system_prompt, user_message)
    yield text


# --- Public interface ---

def call_ai(system_prompt, user_message, model=None, max_tokens=4096):
    """Synchronous AI call. Returns the response text."""
    if is_circuit_open():
        raise RuntimeError("AI circuit breaker open — too many consecutive failures")
    backend = _get_backend()
    try:
        if backend == "cli":
            result = _cli_call(system_prompt, user_message, max_tokens)
        else:
            result = _api_call(system_prompt, user_message, model, max_tokens)
        record_success()
        return result
    except Exception:
        record_failure()
        raise


async def stream_message(system_prompt, user_message, model=None):
    """Async streaming AI call. Yields text chunks."""
    if is_circuit_open():
        raise RuntimeError("AI circuit breaker open — too many consecutive failures")
    backend = _get_backend()
    try:
        if backend == "cli":
            async for chunk in _cli_stream(system_prompt, user_message):
                yield chunk
        else:
            async for chunk in _api_stream(system_prompt, user_message, model):
                yield chunk
        record_success()
    except Exception:
        record_failure()
        raise
