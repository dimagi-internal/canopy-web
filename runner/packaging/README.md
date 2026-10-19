# Runner installers

The downloadable Canopy Runner: a DMG per Mac architecture and a per-user
installer for Windows. Each bundles its whole runtime (Python, the runner, Node,
the CDP sidecar's deps, the ACP adapter, and on macOS the menu-bar app and
`canopy-device-key`), so a person with no developer tools can install a runner and
pair it from the browser. Design and contracts:
`docs/superpowers/specs/2026-10-19-runner-installers-design.md`. The developer path
(`runner/canopy_runner/scripts/install-runner.sh` / `.ps1`) is separate and unchanged.

| File | What it does |
|---|---|
| `build_payload.py` | Builds the runtime directory for one platform (`macos-arm64`, `macos-x86_64`, `windows-x86_64`): stamps `_build_info.py` in a temp copy of the sources, builds the four wheels, unpacks python-build-standalone CPython and Node.js (pinned and checksummed in `versions.json`), installs `canopy-runner[realtime,keys]` into that Python, runs `npm ci` for the CDP sidecar, `npm install -g --prefix acp` for the ACP adapter, and writes `release.json` and the `bin/canopy-runner` shim. `--provenance` prints only the runner sha. |
| `versions.json` | The pinned CPython, Node.js and ACP adapter versions, with sha256 for every download. |
| `macos/build_app.sh` | Compiles the menu-bar app and `canopy-device-key` for one arch, assembles `Canopy Runner.app` with the payload in `Contents/Resources/runtime`, signs, notarizes and staples it when the secrets are set, and writes the DMG, the update zip and a build record. |
| `macos/runtime.entitlements` | The hardened-runtime exceptions given to the bundled Python, Node and native modules, with the reason for each. |
| `windows/canopy-runner.iss` | Inno Setup script: per-user (`PrivilegesRequired=lowest`), `versions\<sha12>` behind a `current` junction. It runs `canopy-runner setup` at the end unless `/UPDATE` is passed. |
| `windows/build_installer.ps1` | Compiles the `.iss` with ISCC. If Inno Setup is missing it installs it with Chocolatey. |
| `manifest.py` | `record` writes a build record for one platform. `assemble` turns the records into the release `manifest.json` and refuses one with a platform missing. |
| `smoke/smoke-macos.sh` | Copies the app out of the DMG and checks that it runs. It uses a throwaway HOME, so it is safe on a Mac with a live runner. |
| `smoke/smoke-windows.ps1` | Installs silently with `/UPDATE`, checks the junction and runtime, then uninstalls and checks that the Scheduled Tasks are removed. |

## Build locally

macOS (arm64 shown; the result is ad-hoc signed and runs only on the Mac that built it):

```bash
python3 runner/packaging/build_payload.py --platform macos-arm64 --out build/payload-macos-arm64
bash runner/packaging/macos/build_app.sh arm64 build/payload-macos-arm64 build/dist-macos-arm64
bash runner/packaging/smoke/smoke-macos.sh build/dist-macos-arm64 arm64
```

To install it, open the DMG and launch the app. **Don't do this on a Mac that
already runs a developer runner you rely on.** The app replaces
`~/Applications/Canopy Runner.app` and runs `canopy-runner setup`, which rewrites
the `com.canopy.runner*` launchd jobs.

Windows (on Windows, from PowerShell):

```powershell
python runner\packaging\build_payload.py --platform windows-x86_64 --out build\payload-windows-x86_64
.\runner\packaging\windows\build_installer.ps1 -Payload build\payload-windows-x86_64 -OutDir build\dist
python runner\packaging\manifest.py record --platform windows-x86_64 --sha <sha> `
  --installer build\dist\Canopy-Runner-Setup-windows-x86_64.exe --update build\dist\Canopy-Runner-Setup-windows-x86_64.exe `
  --out build\dist\windows-x86_64.json
.\runner\packaging\smoke\smoke-windows.ps1 -Dist build\dist
```

`build_payload.py` needs `git` and `uv`. Downloads are cached in
`~/.cache/canopy-runner-packaging`; pass `--cache` to use another directory. A
**native** build (the host can run the target) uses the bundled Python's pip and
the bundled Node's npm, and precompiles bytecode. A **cross** build (for example
Windows from a Mac) uses `uv pip install --target --python-platform` and
`npm --os/--cpu`. It skips bytecode and is only good for looking at the layout; CI
builds every platform natively.

The sha stamped into a build is the last commit that touched the runner's code
paths (`provenance.CODE_PATHS`, which includes this directory). Uncommitted
changes under those paths are built in, with a warning, but the sha still names
the commit, so release only from CI.

