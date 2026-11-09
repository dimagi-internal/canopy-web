"""The two models, both run on this Mac: NVIDIA Parakeet TDT 0.6B v3 for words (through
MLX, on the GPU) and NVIDIA TitaNet-small for telling voices apart (through ONNX).

Neither sends audio anywhere. They are downloaded once, on first use or by
`canopy-listen models`, into Hugging Face's cache and ~/.canopy-listen/models.
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import numpy as np

from .live import SAMPLE_RATE, Sentence
from .store import home

ASR_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
VOICE_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                   "speaker-recongition-models/nemo_en_titanet_small.onnx")


def voice_model_path() -> Path:
    path = home() / "models" / "nemo_en_titanet_small.onnx"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(VOICE_MODEL_URL, tmp)  # noqa: S310 — a fixed https URL
        tmp.rename(path)
    return path


class Recogniser:
    def __init__(self):
        import mlx.core as mx
        from parakeet_mlx import from_pretrained
        from parakeet_mlx.audio import get_logmel

        self._mx, self._logmel = mx, get_logmel
        self.model = from_pretrained(ASR_MODEL)

    def __call__(self, audio: np.ndarray) -> list[Sentence]:
        config = self.model.preprocessor_config
        if len(audio) < config.hop_length * 2:
            return []
        # float32: get_logmel reinterprets the STFT's bytes by the input's dtype, so a
        # bfloat16 input comes out twice as wide (what parakeet's own loader hands it is float32).
        mel = self._logmel(self._mx.array(audio, dtype=self._mx.float32), config)
        result = self.model.generate(mel)[0]
        return [Sentence(text=s.text, start=float(s.start), end=float(s.end)) for s in result.sentences]


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


def download() -> None:
    """Fetch both models now, so the first meeting doesn't wait on them."""
    Recogniser()
    voice_model_path()
