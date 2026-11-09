"""Compare models on real speech, the way a live meeting uses them.

Each recording is replayed through the live engine in 2-second steps, as fast as the
models allow, so what is measured is what a meeting gets:

  * WER: words wrong / reference words, after lower-casing and dropping punctuation.
  * LAG: how long after a sentence ends it becomes final, in meeting time (median; add a
    step's compute for wall time).
  * SPEED: seconds of compute per second of audio (lower is better; under 1 is live).
  * SPEAKERS: the share of speech time given to the right person, after matching each
    found speaker to the reference speaker it overlaps most (one to one), and how many
    speakers were found against how many there were.

`canopy-listen bench-data DIR` builds a test set from published recordings.
"""
from __future__ import annotations

import itertools
import json
import re
import statistics
import time
from pathlib import Path

import numpy as np

from .live import SAMPLE_RATE, Labeller, LiveTranscriber

STEP = 2.0


def words(text: str) -> list[str]:
    text = re.sub(r"[^\w\s']", " ", text.lower()).replace("'", "")
    return text.split()


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = words(reference), words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def speaker_accuracy(segments: list, turns: list) -> dict:
    """segments: [{start, end, speaker}]; turns: [[start, end, name]] (the reference)."""
    weight: dict[tuple, float] = {}
    total = 0.0
    for seg in segments:
        for a, b, name in turns:
            o = min(b, seg["end"]) - max(a, seg["start"])
            if o > 0:
                weight[(seg["speaker"], name)] = weight.get((seg["speaker"], name), 0.0) + o
                total += o
    found = sorted({s["speaker"] for s in segments})
    names = sorted({t[2] for t in turns})
    best = 0.0
    if total and len(found) <= 7:
        small, large = (found, names) if len(found) <= len(names) else (names, found)
        for perm in itertools.permutations(large, len(small)):
            pairs = zip(small, perm) if small is found else zip(perm, small)
            best = max(best, sum(weight.get(p, 0.0) for p in pairs))
    elif total:  # greedy for many speakers
        used_h, used_r = set(), set()
        for (h, r), w in sorted(weight.items(), key=lambda kv: -kv[1]):
            if h not in used_h and r not in used_r:
                best += w
                used_h.add(h)
                used_r.add(r)
    return {"accuracy": round(best / total, 3) if total else None, "found": len(found), "actual": len(names)}


def run_live(recognise, audio: np.ndarray, *, labeller: Labeller | None = None, timed: bool = True) -> dict:
    live = LiveTranscriber(recognise, labeller=labeller or Labeller(), timed=timed)
    segments, lags, compute = [], [], 0.0
    step = int(STEP * SAMPLE_RATE)
    for i in range(0, len(audio), step):
        live.feed(audio[i:i + step])
        t = time.perf_counter()
        out = live.step()
        compute += time.perf_counter() - t
        now = (i + len(audio[i:i + step])) / SAMPLE_RATE
        for s in out:
            lags.append(now - s.end)
        segments += out
    t = time.perf_counter()
    segments += live.step(final=True)
    compute += time.perf_counter() - t
    seconds = len(audio) / SAMPLE_RATE
    return {
        "segments": [{"text": s.text, "start": s.start, "end": s.end, "speaker": s.speaker} for s in segments],
        "lag": round(statistics.median(lags), 1) if lags else None,
        "speed": round(compute / seconds, 3) if seconds else None,
    }


def load_set(directory: Path) -> list[dict]:
    """Every <name>.wav, with <name>.txt (reference words) and/or <name>.turns.json."""
    from .recorder import read_audio

    items = []
    for wav in sorted(directory.glob("*.wav")):
        item = {"name": wav.stem, "audio": read_audio(str(wav))}
        if wav.with_suffix(".txt").exists():
            item["reference"] = wav.with_suffix(".txt").read_text()
        turns = wav.with_name(wav.stem + ".turns.json")
        if turns.exists():
            item["turns"] = json.loads(turns.read_text())
        items.append(item)
    return items


def run(directory: Path, *, asr: list, speakers: list, final: list, base_asr: str, language: str = "en",
        same_voice: float = 0.66, report=print) -> dict:
    from . import engines

    items = load_set(directory)
    results = {"speech": [], "speakers": [], "final": []}
    for name in asr:
        t = time.perf_counter()
        try:
            recognise = engines.Recogniser(name, language)
        except Exception as exc:  # noqa: BLE001 — one model failing mustn't end the comparison
            results["speech"].append({"model": name, "error": f"{type(exc).__name__}: {exc}"[:200]})
            report(f"{name}: failed to load ({type(exc).__name__})")
            continue
        loaded = round(time.perf_counter() - t, 1)
        for item in [i for i in items if "reference" in i]:
            out = run_live(recognise, item["audio"], timed=recognise.timed)
            row = {"model": name, "set": item["name"], "wer": round(wer(item["reference"], " ".join(
                s["text"] for s in out["segments"])), 4), "lag": out["lag"], "speed": out["speed"], "load": loaded,
                "by": "sentence" if recognise.timed else "pause"}
            results["speech"].append(row)
            report(f"{name:18} {item['name']:12} WER {row['wer']:.1%}  lag {row['lag']}s  speed {row['speed']}x  "
                   f"(load {loaded}s, by {row['by']})")
        del recognise
    diar_items = [i for i in items if "turns" in i]
    # Sortformer first: once TitaNet's ONNX runtime has run in a process, Sortformer in it
    # finds nothing (final passes run in their own process for the same reason).
    speakers = sorted(speakers, key=lambda o: o == "titanet")
    if diar_items and (speakers or final):
        recognise = engines.Recogniser(base_asr, language)
        for item in diar_items:
            live_segments = None
            for option in speakers:
                try:
                    out = run_live(recognise, item["audio"], labeller=engines.make_labeller(option, same_voice),
                                   timed=recognise.timed)
                except Exception as exc:  # noqa: BLE001
                    results["speakers"].append({"speakers": option, "set": item["name"], "error": type(exc).__name__})
                    report(f"speakers {option:11} {item['name']:12} failed ({type(exc).__name__}: {exc})"[:200])
                    continue
                score = speaker_accuracy(out["segments"], item["turns"])
                if option == "titanet":
                    live_segments = out["segments"]
                results["speakers"].append({"speakers": option, "set": item["name"], **score})
                report(f"speakers {option:11} {item['name']:12} right {score['accuracy']:.1%}  "
                       f"found {score['found']} of {score['actual']}")
            if live_segments is None:
                live_segments = run_live(recognise, item["audio"], timed=recognise.timed)["segments"]
            for option in final:
                t = time.perf_counter()
                try:
                    relabelled = engines.relabel(live_segments, engines.final_turns(option, item["audio"]))
                except Exception as exc:  # noqa: BLE001
                    results["final"].append({"final": option, "set": item["name"], "error": type(exc).__name__})
                    report(f"final    {option:11} {item['name']:12} failed ({type(exc).__name__}: {exc})"[:200])
                    continue
                took = round(time.perf_counter() - t, 1)
                score = speaker_accuracy(relabelled, item["turns"])
                results["final"].append({"final": option, "set": item["name"], "seconds": took, **score})
                report(f"final    {option:11} {item['name']:12} right {score['accuracy']:.1%}  "
                       f"found {score['found']} of {score['actual']}  ({took}s)")
    return results