## What CI does (`.github/workflows/runner-release.yml`)

- **Triggers:**
  - Every push to `main` that touches the runner code paths, this directory or the workflow.
  - `workflow_dispatch` on any ref, for testing. Only `main` publishes.
  - Pull requests touching `runner/packaging/**`, `runner/menubar/**` or the workflow. These build and smoke-test with no secrets and upload nothing.
- **`resolve`:** computes the runner sha the same way `deploy-labs.yml` computes `RUNNER_CODE_SHA`.
- **`build-macos`:** runs natively on `macos-15` (arm64) and `macos-15-intel` (x86_64). **`build-windows`** runs on `windows-latest`. Each uploads its artifacts and a build record.
- **`smoke-macos` / `smoke-windows`:** run on fresh runners against the uploaded artifacts.
- **`publish`:** runs on `main` only, after every build and smoke has passed:
  - Assumes `secrets.AWS_ROLE_ARN`.
  - Uploads to `s3://labs-jj-canopy-web-attachments-858923557655/runner-releases/<sha>/` and writes `manifest.json` last.
  - The role can only `PutObject`, so it uses conditional writes (`If-None-Match: *`). If any file for that sha already exists, the run ends green with a notice. Dispatching with `force` rebuilds and overwrites everything, manifest included.
  - An `AccessDenied` fails with an error naming the IAM grant in `deploy/aws/deploy_roles.py`, which a human applies.

A published release does nothing until a deploy whose image names that sha is
live. canopy-web only serves the release for its own `RUNNER_CODE_SHA`.

## Signing secrets (repository secrets; all optional)

Leave any of them out and that platform builds unsigned. The manifest then says
`signed: false` / `notarized: false`; an ad-hoc signature is never reported as signed.

### macOS: Developer ID + notarization

| Secret | Where it comes from |
|---|---|
| `MACOS_CERTIFICATE_P12_BASE64` | A **Developer ID Application** certificate. An Account Holder or Admin creates it at developer.apple.com → Certificates. In Keychain Access, export the certificate together with its private key as `.p12`, then run `base64 -i cert.p12 \| pbcopy`. |
| `MACOS_CERTIFICATE_PASSWORD` | The password you set when exporting the `.p12`. |
| `MACOS_SIGNING_IDENTITY` | The certificate's full name, for example `Developer ID Application: Dimagi, Inc. (TEAMID1234)`. `security find-identity -v -p codesigning` lists it. |
| `APPLE_API_KEY_P8_BASE64` | An **App Store Connect API key** for `notarytool`: App Store Connect → Users and Access → Integrations → App Store Connect API → Team Keys, role **Developer**. The `.p8` downloads only once; base64 it. |
| `APPLE_API_KEY_ID` | That key's Key ID. |
| `APPLE_API_ISSUER_ID` | The Issuer ID shown above the keys list. |

Signing needs the first three secrets. Notarization needs the last three and a
signed build.

### Windows: Azure Trusted Signing (Artifact Signing)

An OV/EV certificate in a PFX can no longer be issued: since 2023 the key must
live in an HSM. Azure Trusted Signing provides that and is used through
`azure/artifact-signing-action`.

| Secret | Where it comes from |
|---|---|
| `TRUSTED_SIGNING_ENDPOINT` | The Trusted Signing account's regional endpoint, for example `https://eus.codesigning.azure.net/`. It must be the region the account was created in. |
| `TRUSTED_SIGNING_ACCOUNT` | The Trusted Signing **account** name (Azure portal → Trusted Signing Accounts). |
| `TRUSTED_SIGNING_PROFILE` | The **certificate profile** name in that account, usually Public Trust. Creating one needs the organization's identity validation to be complete. |
| `AZURE_TENANT_ID` | The Entra ID tenant the account lives in. |
| `AZURE_CLIENT_ID` | The client ID of an **Entra app registration** for CI. Give it the **Trusted Signing Certificate Profile Signer** role on the certificate profile (or the account). |
| `AZURE_CLIENT_SECRET` | A client secret of that app registration. It expires, so put the rotation in the calendar. |

CI signs the executables inside the payload first (`exe`, `dll`, `pyd`, `node`),
because SmartScreen and Defender judge `python.exe` and `node.exe` when they run.
It then signs the installer, and records `signed: true` only when Windows reports
the installer's signature as `Valid`.

### Publishing

`AWS_ROLE_ARN` is the same deploy role `deploy-labs.yml` uses. Its
`UploadRunnerReleasesOnly` statement (`deploy/aws/deploy_roles.py`) grants
`s3:PutObject` on `runner-releases/*` only.
