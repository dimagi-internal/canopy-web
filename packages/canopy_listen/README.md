# canopy-listen

Records a meeting on this Mac and transcribes it here, live, with open models you choose.
No audio leaves the Mac: what a local app (Meeting assistant) does with the words is up to it.

`meetings` and `meetings read ID --transcript --json` print what Superwhisper's
command-line tool prints, so Meeting assistant reads either.

Apple Silicon only. Install it as a tool, with its Mac extras:

```bash
uv tool install --python 3.12 "canopy-listen[mac] @ git+https://github.com/dimagi-internal/canopy-web#subdirectory=packages/canopy_listen"
canopy-listen models download     # the configured models, once
```

## Use

```bash
canopy-listen start [--title T]        # records from the default microphone
canopy-listen status
canopy-listen stop                     # ends the meeting: the last words are committed
canopy-listen meetings [--json]
canopy-listen meetings read ID --transcript --json
canopy-listen delete ID                # the recording and its transcript
```

Meetings live in `~/.canopy-listen/meetings/<id>/` (`meeting.json`, `audio.wav`), and each
records which models made it. The log there has states and counts only, never a word. The
first `start` from the menu bar asks macOS for the microphone on behalf of Canopy Runner.

## Choosing models

```bash
canopy-listen models                         # every model, which is chosen, which are downloaded
canopy-listen config asr qwen3-asr-1.7b      # speech
canopy-listen config speakers sortformer     # who spoke, live: titanet | sortformer | none
canopy-listen config final pyannote          # who spoke, redone when the meeting ends: none | sortformer | pyannote
canopy-listen config same_voice 0.7          # titanet: how alike two clips must be to be one person
```

Speech models are listed in `canopy_listen/engines.py::SPEECH`; adding one is one entry.
A model that times its sentences (Parakeet, Whisper) is used **by sentence**: text is final
1-3 s after it is spoken. One that returns only text (Qwen3-ASR, Granite, Cohere) is used
**by pause**: each stretch of speech is read once, when its speaker pauses.

**Who spoke** isn't part of any speech model. `titanet` takes a voice fingerprint of each
finished segment (NVIDIA TitaNet-small) and matches it to the voices heard so far; a
segment under 1.5 s never creates a new person. `sortformer` runs NVIDIA's streaming
diarization model (up to 4 speakers, overlap-aware) beside the speech model. Speakers are
numbered, not named, and not recognised across meetings.

The `final` pass relabels canopy-listen's own copy of the meeting. Meeting assistant saves
the labels it was sent live.

### Cohere Transcribe

Its official weights are gated (the MLX community copies are broken). Accept the terms at
https://huggingface.co/CohereLabs/cohere-transcribe-03-2026, make a Read token at
https://huggingface.co/settings/tokens, then run
`~/.local/share/uv/tools/canopy-listen/bin/hf auth login`. Without that login it is refused
before any download.

## Comparing models

```bash
canopy-listen bench-data ~/canopy-listen-bench        # once: published recordings with known answers
canopy-listen bench ~/canopy-listen-bench --asr all --speakers all --final all
```

Each recording is replayed through the live engine in 2-second steps, so the numbers are
what a meeting gets: word error rate, lag (how long after a sentence ends it is final),
speed (compute seconds per audio second), and the share of speech given to the right
speaker. Results are saved under `~/.canopy-listen/bench/`. Any folder of `NAME.wav` with
`NAME.txt` (the words) and/or `NAME.turns.json` (`[[start, end, speaker], ...]`) works,
for instance a recording of your own meeting.

## Testing without a microphone

```bash
canopy-listen start --file meeting.wav [--speed 4]     # plays a file in as if live
canopy-listen queue-file meeting.wav [--speed 4]       # the NEXT start (e.g. from an app) plays it
```

`tests/test_local_apps_end_to_end.py::test_end_meeting_ends_a_recording_made_on_this_mac_and_keeps_it`
runs the real recorder through the whole app path when `CANOPY_LISTEN_REAL_BIN` and
`CANOPY_LISTEN_AUDIO` are set. Unit tests: `uv run --with pytest pytest -q` (numpy only).

## Not yet

- **Other people on a call with headphones.** Only the microphone is recorded. System
  audio (a Core Audio tap) is next.
- Naming speakers, and knowing the person who owns the Mac across meetings.
