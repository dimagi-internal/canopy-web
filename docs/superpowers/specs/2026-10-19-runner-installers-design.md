# Runner installers: a non-developer installs a laptop runner from a download

Goal: someone with no git, no `uv`, no Xcode command-line tools and no terminal
installs and runs a canopy laptop runner on macOS or Windows from a downloaded
installer, pairs it from their browser, and it keeps itself on the deployed
version. The `install-runner.sh` / `.ps1` developer path keeps working unchanged.

## Decisions

1. **Distribution is a canopy-web download route backed by S3, not GitHub
   Releases.** `dimagi-internal/canopy-web` is private: a release asset there is
   unreachable for anyone without org access, and a box holds no GitHub
   credential by invariant ("No box holds a GitHub token"). canopy-web already
   authenticates both audiences — a person in a browser (session) and a runner
   (its device-key `RunnerAccessToken`) — so it decides who may download and
   which build is current. Bytes live in the existing private attachments bucket
   under `runner-releases/<runner_sha>/`; canopy-web answers with a 302 to a
   short-lived presigned URL (installers are ~100 MB; streaming them through the
   ASGI app would tie up a worker per download). The attachments bucket's
   "Django streams every byte" rule exists to check session ownership per object;
   a release has no owner beyond "signed in", which canopy-web checks before it
   signs the URL.
2. **What is current is the deployed `RUNNER_CODE_SHA`**, as today. CI builds a
   release for every runner-source sha that reaches main; the server only ever
   serves the one its own image names. A sha with no built release answers 404,
   which the updater reads as "unknown — do nothing".
3. **Pairing is a loopback device flow on the keyless path.** The box makes its
   device key first, then asks canopy-web for a pairing. A person approves it in
   the browser behind a passkey step-up; canopy-web hands the approval's one-time
   secret back ONLY through a redirect to `http://127.0.0.1:<port>/` on the
   machine whose browser approved it. The box claims with that secret and a DPoP
   proof of the same key and gets a runner whose device key is already ACTIVE.
   No PAT exists at any point. The loopback leg is what makes a phished approval
   useless: an attacker who sends someone the approve link never receives the
   secret, because the redirect lands on the victim's own loopback.
4. **The installers are per-user.** macOS: a notarized DMG holding
   `Canopy Runner.app`, which moves itself to `~/Applications` and updates by
   replacing itself there (no admin, ever). Windows: an Inno Setup installer with
   `PrivilegesRequired=lowest`, installing versioned directories under
   `%LOCALAPPDATA%\Programs\Canopy Runner` behind a `current` junction, and
   registering the two Scheduled Tasks for the current user as #1588 does.
5. **Each artifact bundles its runtime**: a relocatable CPython
   (python-build-standalone) with the runner wheels and their dependencies
   installed into it, Node.js, the CDP sidecar's `node_modules`, the ACP adapter,
   and on macOS the menu-bar app and `canopy-device-key`. Nothing is fetched at
   install time.
6. **Signing is behind secrets.** macOS: Developer ID Application + notarytool +
   stapler. Windows: Azure Trusted Signing (an OV/EV certificate in a PFX is no
   longer issuable — CA/B rules since 2023 require the key in an HSM). With the
   secrets absent CI still builds and smoke-tests unsigned artifacts and the
   manifest says `signed: false`; nothing pretends.

## Contracts

### Pairing API (`apps/harness/api_runner_pairings.py`)

| Route | Auth | Body → answer |
|---|---|---|
| `POST /api/harness/runner-pairings/` | none; `DPoP` proof (htm POST, htu = this URL) by the key in the body | `{public_jwk, kind: secure-enclave\|tpm\|software, ecdh_public_jwk?, ecdh_store?, hostname, account, platform: macos\|windows\|linux, has_emdash, redirect_port}` → 201 `{id, user_code, verify_url, expires_at, interval}`; or 200 `{status: "reattached", runner_id, name, workspace, device_key_id}` when an ACTIVE key with this jkt is on a non-retired runner. Proof jkt ≠ thumbprint(public_jwk) → uniform 401. One live pairing per jkt (a new one replaces it). Expires in 15 min. |
| `GET /api/harness/runner-pairings/{user_code}` | session | `{user_code, hostname, account, platform, kind, has_emdash, status: pending\|approved\|consumed\|declined\|expired, expires_at, suggested_name, workspaces: [{slug, name}], runner_id?}` — `workspaces` = the caller's workspaces with `AGENT_WORK`. |
| `POST /api/harness/runner-pairings/{user_code}/approve` | session + passkey step-up; tokens refused | `{workspace, name}` → `{status: "approved", redirect_url: "http://127.0.0.1:<port>/paired?pairing=<id>&secret=<raw>"}`. Re-approving an approved, unconsumed pairing issues a new secret. |
| `POST /api/harness/runner-pairings/{user_code}/decline` | session | → `{status: "declined"}` |
| `POST /api/harness/runner-pairings/{id}/claim` | none; `DPoP` proof (htu = this URL) by the pairing's key | `{secret?}` → 202 `{status: "waiting"}` while pending; 201 `{status: "paired", runner_id, name, workspace, device_key_id}` with the right secret — creates `Runner(kind=emdash, engine=emdash if has_emdash else claude-cli, capabilities={"sessions": true}, owner=approver, workspace)` and its `RunnerDeviceKey` ACTIVE (ECDH approved with it); anything else (declined, expired, consumed, bad secret, bad proof, approver lost `AGENT_WORK`) a uniform 401. |

