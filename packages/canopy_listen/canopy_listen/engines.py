"""Every model canopy-listen can use, by name, and how to load each.

Two kinds, chosen in ~/.canopy-listen/config.json (`canopy-listen config`):

  * SPEECH (`asr`): audio -> sentences. A model that times its sentences is used BY
    SENTENCE (lower latency); one that only returns text is used BY PAUSE.
  * SPEAKERS (`speakers`, live) and FINAL (`final`, once the meeting ends): who spoke.

Adding a model is one entry here. `canopy-listen bench` compares them on real speech.
All run on this Mac, through MLX (GPU) or ONNX (CPU); none sends audio anywhere.
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .live import SAMPLE_RATE, EmbeddingLabeller, Labeller, Sentence, Speakers, TimelineLabeller
from .store import home


@dataclass(frozen=True)
class Speech:
    name: str
    repo: str
    backend: str  # parakeet-mlx | mlx-whisper | mlx-audio
    timed: bool
    languages: str
    note: str
    gated: bool = False  # needs terms accepted on Hugging Face, and `hf auth login` on this Mac


SPEECH = {s.name: s for s in [
    Speech("parakeet-v3", "mlx-community/parakeet-tdt-0.6b-v3", "parakeet-mlx", True, "25 European",
           "NVIDIA, 0.6B. The default: fastest, accurate, timed."),
    Speech("parakeet-v2", "mlx-community/parakeet-tdt-0.6b-v2", "parakeet-mlx", True, "English",
           "NVIDIA, 0.6B, English only; slightly better English than v3."),
    Speech("whisper-turbo", "mlx-community/whisper-large-v3-turbo", "mlx-whisper", True, "99",
           "OpenAI Whisper large-v3-turbo. Widest language coverage."),
    Speech("qwen3-asr-1.7b", "mlx-community/Qwen3-ASR-1.7B-8bit", "mlx-audio", False, "52",
           "Alibaba Qwen3-ASR 1.7B, 8-bit. Text only, so used by pause."),
    Speech("qwen3-asr-0.6b", "mlx-community/Qwen3-ASR-0.6B-4bit", "mlx-audio", False, "52",
           "Alibaba Qwen3-ASR 0.6B, 4-bit. Small and fast; text only."),
    # The MLX community copies are broken (one has no config, one decodes gibberish), so the
    # official weights, which are gated.
    Speech("cohere-transcribe", "CohereLabs/cohere-transcribe-03-2026", "mlx-audio", False, "14",
           "Cohere Transcribe 2B. Tops the open leaderboard; what Superwhisper's Local mode uses.", gated=True),
    Speech("granite-speech-1b", "mlx-community/granite-4.0-1b-speech-8bit", "mlx-audio", False, "English+",
           "IBM Granite Speech 1B, 8-bit. Text only."),
]}

SPEAKERS = {
    "titanet": "NVIDIA TitaNet-small voice fingerprints, matched as people talk (ONNX). The default.",
    "sortformer": "NVIDIA Sortformer streaming diarization, up to 4 speakers, handles overlap (MLX).",
    "none": "Everything is Speaker 1.",
}
FINAL = {
    "none": "Keep the live labels.",
    "sortformer": "Re-run Sortformer over the whole recording when the meeting ends, and relabel.",
    "pyannote": "pyannote segmentation-3.0 + TitaNet clustering over the whole recording (ONNX), and relabel.",
}

VOICE_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                   "speaker-recongition-models/nemo_en_titanet_small.onnx")
SEGMENTATION_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
SORTFORMER_REPO = "mlx-community/diar_streaming_sortformer_4spk-v2.1-fp16"


def _fetch(url: str, name: str) -> Path:
    path = home() / "models" / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)  # noqa: S310 — fixed https URLs
        tmp.rename(path)
    return path


def voice_model_path() -> Path:
    return _fetch(VOICE_MODEL_URL, "nemo_en_titanet_small.onnx")


def segmentation_model_path() -> Path:
    target = home() / "models" / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    if not target.exists():
        import tarfile

        archive = _fetch(SEGMENTATION_URL, "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
        with tarfile.open(archive) as tar:
            tar.extractall(archive.parent, filter="data")
    return target


def hf_logged_in() -> bool:
    """Without asking the network: a refused download is slow, and loud."""
    import os

    if os.environ.get("HF_TOKEN"):
        return True
    cache = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    return (cache / "token").exists()


def _speech_span(audio: np.ndarray) -> tuple[float, float]:
    """Where the speech is, for a model that doesn't say: first and last loud 50 ms."""
    frame = SAMPLE_RATE // 20
    n = len(audio) // frame
    if n == 0:
        return 0.0, len(audio) / SAMPLE_RATE
    rms = np.sqrt(np.mean(np.square(audio[: n * frame].reshape(n, frame)), axis=1))
    loud = np.nonzero(rms >= max(0.004, 3.0 * float(np.percentile(rms, 10))))[0]
    if len(loud) == 0:
        return 0.0, len(audio) / SAMPLE_RATE
    return loud[0] * 0.05, min(len(audio) / SAMPLE_RATE, (loud[-1] + 1) * 0.05)


