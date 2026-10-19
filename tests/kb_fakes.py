"""A stand-in embedding model and a small KB for the knowledge-base vector tests.

The fake model maps words to concepts, so a paraphrase that shares no word with a
passage still lands near it — the property hybrid search exists for — and every
other word to a hashed dimension of its own."""
from __future__ import annotations

import hashlib
import json
import math
import re

from apps.agents.kb_vectors import DIMENSIONS
from apps.contacts import embeddings
from canopy_kb import build

MODEL = "vertex/fake-embedding"
CONCEPTS = {"cholera": 0, "vaccine": 1, "vaccines": 1, "inoculation": 1, "ocv": 1, "jab": 1,
            "scarce": 2, "shortage": 2, "limited": 2, "stockpiles": 2,
            "chlorine": 3, "soap": 3, "hygiene": 3, "lead": 4, "paint": 4}


def vector(text: str) -> list[float]:
    v = [0.0] * DIMENSIONS
    for word in re.findall(r"[a-z]+", text.lower()):
        dim = CONCEPTS.get(word)
        if dim is None:
            dim = 16 + int(hashlib.sha1(word.encode()).hexdigest(), 16) % 700
            v[dim] += 0.2
        else:
            v[dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def install(monkeypatch, settings, calls: list | None = None, fail=lambda text: False):
    """Point KB embeddings at the fake model, verified to keep nothing."""
    settings.KB_EMBEDDINGS_MODEL = MODEL
    monkeypatch.setitem(embeddings._retention, "model", None)
    monkeypatch.setattr(embeddings, "_check_retention", lambda m: (True, "keeps nothing"))

    def invoke(model_id, texts, purpose, dimensions=768):
        assert model_id == MODEL and dimensions == DIMENSIONS
        out = []
        for t in texts:
            if fail(t):
                raise TimeoutError("vertex timed out")
            if calls is not None:
                calls.append((purpose, t))
            out.append(vector(t))
        return out
    monkeypatch.setattr(embeddings, "_invoke", invoke)
    monkeypatch.setattr("apps.agents.kb_vectors.time.sleep", lambda s: None)


ITEMS = [
    {"id": "doc:ocv", "type": "gdoc", "title": "Oral cholera vaccines", "url": "https://gw/ocv",
     "date": "2025-01-02", "text": "OCV stockpiles are limited.", "sha256": "1"},
    {"id": "blog:wash", "type": "blog_post", "title": "WASHmobile", "url": "https://gw/wash",
     "date": "2026-07-23", "text": "Chlorine and soap for households.", "sha256": "2"},
    {"id": "page:lead", "type": "page", "title": "Lead exposure", "url": "https://gw/lead",
     "date": "", "text": "Lead paint in homes.", "sha256": "3"},
]


def build_kb(path, items=ITEMS):
    bundle = path.parent / f"{path.stem}-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "manifest.json").write_text(json.dumps({"name": "givewell", "publisher": "GiveWell"}))
    (bundle / "items.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    build(bundle, path)
    return path
