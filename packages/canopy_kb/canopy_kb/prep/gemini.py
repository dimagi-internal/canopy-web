"""Gemini on Vertex AI, for the stages that read documents (OCR, cards, aliases).

Auth is a Google access token: `$GOOGLE_ACCESS_TOKEN` if set, else `gcloud auth
print-access-token` for the machine's signed-in account, refreshed every 25 minutes.
Project and region: `$CANOPY_KB_VERTEX_PROJECT` (default canopy-494811, where canopy-web's
KB reranker runs) and `$CANOPY_KB_VERTEX_REGION` (default us-central1). A knowledge base
holds public material only, so nothing private is sent.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

PROJECT = os.environ.get("CANOPY_KB_VERTEX_PROJECT", "canopy-494811")
REGION = os.environ.get("CANOPY_KB_VERTEX_REGION", "us-central1")
FLASH = "gemini-2.5-flash"
PRO = "gemini-2.5-pro"

_tok = {"v": "", "at": 0.0}
_lock = threading.Lock()


def token() -> str:
    if os.environ.get("GOOGLE_ACCESS_TOKEN"):
        return os.environ["GOOGLE_ACCESS_TOKEN"]
    with _lock:
        if time.time() - _tok["at"] > 1500:
            r = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("no Google access token: run `gcloud auth login` (or set GOOGLE_ACCESS_TOKEN). "
                                   + (r.stderr or "").strip()[:200])
            _tok["v"], _tok["at"] = r.stdout.strip(), time.time()
        return _tok["v"]


def generate(model: str, parts: list[dict], *, json_out: bool = True, max_tokens: int = 8192,
             thinking: int | None = 0, timeout: float = 300, tries: int = 6) -> tuple[str, dict]:
    """One generateContent call: (text, usageMetadata). Retries 429/5xx with backoff."""
    import requests
    url = (f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/"
           f"publishers/google/models/{model}:generateContent")
    cfg: dict = {"temperature": 0, "maxOutputTokens": max_tokens}
    if json_out:
        cfg["responseMimeType"] = "application/json"
    if thinking is not None:
        cfg["thinkingConfig"] = {"thinkingBudget": thinking}
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}
    err = ""
    for n in range(tries):
        try:
            r = requests.post(url, json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {token()}", "x-goog-user-project": PROJECT})
            if r.status_code == 200:
                d = r.json()
                text = "".join(p.get("text", "") for p in d["candidates"][0]["content"].get("parts", []))
                return text, d.get("usageMetadata", {})
            err = f"{r.status_code}: {r.text[:200]}"
            if r.status_code == 401:
                _tok["at"] = 0.0
            elif r.status_code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # noqa: BLE001
            err = str(e)[:200]
        time.sleep(min(60, 2 ** n * 2))
    raise RuntimeError(f"{model}: {err}")


def generate_json(model: str, parts: list[dict], **kw):
    text, usage = generate(model, parts, json_out=True, **kw)
    return json.loads(text), usage