class Recogniser:
    """One speech model, loaded once: audio -> [Sentence]."""

    def __init__(self, name: str, language: str = "en"):
        if name not in SPEECH:
            raise ValueError(f"unknown speech model {name!r}; one of: {', '.join(SPEECH)}")
        self.spec, self.language = SPEECH[name], language
        if self.spec.gated and not hf_logged_in():
            raise PermissionError(f"{name} needs its terms accepted on Hugging Face and `hf auth login` on this Mac; "
                                  "see canopy-listen's README")
        backend = self.spec.backend
        if backend == "parakeet-mlx":
            import mlx.core as mx
            from parakeet_mlx import from_pretrained
            from parakeet_mlx.audio import get_logmel

            self._mx, self._logmel = mx, get_logmel
            self.model = from_pretrained(self.spec.repo)
        elif backend == "mlx-whisper":
            import mlx_whisper

            self._whisper = mlx_whisper
            # Loads and caches the weights now, rather than on the first words.
            self._whisper.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=self.spec.repo,
                                     language=language)
        else:
            from mlx_audio.stt.utils import load_model

            self.model = load_model(self.spec.repo)

    @property
    def timed(self) -> bool:
        return self.spec.timed

    def __call__(self, audio: np.ndarray) -> list[Sentence]:
        if len(audio) < SAMPLE_RATE // 4:
            return []
        backend = self.spec.backend
        if backend == "parakeet-mlx":
            config = self.model.preprocessor_config
            # float32: get_logmel reinterprets the STFT's bytes by the input's dtype, so a
            # bfloat16 input comes out twice as wide (parakeet's own loader hands it float32).
            mel = self._logmel(self._mx.array(audio, dtype=self._mx.float32), config)
            result = self.model.generate(mel)[0]
            return [Sentence(s.text, float(s.start), float(s.end)) for s in result.sentences]
        if backend == "mlx-whisper":
            result = self._whisper.transcribe(np.ascontiguousarray(audio, dtype=np.float32),
                                              path_or_hf_repo=self.spec.repo, language=self.language,
                                              condition_on_previous_text=False)
            return [Sentence(s["text"], float(s["start"]), float(s["end"])) for s in result.get("segments", [])
                    if not s.get("no_speech_prob", 0) > 0.6]
        result = self.model.generate(np.ascontiguousarray(audio, dtype=np.float32))
        text = (getattr(result, "text", "") or "").strip()
        if not text:
            return []
        start, end = _speech_span(audio)
        return [Sentence(text, start, end)]


class VoiceEmbedder:
    def __init__(self):
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(voice_model_path()), num_threads=2)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        stream = self.extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(audio, dtype=np.float32))
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return np.zeros(self.extractor.dim, dtype=np.float32)
        return np.array(self.extractor.compute(stream), dtype=np.float32)


