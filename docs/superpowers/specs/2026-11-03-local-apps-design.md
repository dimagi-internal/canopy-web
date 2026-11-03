# Local apps: small pluggable programs that run on a person's Mac

**Status:** proposed 2026-11-03. Not signed off, and nothing here is built. The first app exists outside canopy as `bin/hal-meeting-feed` in the hal repo.

## Intent

Jonathan: "lets have the idea of 'expiremental' feature area ... there will need to be generic framework on how we get the transcript (we might even end up creating it in the end). This should be built in such a way that its a sort of like an app within canopy designed to run locally, similiar to how we built apps on the website, I may invent multiple ideas of these or others might want to as well."

And on how it reaches people: "the install should come from the canopy system, not github directly, and there would be a publishing process we could implement longer term ... for a normal user, they would click install meeting assistant, config it, and then be able to use it, utilize the canopy ecosystem for doing agentic stuff after tey have install the app."

The first idea is live feedback in a meeting: read a running Superwhisper transcript, send batches to an agent's session, read its replies during the call.

## What a person does

1. **Install.** Open **Experimental** in Canopy, click **Install** on Meeting assistant. No terminal, no GitHub account.
2. **Configure.** A form from the app's manifest: which agent, what kind of feedback, plus checks on what it needs ("Superwhisper is installed", "its command-line tool is set up"), each saying how to fix it.
3. **Use.** Start it from the menu bar during a meeting and read the replies in a normal Canopy session.

The app does the local part and Canopy does the agentic part. Agents, sessions, turns, approvals and routing are the ones the person already has, so an app contains no model calls and no agent logic of its own.

## What a local app is

A manifest plus a program, run as its own process on the person's Mac. It's off until the person installs and starts it, and it's listed under **Experimental**, meaning it can change or disappear without notice.

| | Workspace app (`docs/architecture/workspace-apps.md`) | Local app |
|---|---|---|
| What it is | A manifest: data, never server code | A manifest plus **real code** |
| Comes from | Canopy's app store | **Canopy**, the same idea |
| Installed for | A workspace, by an admin | One person, on one machine |
| Versions | Immutable template versions, plus an overlay | Immutable versions, no overlay |
| Runs | Inside canopy-web, sandboxed pages | As the person, with what they can read |
| On/off | Yes | Yes, and off by default |

The ideas carry over, but the "data only" guarantee can't. An app that reads a local CLI or a microphone has to be code, so what protects people is who gets to publish (below).

## Goals

1. A normal user installs, configures and uses an app without a terminal or GitHub.
2. New ideas don't need changes to the runner.
3. A meeting app doesn't know or care where its transcript comes from.
4. A person can always see what's running, and can stop all of it in one click.
5. No app runs inside the daemon, and none starts by itself.

## Non-goals

- Sandboxing app code. macOS gives us no practical way to do this for arbitrary programs here.
- Self-serve publishing, ratings or third-party review, for now. See **Publishing**.
- Windows and cloud runners. The menu bar and the first source are macOS-only.
- Running apps unattended, on a schedule or from a remote request.
- Customizing an app through overlays.

## Host

A new `canopy-runner app` command family, shipped in the runner package. The menu bar is how a person uses it; the commands are what the menu bar calls:

```
canopy-runner app list [--json]
canopy-runner app install <key>
canopy-runner app dev-install <folder>      # for someone building an app; marked as unpublished
canopy-runner app start <key> [--setting k=v]
canopy-runner app status [<key>] [--json]
canopy-runner app stop <key> | --all
canopy-runner app remove <key>
```

- **Separate from the daemon.** `app start` launches a small supervisor process, which launches the app. The daemon never imports or spawns app code, because that machine holds fleet credentials (`hook_listener.py` says so where it binds its port). This limits accidents and crashes. It isn't a security boundary, since an app runs as the same user and could read `~/.canopy/runner.json`.
- **State** lives in `~/.canopy/apps/<key>/`: `settings.json`, `run.json` (pid, start time, last event, stop reason) and `log.jsonl`.
- **The environment is scrubbed.** Apps get a minimal environment, so no token is handed over by accident.
- **The host enforces** the manifest's `max_minutes`, kills an app that ignores `stop` after 5 seconds, and records every stop reason.

### Menu bar

An **Experimental** submenu in `runner/menubar`, listing what's available with **Install**, each installed app's status plus Configure, Start and Stop, and **Stop all**. It calls `canopy-runner app …` through its existing `cli.run`, as **Close sessions** already calls `canopy-runner close-sessions --list --json` (`main.swift`). A running app tints or badges the status item, because an app that's listening should be obvious at a glance.

## Manifest

