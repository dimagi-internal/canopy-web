# canopy-listen

Records a meeting on this Mac and transcribes it here, live, with open models. Nothing
leaves the Mac: what a local app (Meeting assistant) does with the words is up to it.

- **Words:** NVIDIA Parakeet TDT 0.6B v3 through MLX, on the Mac's GPU. On an M5 Pro, 20 s
  of speech takes about 0.3 s. Text is final 2-6 s after it is spoken.
- **Who spoke:** NVIDIA TitaNet-small voice embeddings (ONNX), clustered as people talk.
  A segment shorter than 1.5 s never creates a new speaker.
- **Shape:** `meetings` and `meetings read ID --transcript --json` print what
  Superwhisper's command-line tool prints, so Meeting assistant reads either.

Apple Silicon only. Install it as a tool, with its Mac extras:

```bash
uv tool install --python 3.12 "canopy-listen[mac] @ git+https://github.com/dimagi-internal/canopy-web#subdirectory=packages/canopy_listen"
canopy-listen models     # downloads both models once (about 1.3 GB)
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

Meetings live in `~/.canopy-listen/meetings/<id>/` (`meeting.json`, `audio.wav`). The log
there has states and counts only, never a word.

The first `start` from the menu bar asks macOS for the microphone on behalf of Canopy
Runner.

## Testing without a microphone

```bash
canopy-listen start --file meeting.wav [--speed 4]     # plays a file in as if live
canopy-listen queue-file meeting.wav [--speed 4]       # the NEXT start (e.g. from an app) plays it
```

`tests/test_local_apps_end_to_end.py::test_end_meeting_ends_a_recording_made_on_this_mac_and_keeps_it`
runs the real recorder through the whole app path when `CANOPY_LISTEN_REAL_BIN` and
`CANOPY_LISTEN_AUDIO` are set. Unit tests: `uv run --with pytest pytest -q` (numpy only).

## Not yet

- **Other people on a call with headphones.** Only the microphone is recorded, so their
  voices are heard only if they come out of the speakers. System audio (a Core Audio tap)
  is next.
- Speaker names: speakers are numbered, and naming them is up to the reader.