The approve page is `/pair/:code` (root: a pairing has no workspace until the
person picks one).

### Releases

S3: `s3://<CANOPY_RUNNER_RELEASES_BUCKET>/runner-releases/<sha>/` holds the
artifacts and `manifest.json` (written last):

```json
{"sha": "<40-hex runner sha>", "committed_at": 1760000000, "built_at": "2026-10-19T12:00:00Z",
 "artifacts": {
   "macos-arm64":    {"installer": {"file": "Canopy-Runner-macos-arm64.dmg", "sha256": "…", "size": 1},
                      "update":    {"file": "Canopy-Runner-macos-arm64.app.zip", "sha256": "…", "size": 1},
                      "signed": true, "notarized": true},
   "macos-x86_64":   {…},
   "windows-x86_64": {"installer": {"file": "Canopy-Runner-Setup-windows-x86_64.exe", …},
                      "update":    {"file": "Canopy-Runner-Setup-windows-x86_64.exe", …},
                      "signed": false, "notarized": false}}}
```

| Route | Auth | Answer |
|---|---|---|
| `GET /api/harness/runner-releases/current?platform=<p>` | runner token (added to `runner_auth`) or a person | `{sha, platform, file, url (presigned, 15 min), sha256, size, signed, notarized}` for the update artifact of the deployed sha; 404 none built; 503 no bucket. |
| `GET /api/harness/runner-releases/downloads` | a person | `{sha, downloads: [{platform, label, file, size, signed, notarized, href: "/download/runner/<p>"}]}` |
| `GET /download/runner/<platform>` (bare view) | signed-in browser (else login) | 302 to the presigned installer URL; a friendly 404 page when none is built. |

### `_build_info.py`

`SHA`, `BUILT_AT`, `COMMITTED_AT` as today, plus `PLATFORM` (`""` for a
developer install, else the release platform). CI stamps it exactly as
`install-runner.sh` does.

### Bundle layout

macOS (`Canopy Runner.app/Contents/`):

```
MacOS/CanopyRunner                    the menu-bar app (Swift)
Resources/runtime/release.json        {"sha", "platform", "built_at"}
Resources/runtime/python/             python-build-standalone, runner + deps in site-packages
Resources/runtime/node/bin/node, npm
Resources/runtime/acp/                npm --prefix for the ACP adapter
Resources/runtime/bin/canopy-runner   sh shim: node on PATH, exec python -m canopy_runner.main "$@"
Resources/runtime/bin/canopy-device-key
```

Windows (`%LOCALAPPDATA%\Programs\Canopy Runner\`):

```
versions\<sha12>\release.json, python\(python.exe, pythonw.exe, Lib\site-packages), node\node.exe,
                 acp\, bin\canopy-runner.cmd
current          junction → versions\<sha12>
```

The runner finds its bundle from `sys.executable` (the directory holding
`release.json` above `python/`) and, when bundled, puts its `node` on `PATH` and
its `acp` prefix first in the adapter search.

### Runner commands

- `canopy-runner setup [--no-pair] [--base-url]` — what the installer runs: pair
  by browser if there is no `runner.json`, link `~/.canopy/bin/canopy-device-key`
  (macOS), render and (re)load the jobs pointing at the bundle (launchd:
  `com.canopy.runner`, `com.canopy.runner.updater`, `com.canopy.runner.menubar`;
  Windows: `\Canopy\canopy-runner`, `\Canopy\canopy-runner-updater`), start the
  runner. Idempotent.
- `canopy-runner pair --browser` — the loopback pairing above; writes
  `runner.json` with no token and `~/.canopy/device-key/status.json` as canopy's
  CLI writes it, so a later `canopy vault …` shares the key.
- `canopy-runner self-update [--if-stale]` — the bundled updater: asks
  `runner-releases/current` with the runner's own token, compares with
  `_build_info.SHA`, defers while `in-flight` says a turn is running, downloads,
  checks the sha256 (and on macOS the code signature's team against the running
  app's), installs, restarts the runner. Same `current|stale|busy|unknown`
  vocabulary as `update-check`.
- `canopy-runner run --log-file PATH` — Windows runs `pythonw.exe` (no console
  window), so the log is written by the runner itself.

The runner signs in natively when there is no canopy CLI: it makes and uses the
device key exactly where and how canopy's `orchestrator/device_key.py` does
(`~/.canopy/device-key/`: `se.key` via the helper, the TPM key
`canopy-device-key`, or `sw.pem`), so the two never disagree about the key.
