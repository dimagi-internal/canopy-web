"""The comparison is only as good as its scoring, and the choice only as good as its
validation; neither needs a model."""
import json

import numpy as np
import pytest
from canopy_listen import bench, cli, config, engines
from canopy_listen.live import SAMPLE_RATE, Sentence


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_LISTEN_HOME", str(tmp_path))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))


def test_word_error_rate_ignores_case_and_punctuation_and_counts_every_kind_of_mistake():
    assert bench.wer("The cat sat.", "the cat, sat") == 0.0
    assert bench.wer("the cat sat on the mat", "the cat sat on mat") == pytest.approx(1 / 6)
    assert bench.wer("a b c", "a x c d") == pytest.approx(2 / 3)
    assert bench.wer("", "") == 0.0


def test_speaker_accuracy_matches_found_speakers_to_real_ones_one_to_one():
    turns = [[0, 10, "ann"], [10, 20, "bob"]]
    swapped = [{"start": 0, "end": 10, "speaker": 1}, {"start": 10, "end": 20, "speaker": 0}]
    assert bench.speaker_accuracy(swapped, turns) == {"accuracy": 1.0, "found": 2, "actual": 2}
    merged = [{"start": 0, "end": 20, "speaker": 0}]
    assert bench.speaker_accuracy(merged, turns)["accuracy"] == 0.5
    split = [{"start": 0, "end": 5, "speaker": 0}, {"start": 5, "end": 10, "speaker": 2},
             {"start": 10, "end": 20, "speaker": 1}]
    assert bench.speaker_accuracy(split, turns) == {"accuracy": 0.75, "found": 3, "actual": 2}


def test_a_live_replay_reports_what_was_said_how_late_and_how_fast():
    def recognise(audio):
        n = len(audio) / SAMPLE_RATE
        return [Sentence("hello there", 0.0, 1.0)] if n >= 1.0 else []

    audio = np.concatenate([np.full(SAMPLE_RATE, 0.1, np.float32), np.zeros(4 * SAMPLE_RATE, np.float32)])
    out = bench.run_live(recognise, audio)
    assert [s["text"] for s in out["segments"]] == ["hello there"]
    assert out["lag"] is not None and out["lag"] >= 1.0 and out["speed"] is not None


def test_relabel_gives_each_segment_to_whoever_spoke_most_of_it_numbered_by_first_appearance():
    segments = [{"text": "a", "start": 0, "end": 4, "speaker": 0}, {"text": "b", "start": 4, "end": 9, "speaker": 0},
                {"text": "c", "start": 30, "end": 31, "speaker": 0}]
    turns = [(0, 4.5, 7), (4.5, 10, 3)]
    out = engines.relabel(segments, turns)
    assert [s["speaker"] for s in out] == [0, 1, 1]  # 7 -> 0, 3 -> 1; uncovered goes with the one before
    assert engines.relabel(segments, []) == segments


def test_config_takes_only_known_models_and_sane_thresholds(capsys):
    assert config.load()["asr"] == "parakeet-v3"
    assert cli.main(["config", "asr", "qwen3-asr-1.7b"]) == 0
    assert json.loads(capsys.readouterr().out)["asr"] == "qwen3-asr-1.7b"
    assert config.load()["asr"] == "qwen3-asr-1.7b"
    assert cli.main(["config", "asr", "otter"]) == 2
    assert cli.main(["config", "same_voice", "2"]) == 2
    assert cli.main(["config", "same_voice", "0.7"]) == 0 and config.load()["same_voice"] == 0.7
    assert cli.main(["config", "colour", "red"]) == 2


def test_a_gated_model_is_refused_before_any_download_without_a_login():
    with pytest.raises(PermissionError, match="hf auth login"):
        engines.Recogniser("cohere-transcribe")


def test_every_model_says_how_it_is_used(capsys):
    assert cli.main(["models", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    asr = [r for r in rows if r["kind"] == "asr"]
    assert {r["name"] for r in asr} == set(engines.SPEECH)
    assert all(r["by"] in ("sentence", "pause") for r in asr)
    assert [r["name"] for r in rows if r.get("chosen")] == ["parakeet-v3", "titanet", "none"]


def test_someone_first_heard_too_briefly_to_fingerprint_gets_the_first_real_fingerprint():
    from canopy_listen.live import Speakers

    sp = Speakers()
    assert sp.assign(None, 0.0) == 0
    assert sp.assign(np.ones(192), 3.0) == 0
    assert sp.assign(-np.ones(192), 3.0) == 1
