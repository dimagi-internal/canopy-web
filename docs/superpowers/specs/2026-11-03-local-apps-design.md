# Local apps: small pluggable programs that run on a person's Mac

**Status:** proposed 2026-11-03. Not signed off, and nothing here is built. The first app exists outside canopy as `bin/hal-meeting-feed` in the hal repo.

## Intent

Jonathan: "lets have the idea of 'expiremental' feature area ... there will need to be generic framework on how we get the transcript (we might even end up creating it in the end). This should be built in such a way that its a sort of like an app within canopy designed to run locally, similiar to how we built apps on the website, I may invent multiple ideas of these or others might want to as well."

The first idea is live feedback in a meeting: read a running Superwhisper transcript, send batches to an agent's session, read its replies during the call.

## What a local app is

A folder with a manifest and an executable, run as its own process on the person's Mac. It's off until the person installs and starts it, and it's listed under **Experimental**, meaning it can change or disappear without notice.

| | Workspace app (`docs/architecture/workspace-apps.md`) | Local app |
|---|---|---|
| What it is | A manifest: data, never server code | A manifest plus **real code** |
| Installed for | A workspace, by an admin | One person, on one machine |
| Versions | Immutable template versions, plus an overlay | A pinned git commit, no overlay |
| Runs | Inside canopy-web, sandboxed pages | As the person, with what they can read |
| On/off | Yes | Yes, and off by default |

The ideas carry over, but the "data only" guarantee can't. An app that reads a local CLI or a microphone has to be code, so a local app gets a different trust model (below).

## Goals

1. Anyone can add an idea without changing the runner.
2. A meeting app doesn't know or care where its transcript comes from.
3. A person can always see what's running, and can stop all of it in one click.
4. No app runs inside the daemon, and none starts by itself.

## Non-goals

- Sandboxing app code. macOS gives us no practical way to do this for arbitrary programs here.
- A store, ratings, or review of third-party code.
- Windows and cloud runners. The menu bar and the first source are macOS-only.
- Running apps unattended, on a schedule or from a remote request.
- Customizing an app through overlays.

## Host

A new `canopy-runner app` command family, shipped in the runner package:

```
canopy-runner app list [--json]
canopy-runner app install <github-repo>@<ref>
canopy-runner app start <key> [--setting k=v]
canopy-runner app status [<key>] [--json]
canopy-runner app stop <key> | --all
canopy-runner app remove <key>
```

- **Separate from the daemon.** `app start` launches a small supervisor process, which launches the app. The daemon never imports or spawns app code, because that machine holds fleet credentials (`hook_listener.py` says so where it binds its port). This limits accidents and crashes. It isn't a security boundary, since an app runs as the same user and could read `~/.canopy/runner.json`.
- **State** lives in `~/.canopy/apps/<key>/`: the code at `<sha>/`, `settings.json`, `run.json` (pid, start time, last event, stop reason) and `log.jsonl`.
- **The environment is scrubbed.** Apps get a minimal environment, so no token is handed over by accident.
- **The host enforces** the manifest's `max_minutes`, kills an app that ignores `stop` after 5 seconds, and records every stop reason.

### Menu bar

An **Experimental** submenu in `runner/menubar`, with each installed app's status plus Start and Stop, and **Stop all**. It calls `canopy-runner app …` through its existing `cli.run`, as **Close sessions** already calls `canopy-runner close-sessions --list --json` (`main.swift`). A running app tints or badges the status item, because an app that's listening should be obvious at a glance.

## Manifest

```yaml
key: meeting-feed
name: Meeting feedback
version: 0.1.0
entry: ["{python}", "app.py"]   # {python} = the runner's own interpreter; v1 installs no dependencies
consumes: [transcript]          # or provides: [transcript] for a source
max_minutes: 120
notice: "What's said in this meeting leaves your Mac. Tell the people in it."
disclosure:                     # shown at install and start; NOT enforced
  reads: ["the transcript of the meeting you pick"]
  sends: ["transcript text to the canopy session you pick, and so to the agent's model provider"]
settings:
  agent: {type: string, required: true}
  workspace: {type: string, required: true}
  brief: {type: string}
canopy: [sessions.create, sessions.send, turns.status]   # what the host will call for it
```

## Protocol

JSON lines over stdin and stdout, so an app can be written in any language. Stderr goes to the log, truncated.

| Direction | Line |
|---|---|
| host → app | `{"type":"start","settings":{...}}`, `{"type":"stop","reason":"..."}` |
| host → app | Stream items from a source: `{"type":"segment",...}`, `{"type":"ended"}`, `{"type":"gone"}` |
| host → app | `{"type":"canopy.result","id":"1","status":200,"body":{...}}` |
| app → host | `{"type":"status","state":"listening","counts":{"sent":4,"words":812}}` |
| app → host | `{"type":"event","name":"sent","fields":{"words":120}}` |
| app → host | `{"type":"canopy.call","id":"1","op":"sessions.send","args":{...}}` |
| app → host | `{"type":"stopped","reason":"meeting_ended"}` |

**Log rule.** `sensitive: true` is the default for anything consuming a transcript. The host never writes stream items to its log, and event and status fields are numbers or short identifiers only: a value that's a long string is replaced with its length. Stderr from a sensitive app is dropped. Stdout logs end up in agent transcripts, which is why this is enforced rather than asked for.

