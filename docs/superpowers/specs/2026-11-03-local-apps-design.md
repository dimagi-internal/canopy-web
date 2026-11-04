# Local apps: small pluggable programs that run on a person's Mac

**Status:** being built, 2026-11-03. Jonathan chose to build the full publish, approve and release path now: "lets just build out the full longer term mode, so that the iteration can be a lot faster once we build it out stably." The first app also exists outside canopy as `bin/hal-meeting-feed` in the hal repo, until the live test passes.

## Intent

Jonathan: "lets have the idea of 'expiremental' feature area ... there will need to be generic framework on how we get the transcript (we might even end up creating it in the end). This should be built in such a way that its a sort of like an app within canopy designed to run locally, similiar to how we built apps on the website, I may invent multiple ideas of these or others might want to as well."

And on how it reaches people: "the install should come from the canopy system, not github directly, and there would be a publishing process we could implement longer term ... for a normal user, they would click install meeting assistant, config it, and then be able to use it, utilize the canopy ecosystem for doing agentic stuff after tey have install the app."

The first idea is live feedback in a meeting: read a running Superwhisper transcript, send batches to an agent's session, read its replies during the call.

## What a person does

1. **Install.** In the workspace's app store, under **Runs on your computer**, click **Install** on Meeting assistant, read what it reads and sends, and pick the computer. No terminal, no GitHub account.
2. **Configure.** A form from the app's manifest: which agent, what kind of feedback.
3. **Use.** Start it from the menu bar during a meeting. It first runs the app's checks ("Superwhisper's command-line tool is set up"), each saying how to fix it, shows the notice, then reads replies in a normal Canopy session.

The app does the local part and Canopy does the agentic part. Agents, sessions, turns, approvals and routing are the ones the person already has, so an app contains no model calls and no agent logic of its own.

## What a local app is

A manifest plus a program, run as its own process on the person's computer. It's off until the person installs and starts it, and it's marked **Experimental**, meaning it can change or disappear without notice.

## What is shared with workspace apps

One store and one approvals system. The code runs in a different place, so installs are separate.

| | Workspace app | Local app | Shared? |
|---|---|---|---|
| Catalog | `AppTemplate` + immutable `AppTemplateVersion` | The same rows, `runtime="local"` | **Yes** |
| Version ordering, listing, changelog | `latest_version`, the store page | The same | **Yes** |
| "What changed" in plain words | `template_diff.changes` | `local_apps.trust.changes`, same shape, its own rules | Pattern |
| Publish approval | A workspace admin, through `Publish` | `apps/approvals` kind `local_app.release` | Approvals is shared |
| Install | `AppInstallation`: one workspace, composed overlay | `LocalAppInstallation`: one person, one computer, no overlay | No |
| Runs | In canopy-web, sandboxed | On the computer, as the person | No |

Existing web store queries filter to `runtime="web"`, so a local app can't reach code that expects `collections`, and the store page draws the two side by side.

## Goals

1. A normal user installs, configures and uses an app without a terminal or GitHub.
2. A new app or a new version of one needs no runner release and no canopy-web deploy.
3. A meeting app doesn't know or care where its transcript comes from.
4. A person can always see what's running, and can stop all of it in one click.
5. Canopy can stop a bad version on every computer.
6. No app runs inside the daemon, and none starts by itself. A web page can install or configure an app, but only the person at the computer can start one.

## Non-goals

- Sandboxing app code. macOS gives us no practical way to do this for arbitrary programs here.
- Third-party ratings or self-serve listing without review.
- Windows and cloud runners. Only a person's own laptop runner offers apps, and the first source is macOS-only.
- Running apps unattended, on a schedule or from a remote request.
- Customizing an app through overlays.

## Publish, approve, release

```
author                 canopy-web                     reviewer                 people
pack + submit  ──────► validate, store, compute facts
                       file approval ───────────────► reads code, diff, facts
                                                      Approve ──► released ──► next Start uses it
                                                      Decline ──► nothing changes
                       revoke ◄───────────────────── (any time)  ──────────► running copies stop
```

