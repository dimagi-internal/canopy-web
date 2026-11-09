"""Build `canopy-listen bench`'s test set from published recordings with known answers.

  * libri40: 40 read sentences, one speaker (LibriSpeech dev-clean sample): words.
  * meeting4: 40 turns by 4 people, 0.25-0.9 s apart (LibriSpeech test-clean): words and who.
  * pyannote-sample: a real 2-person conversation (pyannote's tutorial clip): who.

LibriSpeech is CC BY 4.0; the pyannote sample ships with pyannote-audio (MIT). About 360 MB
is downloaded once and only ~25 MB kept.
"""
from __future__ import annotations

import collections
import io
import json
import random
import urllib.request
from pathlib import Path

import numpy as np

SR = 16000
HF = "https://huggingface.co/api/datasets"
PYANNOTE = "https://github.com/pyannote/pyannote-audio/raw/develop/tutorials/assets/"


def _get(url: str, path: Path) -> Path:
    if not path.exists():
        urllib.request.urlretrieve(url, path)  # noqa: S310 — fixed https URLs
    return path


def build(directory: Path, report=print) -> None:
    import pyarrow.parquet as pq
    import soundfile as sf

    directory.mkdir(parents=True, exist_ok=True)
    report("pyannote sample")
    _get(PYANNOTE + "sample.wav", directory / "pyannote-sample.wav")
    rttm = _get(PYANNOTE + "sample.rttm", directory / "pyannote-sample.rttm").read_text().splitlines()
    turns = [[float(x.split()[3]), float(x.split()[3]) + float(x.split()[4]), x.split()[7]] for x in rttm if x]
    (directory / "pyannote-sample.turns.json").write_text(json.dumps(turns))

    report("libri40 (LibriSpeech, one speaker)")
    dev = _get(f"{HF}/hf-internal-testing/librispeech_asr_dummy/parquet/clean/validation/0.parquet",
               directory / ".dev.parquet")
    rows = pq.read_table(dev).to_pylist()[:40]
    clips, refs = [], []
    for r in rows:
        a, _ = sf.read(io.BytesIO(r["audio"]["bytes"]), dtype="float32")
        clips += [a, np.zeros(int(0.8 * SR), dtype=np.float32)]
        refs.append(r["text"].lower())
    sf.write(directory / "libri40.wav", np.concatenate(clips), SR)
    (directory / "libri40.txt").write_text(" ".join(refs))

    report("meeting4 (LibriSpeech, four speakers; a 350 MB download)")
    test = _get(f"{HF}/openslr/librispeech_asr/parquet/clean/test/0.parquet", directory / ".test.parquet")
    by = collections.defaultdict(list)
    for r in pq.read_table(test).to_pylist():
        by[r["speaker_id"]].append(r)
    rng = random.Random(7)
    people = rng.sample(sorted(by), 4)
    pos, clips, turns, refs, last = 0.0, [], [], [], None
    for _ in range(40):
        who = rng.choice([p for p in people if p != last])
        last = who
        r = by[who].pop()
        a, _ = sf.read(io.BytesIO(r["audio"]["bytes"]), dtype="float32")
        gap = rng.uniform(0.25, 0.9)
        clips += [a, np.zeros(int(gap * SR), dtype=np.float32)]
        turns.append([round(pos, 2), round(pos + len(a) / SR, 2), str(who)])
        refs.append(r["text"].lower())
        pos += len(a) / SR + gap
    sf.write(directory / "meeting4.wav", np.concatenate(clips), SR)
    (directory / "meeting4.txt").write_text(" ".join(refs))
    (directory / "meeting4.turns.json").write_text(json.dumps(turns))
    for big in (dev, test):
        big.unlink(missing_ok=True)
    report(f"ready: {directory}")