**Canopy calls go through the host.** Apps ask the host to make the few declared calls rather than holding a token. It keeps credentials out of app code and gives one place to log counts of what left the machine.

## Plug points

| Plug point | v1 | Contract |
|---|---|---|
| **Apps** | Real | Manifest and protocol above |
| **Transcript sources** | Real, with one source | A process that `provides: [transcript]`. The host starts it and relays its stream to the app |
| **"When to think" policy** | Reserved | Stays inside the meeting app: fixed batches. Becomes a plug point only once a second policy exists |
| **Sources other than transcript** | Reserved | `provides`/`consumes` are plain strings, so nothing else is needed yet |

### The transcript stream

```json
{"type":"segment","start":12.4,"end":12.9,"speaker":"Speaker 1","text":"..."}
{"type":"ended"}
{"type":"gone"}
```

- `speaker` is the source's current label, so a rename reaches only later segments.
- `ended` means the meeting finished normally; `gone` means it was deleted or became unreadable. An app must treat `gone` as "drop anything unsent".
- A source emits each segment once, in order. Sources own their own cursor and their own quirks.

**First source: Superwhisper CLI.** It polls `superwhisper meetings read <id> --transcript --json`. There is no streaming API or webhook, and live text is in `segments` while `transcript` stays empty. Tested 2026-11-03: text was readable 8–14 seconds after it was spoken, using Superwhisper's cloud voice.

**Later: our own transcriber** (a macOS audio tap plus a local model) would be another source with the same stream. No app changes.

## Trust

An app runs as the person and can read whatever they can read. So the manifest's `disclosure` is what the author says it does, not what the host can enforce. `canopy` in the manifest limits only what the host will call for it, since the app could reach a token on disk.

Installing is a deliberate act by one person, who is shown the disclosure and the commit. Code is fetched with **their own git credentials**, as the plugin catalog does (`docs/architecture/plugin-marketplace.md`: canopy never lends GitHub access and holds no plugin bytes). Updates are explicit, since pulling new code silently would change what's running on someone's machine.

**Open decision for Jonathan:** who can install what?
- **A: any repo the person names** (recommended for Experimental). This is simplest and fits "others might want to", and the risk is the same as running any script from GitHub.
- **B: only an allowlist kept in canopy-web.** Safer for less technical colleagues, but each new idea needs an admin first, and it adds a server model.

Recommendation: A now, with B added before this leaves Experimental or reaches people outside the team.

## Safety rules from the meeting feed

| Rule | Owner |
|---|---|
| Stop-all, time cap, kill on ignored stop, a visible "running" state | Host |
| Scrubbed environment; log rule | Host |
| Show `notice` and disclosure on start | Host |
| Attach to one meeting only, and refuse a stale one or one that has ended | Source |
| Report `ended` and `gone` | Source |
| Stop after silence; default to only what's said after the start | App |
| Drop unsent text on `gone` | App |
| Fence transcript in a per-run random tag, and tell the session it's data | App |
| Retry a failed batch byte-identical under the same `client_id` | App |
| Send nothing new while the last turn is unfinished | App |

The app-owned rules stay in the app because they're about this idea, not all ideas. Fencing can move to a shared helper when a second app needs it.

## First app: meeting feed

Moves from `hal/bin/hal-meeting-feed` with its behaviour unchanged, including its tests. It generalizes what's hard-coded there: the agent (`hal`), the owner email added as a participant, and the workspace (`connect`) become settings or follow from who is running it. Its Superwhisper reading splits out into the source.

## Slices

1. **Host, manifest and meeting feed.** `canopy-runner app`, the protocol, the Superwhisper source, the app moved over. Usable from a terminal. Ships after a live test (below) passes against the hal script.
2. **Menu bar.** The Experimental submenu and the running indicator.
3. **Website.** An Experimental page, from the runner's report of what's installed and running. It only shows and links; it can't start anything on a Mac. It needs a descriptor in `frontend/src/guide/surfaces.ts`, like any page.

## Open questions

- **The live test hasn't happened.** The hal script's send path is tested against canopy with a made-up transcript, but not against a live meeting. Still unknown: whether real segment text joins with correct spacing, whether the script notices a meeting ending (it checks for `summary` or `transcript`, unverified), and whether other people's audio on a call is captured.
- **Local voice models.** The timings above are for the cloud voice. Superwhisper's docs say Cohere Transcribe doesn't support realtime, though S1-Voice isn't listed as realtime either and worked live. Parakeet is the fallback.
- **Which identity sends?** The hal script uses Hal's token, so the session contains only Hal until the owner is added. For a local app the person should probably own the session. To confirm: whether the credential in `~/.canopy/runner.json` can create a session with a chosen agent as that person, and whether it should be used for this at all.
- **Turn cost.** Every batch starts a full agent turn, so up to about 80 an hour during active talk, most of them answering "-". This is the likeliest reason for the policy plug point to become real.
- **Dependencies.** v1 apps get the runner's interpreter and the standard library. Anything more needs an install story.
- **Sessions per meeting.** One new session each time is simple, but it adds up in a person's session list.