1. **Pack.** `canopy-runner app pack <folder>` validates the manifest and writes a deterministic zip: sorted names, fixed times, `tests/` and caches left out. The same folder gives the same sha256.
2. **Submit.** `POST /api/local-apps/submissions/` with the package base64-encoded, so it also works as an MCP tool. `canopy-runner app submit <folder>` and any agent with a token do the same. It needs `content.write` in a workspace. Submitting the same version and sha again returns the same submission; the same version with different bytes is refused, because versions are immutable.
3. **Validate.** The server never runs the package. It checks size (5 MB packed, 20 MB unpacked, 200 files), paths (no traversal, no symlinks, no binaries), the manifest, that `version` is newer than the latest release, and computes **facts** for the reviewer: every file and size, modules imported that reach the network or start processes, and what changed in the trust surface.
4. **Approve.** Submitting files an `ApprovalRequest` of kind `local_app.release`, so it arrives by push, Slack and email like every other approval, with an audit trail. Its snapshot names the exact sha256, so what's approved is exactly those bytes. A review page shows the code, the diff from the last release and the facts. Approving needs `is_superuser` for now, because the catalog is Canopy's and Canopy is vouching for it, and nobody approves their own submission.
5. **Release.** Approving creates the immutable `AppTemplateVersion` and lists the app. A released app belongs to Canopy's catalog (`owner=None`) with the submitting workspace recorded, so it's visible to every workspace that has switched local apps on, and it holds no tenant data.
6. **Reach a computer.** At each Start, the runner asks the server what to run. See **Updates**.
7. **Revoke.** A superuser revokes a version with a reason. Start refuses it, and a run's next heartbeat, at most 30 seconds later, is told to stop. Canopy calls for that run are refused straight away.

**Why no second signature.** A runner release is Apple-signed so a compromised server can't ship new runner code. A local app gets only TLS, the server's sha256 and the approval record. Compromising canopy-web already lets someone start agent turns on these computers, so a second key wouldn't raise the bar today. If turns are ever confined so that stops being true, the release gains a signature checked against a key inside the signed runner.

**Canopy is vouching for the code.** An app runs as the person and can read whatever they can read. So `disclosure` is what the author says, the facts help a reviewer check it, and `canopy` limits only what the server does for the app. Clicking Install is consent, not review.

### Updates

The runner uses the latest released version at each Start, never during a run, as long as the **trust surface** is unchanged: `notice`, `disclosure`, `canopy`, `consumes`, `provides`, `entry` and `platforms`. Canopy has reviewed the code, and asking a normal user to click Update on code they can't judge adds friction without adding safety.

If the trust surface changed, the person is shown the differences and has to accept before the new version runs. Until then they stay on the version they accepted, unless it's revoked, when Start refuses and says why. This is also why iteration is fast: an approved fix reaches everyone at their next Start.

### Building an app

`canopy-runner app dev-install <folder>` runs a folder from your own disk, shown as **unpublished**, with no server involved. Its Canopy calls go to a local stub that answers them and writes what would have been sent to a private file, so nothing leaves the computer. It's for the person who wrote the code, not a way to pass an app around.

## Who may use local apps

- **A workspace switch, off by default.** `Workspace.settings.local_apps`, set by anyone with `integrations`, because these apps can record conversations. Off means the store hides them, and installing and starting are refused.
- **A person's own laptop runner only.** `Runner.kind == emdash` and `Runner.owner` is the person. Cloud and computer-only runners never offer apps. `private` isn't the test, because most laptops also serve fleet work.
- **Acting as the person.** The app never holds a credential. Canopy calls go through the runner's own token to `/api/harness/runners/{runner}/local-apps/…`, and each is allowed only by an installation row the person created while signed in. The agent comes from that row, and `sessions.send` works only on a session that run opened, so the box token still can't act in the owner's other chats. Whether the person may use the agent is asked of `access.decide` when configuring and again at each Start.

## Host