```yaml
key: meeting-assistant
name: Meeting assistant
version: 0.1.0
entry: ["{python}", "app.py"]   # {python} = the runner's own interpreter; v1 installs no dependencies
consumes: [transcript]          # or provides: [transcript] for a source
max_minutes: 120
notice: "What's said in this meeting leaves your Mac. Tell the people in it."
disclosure:                     # shown at install and start; NOT enforced
  reads: ["the transcript of the meeting you pick"]
  sends: ["transcript text to the canopy session you pick, and so to the agent's model provider"]
settings:                       # draws the Configure form
  agent: {type: agent, label: "Which agent gives feedback", required: true}
  brief: {type: text, label: "What kind of feedback do you want?"}
checks:                         # shown in Configure, each with how to fix it
  - {label: "Superwhisper's command-line tool is set up", run: ["superwhisper", "doctor"]}
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

## Delivery and trust

**Apps come from Canopy, never directly from GitHub** (Jonathan's decision, above). This differs on purpose from the plugin catalog (`docs/architecture/plugin-marketplace.md`), where canopy holds no plugin bytes and each person needs GitHub access to the repo. A normal user has neither a GitHub login nor a reason to get one.

**Canopy is therefore vouching for the code.** An app runs as the person and can read whatever they can read, and we can't sandbox it. So the manifest's `disclosure` is what the author says it does, not what the host can enforce, and `canopy` in the manifest limits only what the host will call for it. What protects people is that only reviewed code gets published. The person's install is consent, not review.

### Two stages

| | Now: bundled | Longer term: published |
|---|---|---|
| Where the code lives | `runner/apps/<key>/` in this repo, inside the runner release | Immutable versions stored by canopy-web |
| What "Install" does | Turns on a copy already on the Mac; no download | Downloads that version |
| How it's checked | The runner release's own signing and notarization (`runner/packaging`) | The same checks as `self_update.py`: only the server's answer, sha256, and a signature from the same publisher |
| Who can publish | Whoever can merge to this repo | The publishing process below |
| How an app updates | With the runner | Explicitly, showing what changed, because new code would otherwise change silently on someone's machine |

Bundling comes first because it needs no new server model and no new signing, and it matches how workspace apps ship canopy's own (`apps/workspace_apps/bundled/<key>/`). The manifest and protocol are the same in both stages, so moving an app to published needs no change to the app.

The catch is that an app update means a runner release, and a new idea means a PR here. That's acceptable while the apps are ours.

### Publishing (longer term, not designed here)

Submit a version, review it, sign it, list it. It needs to answer who may submit, who reviews, how a version is revoked from Macs that already have it, and how a disclosure gets checked against the code.

### Building an app

`canopy-runner app dev-install <folder>` runs a folder from your own disk, shown as **unpublished**. It's the way to try an idea before proposing it, and it's for the person who wrote the code, not a way to pass an app around.

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

## First app: Meeting assistant

Moves from `hal/bin/hal-meeting-feed` with its behaviour unchanged, including its tests. It generalizes what's hard-coded there: the agent (`hal`) becomes a setting, and the owner email and workspace (`connect`) follow from who is running it and the agent they pick. Its Superwhisper reading splits out into the source.

## Slices

1. **Host, manifest and Meeting assistant, bundled.** `canopy-runner app`, the protocol, the Superwhisper source, the app moved over. Usable from a terminal, for us. Ships after a live test (below) passes against the hal script.
2. **Menu bar.** Install, the Configure form, Start and Stop, and the running indicator. This is the slice that makes it usable by a normal user.
3. **Website.** An Experimental page listing apps and showing what's running on your Macs, from the runner's report. It can't start anything on a Mac. It needs a descriptor in `frontend/src/guide/surfaces.ts`, like any page.
4. **Publishing.** Stored versions, downloads and the review process, when someone outside this repo wants to ship an app.

## Open questions

- **The live test hasn't happened.** The hal script's send path is tested against canopy with a made-up transcript, but not against a live meeting. Still unknown: whether real segment text joins with correct spacing, whether the script notices a meeting ending (it checks for `summary` or `transcript`, unverified), and whether other people's audio on a call is captured.
- **Local voice models.** The timings above are for the cloud voice. Superwhisper's docs say Cohere Transcribe doesn't support realtime, though S1-Voice isn't listed as realtime either and worked live. Parakeet is the fallback.
- **Which identity sends?** This now matters more, because using the Canopy ecosystem should mean using it as the person: their agents, their sessions, their access. The hal script uses Hal's token, so the session contains only Hal until the owner is added. To confirm: whether the credential in `~/.canopy/runner.json` can create a session with a chosen agent as that person, and whether it should be used for this at all. Slice 2 can't be finished without an answer.
- **Who can see an app?** Everyone with a runner, or only people an admin turns Experimental on for. This should be a real switch given that these apps can record conversations.
- **Runners that aren't personal.** Installing and configuring assume one person at their own Mac. A shared or cloud runner shouldn't offer these apps at all.
- **Turn cost.** Every batch starts a full agent turn, so up to about 80 an hour during active talk, most of them answering "-". This is the likeliest reason for the policy plug point to become real.
- **Dependencies.** v1 apps get the runner's interpreter and the standard library. Anything more needs an install story.
- **Sessions per meeting.** One new session each time is simple, but it adds up in a person's session list.