class SortformerStream:
    """Sortformer fed as audio arrives: (start, end, speaker) turns in meeting time."""

    def __init__(self, threshold: float = 0.5):
        from mlx_audio.vad.utils import load

        self.model = load(SORTFORMER_REPO)
        self.state = self.model.init_streaming_state()
        self.threshold = threshold
        self.pending = np.zeros(0, dtype=np.float32)
        self.at = 0.0

    def __call__(self, samples: np.ndarray) -> list:
        # Feed whole seconds: the model's frames are 80 ms and tiny chunks waste its steps.
        self.pending = np.concatenate([self.pending, samples])
        if len(self.pending) < SAMPLE_RATE:
            return []
        chunk, self.pending = self.pending, np.zeros(0, dtype=np.float32)
        out, self.state = self.model.feed(chunk, self.state, sample_rate=SAMPLE_RATE, threshold=self.threshold)
        return [(float(s.start), float(s.end), int(s.speaker)) for s in out.segments]


def make_labeller(name: str, same_voice: float = 0.66) -> Labeller:
    if name == "titanet":
        return EmbeddingLabeller(VoiceEmbedder(), Speakers(same_voice=same_voice))
    if name == "sortformer":
        return TimelineLabeller(SortformerStream())
    if name == "none":
        return Labeller()
    raise ValueError(f"unknown speakers option {name!r}; one of: {', '.join(SPEAKERS)}")


def final_turns(name: str, audio: np.ndarray) -> list:
    """Who spoke when over the whole recording, as (start, end, speaker), or [] for none.

    Run in a fresh process: once TitaNet's ONNX runtime has run in a process, Sortformer
    (MLX) in the same process finds no speech at all (measured: 145 turns fresh, 0 after)."""
    if name == "none" or len(audio) < SAMPLE_RATE:
        return []
    import json
    import subprocess
    import sys
    import tempfile

    import soundfile as sf

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        sf.write(wav, audio, SAMPLE_RATE)
        done = subprocess.run([sys.executable, "-m", "canopy_listen", "_turns", name, str(wav)],
                              capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise RuntimeError(f"final pass {name} exited {done.returncode}")
    return [tuple(t) for t in json.loads(done.stdout.strip().splitlines()[-1])]


def turns_here(name: str, audio: np.ndarray) -> list:
    """final_turns' work, in this process."""
    if name == "none" or len(audio) < SAMPLE_RATE:
        return []
    if name == "sortformer":
        from mlx_audio.vad.utils import load

        out = load(SORTFORMER_REPO).generate(audio, sample_rate=SAMPLE_RATE)
        return [(float(s.start), float(s.end), int(s.speaker)) for s in out.segments]
    if name == "pyannote":
        import sherpa_onnx as so

        config = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation_model_path()))),
            embedding=so.SpeakerEmbeddingExtractorConfig(model=str(voice_model_path())),
            clustering=so.FastClusteringConfig(num_clusters=-1, threshold=0.5),
            min_duration_on=0.3, min_duration_off=0.5)
        result = so.OfflineSpeakerDiarization(config).process(audio).sort_by_start_time()
        return [(float(s.start), float(s.end), int(s.speaker)) for s in result]
    raise ValueError(f"unknown final option {name!r}; one of: {', '.join(FINAL)}")


def relabel(segments: list, turns: list) -> list:
    """Each segment's speaker becomes whoever spoke most of it in `turns`, numbered by first
    appearance. A segment the turns don't cover goes with the one before it: keeping its live
    label would mix two numberings."""
    if not turns:
        return segments
    order: dict[int, int] = {}
    out, last = [], 0
    for seg in segments:
        overlap: dict[int, float] = {}
        for a, b, who in turns:
            o = min(b, seg["end"]) - max(a, seg["start"])
            if o > 0:
                overlap[who] = overlap.get(who, 0.0) + o
        who = max(overlap, key=overlap.get) if overlap else None
        if who is not None:
            last = order.setdefault(who, len(order))
        out.append({**seg, "speaker": last})
    return out