A `canopy-runner app` command family in the runner package. The menu bar is how a person uses it.

```
canopy-runner app list [--json]
canopy-runner app check <key> [--json]
canopy-runner app start <key> [--accept-notice]
canopy-runner app status [<key>] [--json]
canopy-runner app stop <key> | --all
canopy-runner app pack <folder> [--out <zip>]
canopy-runner app submit <folder> [--note <text>]
canopy-runner app dev-install <folder>
canopy-runner app remove <key>
```

- **Separate from the daemon.** `app start` launches a detached supervisor process, which launches the app. The daemon never imports or spawns app code. This limits accidents and crashes. It isn't a security boundary, since an app runs as the same user.
- **State** lives in `~/.canopy/apps/<key>/`: `versions/<version>/` (unpacked, read-only), `run.json` (pid, run id, start time, last event, stop reason) and `log.jsonl`.
- **The download is checked**: only the server's answer, its sha256 and size, and the same path limits as the server, unpacked beside the target and renamed in.
- **The environment is scrubbed.** Apps get `PATH`, `HOME`, `LANG` and `TMPDIR`, so no token is handed over by accident.
- **The host enforces** the manifest's `max_minutes`, kills an app that ignores `stop` after 5 seconds, heartbeats every 15 seconds, stops the app if the server says to or can't be reached for 5 minutes, and records every stop reason.

### Menu bar

An **Experimental** submenu in `runner/menubar`: each installed app's status, Start and Stop, **Stop all**, and **Browse apps…**, which opens the store. It calls `canopy-runner app …` through its existing `cli.run`, as **Close sessions** calls `close-sessions --list --json`. Start shows failed checks, or the `notice` in an `NSAlert`, then starts. A running app badges the status item, because an app that's listening should be obvious at a glance.

### Website

The store lists local apps beside web apps, with a detail page showing the disclosure, the versions and Install. Installed apps show Configure, Remove, what's running from the run's heartbeats, and a Stop button. Stop is a durable request read at the next heartbeat. A web page can stop an app but never start one.

## Manifest

`manifest.json`, like a bundled workspace app, validated by one module on the server and a copy in the runner, kept equal by a test.

```json
{
  "key": "meeting-assistant",
  "name": "Meeting assistant",
  "description": "Live feedback from an agent during a meeting.",
  "version": "0.1.0",
  "changelog": "First release.",
  "entry": ["{python}", "app.py"],
  "platforms": ["macos"],
  "consumes": ["transcript"],
  "max_minutes": 120,
  "notice": "What's said in this meeting leaves your Mac. Tell the people in it.",
  "disclosure": {
    "reads": ["the transcript of the meeting you pick"],
    "sends": ["transcript text to the canopy session it opens, and so to the agent's model provider"]
  },
  "settings": {
    "agent": {"type": "agent", "label": "Which agent gives feedback", "required": true},
    "brief": {"type": "longtext", "label": "What kind of feedback do you want?"}
  },
  "checks": [],
  "canopy": ["sessions.create", "sessions.send", "turns.status"]
}
```

`{python}` is the runner's own interpreter, and v1 installs no dependencies. `checks` run locally at Start: `{"label", "run": [argv], "fix"}`, up to 10 seconds each.

## Protocol

JSON lines over stdin and stdout, so an app can be written in any language. Stderr goes to the log, truncated.

| Direction | Line |
|---|---|
| host → app | `{"type":"start","settings":{...},"run_id":"..."}`, `{"type":"stop","reason":"..."}` |
| host → app | Stream items from a source: `{"type":"segment",...}`, `{"type":"ended"}`, `{"type":"gone"}` |
| host → app | `{"type":"canopy.result","id":"1","status":200,"body":{...}}` |
| app → host | `{"type":"status","state":"listening","counts":{"sent":4,"words":812}}` |
| app → host | `{"type":"event","name":"sent","fields":{"words":120}}` |
| app → host | `{"type":"canopy.call","id":"1","op":"sessions.send","args":{...}}` |
| app → host | `{"type":"stopped","reason":"meeting_ended"}` |

**Log rule.** `sensitive` is on for anything consuming a transcript. The host never writes stream items to its log, and event and status fields are numbers or short identifiers only: a value that's a long string is replaced with its length. Stderr from a sensitive app is dropped. Heartbeats to the server carry `state` and numeric `counts` only. Stdout logs end up in agent transcripts, which is why this is enforced rather than asked for.

### Server calls for a run

All under `/api/harness/runners/{runner}/local-apps/`, gate `runner`:

| Call | Does |
|---|---|
| `GET /` | This computer's installations: version to run, sha256, notice, settings, whether it needs acceptance, why it can't start |
| `GET /{key}/package?version=` | The package bytes, only for a version this installation may run |
| `POST /{key}/runs/` | Start: refuses if revoked, switched off, unaccepted, or the person can't use the agent. Returns `run_id` |
| `POST /runs/{id}/heartbeat` | `{state, counts}` → `{stop, reason}` |
| `POST /runs/{id}/finish` | `{reason, counts}` |
| `POST /runs/{id}/canopy` | `{op, args}` for an op the manifest declares |

`sessions.create` takes `{title}`, and the agent and workspace come from the installation, with the person as creator. `sessions.send` takes `{session_id, text, client_id}`, only for a session this run opened. `turns.status` takes `{turn_id}`, only for a turn this run started.

## Plug points

| Plug point | v1 | Contract |
|---|---|---|
| **Apps** | Real | Manifest and protocol above |
| **Transcript sources** | Real, with one source | An app that `provides: ["transcript"]`. Installing a consumer also installs the sole provider, listed in what the person accepts. The host starts it and relays its stream |
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

**First source: Superwhisper CLI** (`local_apps/superwhisper-transcript/`). It polls `superwhisper meetings read <id> --transcript --json`. There is no streaming API or webhook, and live text is in `segments` while `transcript` stays empty. Tested 2026-11-03: text was readable 8–14 seconds after it was spoken, using Superwhisper's cloud voice.

**Later: our own transcriber** (a macOS audio tap plus a local model) would be another source with the same stream. No app changes.

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

## First apps

`local_apps/meeting-assistant/` moves from `hal/bin/hal-meeting-feed` with its behaviour unchanged, including its tests. The agent, workspace and owner it hard-codes now come from the installation. Its Superwhisper reading splits out into `local_apps/superwhisper-transcript/`. `local_apps/` is source only: nothing in the runner release or the web image depends on it, and an app reaches people only by being submitted and approved.

## Build order

1. **Server** (`apps/local_apps`): runtime on the catalog, submissions, approval kind, installations, runs, Canopy calls, revoke.
2. **Runner and apps**: the host, the commands, the menu bar, both apps.
3. **Website**: the store section, detail, Install, Configure, review page, the switch.
4. **Go live** (owner's approval): deploy, switch on, submit, approve, live meeting test.

## Open questions

- **The live test hasn't happened.** Still unknown: whether real segment text joins with correct spacing, whether a meeting's end is noticed (it checks for `summary` or `transcript`, unverified), and whether other people's audio on a call is captured.
- **Local voice models.** The timings above are for the cloud voice. Superwhisper's docs say Cohere Transcribe doesn't support realtime, though S1-Voice isn't listed as realtime either and worked live. Parakeet is the fallback.
- **Transcript as an instruction channel.** Anyone in a meeting can say something aimed at the agent. Fencing tells the model it's data, and one planted line was ignored in testing, but that isn't a guarantee when the agent holds real tools. A read-only agent is the safer pick until this is tested harder.
- **Turn cost.** Every batch starts a full agent turn, so up to about 80 an hour during active talk, most of them answering "-". This is the likeliest reason for the policy plug point to become real.
- **Who else can approve.** Superusers only for now. Opening this up needs a reviewer role.
- **Dependencies.** v1 apps get the runner's interpreter and the standard library. Anything more needs an install story.
- **Sessions per meeting.** One new session each time is simple, but it adds up in a person's session list.
