<#
.SYNOPSIS
  Install (or update) the canopy laptop runner on Windows -- the Task Scheduler
  counterpart to install-runner.sh's launchd path.

.DESCRIPTION
  The runner's Python is platform-neutral with two exceptions: the producers
  (inbox.py, schedules.py) shell out to `gog` and do date math, cdp_control drives
  emdash through `node`, and the transcript layer is pure pathlib. What made the
  daemon macOS-only was its SUPERVISION -- two launchd jobs and a `launchctl
  kickstart`. This script provides the other half of that split (see
  canopy_runner/platform_jobs.py for the in-process half); the second exception is
  the collision dialog, which carries its own split in canopy_runner/dialog.py
  (osascript on macOS, WScript.Shell.Popup here). That one was macOS-only long
  after this file said supervision was the only gap -- on Windows the collision
  dialog never rendered and the message was dropped without anyone being asked.

  It mirrors install-runner.sh step for step, deliberately:

    fetch ref -> resolve the runner-source sha -> `git archive` to a temp tree
    -> stamp _build_info.py -> build the three wheels -> `uv tool install`
    -> provision the CDP sidecar -> register two Scheduled Tasks

  Why a SNAPSHOT and not the working tree: the daemon must not be able to change
  what it runs because someone did a `git checkout` in their clone. Building from
  `git archive <ref>` makes installing a branch an act rather than a slip.

  THE TWO JOBS, and why they are two (unchanged from the launchd design): if the
  runner updated itself, a build that crash-loops on startup could never update
  again -- it never heartbeats, never learns it is behind, and the box stays
  bricked. An independent timer keeps checking whatever state the runner is in.

    \Canopy\canopy-runner           AtLogOn + restart-on-failure   (launchd KeepAlive)
    \Canopy\canopy-runner-updater   every 30 min                   (launchd StartInterval)

  WINDOWS DIFFERENCES worth knowing, none of them cosmetic:

  - **Task Scheduler has no StandardOutPath.** launchd redirects the job's stdout
    and stderr for you; Task Scheduler does not. Both jobs are therefore wrapped
    in `cmd.exe /c "... >> log 2>&1"` so ~/.canopy/runner.log and updater.log
    exist on Windows exactly as they do on macOS. Without the wrapper the logs
    are simply empty, which looks identical to a healthy quiet runner.
  - **No PATH stanza.** launchd starts with a stripped PATH (hence the Homebrew
    entries in the plists); a Scheduled Task running as the logged-in user
    inherits the user's PATH, so `gog`, `node`, `npm`, `git` and `uv` resolve as
    they do in that user's shell. If they do not, they are not on the user's PATH
    either and that is the thing to fix.
  - **Session-bound, same as a LaunchAgent.** These are registered for the
    interactive user, not as a service, because the runner drives emdash's real
    UI over CDP and genuinely needs a desktop session. Logging out stops both
    jobs together -- the same boundary the updater plist documents, and the same
    countermeasure applies (the supervisor's dark-runner banner).

.PARAMETER Ref          git ref to install (default: origin/main)
.PARAMETER Repo         canopy-web checkout to build from (default: $env:CANOPY_WEB_REPO or ~\emdash-projects\canopy-web)
.PARAMETER Config       runner config path (default: ~\.canopy\runner.json)
.PARAMETER IfStale      auto-update mode: install only if the control plane says this box is behind
.PARAMETER NoTasks      install the package but do not touch the Scheduled Tasks
.PARAMETER NoAutoUpdate skip registering the updater task

.EXAMPLE
  .\install-runner.ps1
.EXAMPLE
  .\install-runner.ps1 -Ref my-branch -NoTasks
#>
[CmdletBinding()]
# Write-Host is deliberate. PSAvoidUsingWriteHost targets modules and functions
# whose output a caller may want to capture; this is an installer whose output is
# diagnostic narration for a human, mirroring the bash installer's `echo`. It
# still reaches ~/.canopy/*.log: the Scheduled Task wraps the script in
# `cmd.exe /c ... >> log`, and PowerShell renders that stream to stdout.
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]
param(
    [string] $Ref    = "origin/main",
    [string] $Repo   = $(if ($env:CANOPY_WEB_REPO) { $env:CANOPY_WEB_REPO } else { Join-Path $HOME "emdash-projects\canopy-web" }),
    [string] $Config = $(Join-Path $HOME ".canopy\runner.json"),
    [switch] $IfStale,
    [switch] $NoTasks,
    [switch] $NoAutoUpdate
)

# Write-Host is deliberate and suppressed below. PSAvoidUsingWriteHost targets
# modules and functions whose output a caller may want to capture; this is an
# installer whose output is diagnostic narration for a human, mirroring the
# bash installer's `echo`. It still reaches ~/.canopy/*.log: the Scheduled Task
# wraps the script in `cmd.exe /c ... >> log`, and PowerShell renders the
# information stream to stdout, which that redirect captures.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RUNNER_SRC   = "runner/canopy_runner/canopy_runner"
$TASK_FOLDER  = "\Canopy"
$RUNNER_TASK  = "canopy-runner"
$UPDATER_TASK = "canopy-runner-updater"
$CanopyDir    = Join-Path $HOME ".canopy"

function Fail($msg) { Write-Error $msg; exit 1 }
function Info($msg) { Write-Host "==> $msg" }
function Stamp { (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }

# --- preflight ---------------------------------------------------------------
foreach ($tool in @("git", "uv")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool not found on PATH. uv: https://docs.astral.sh/uv/"
    }
}
# `rev-parse`, not `Test-Path $Repo\.git`: in a git WORKTREE .git is a FILE, and
# the directory test would reject a perfectly good checkout.
& git -C $Repo rev-parse --git-dir *> $null
if ($LASTEXITCODE -ne 0) { Fail "not a git checkout: $Repo (set CANOPY_WEB_REPO)" }
New-Item -ItemType Directory -Force -Path $CanopyDir | Out-Null

# --- auto-update mode --------------------------------------------------------
# Ask the INSTALLED runner whether this box should update, and PIN to the sha the
# control plane expects. This must NOT track origin/main: the expected sha is the
# runner source in the DEPLOYED image, already through CI and a deploy. Installing
# main would run undeployed code AND leave code_sha != expected_code_sha forever,
# so the staleness banner would fire permanently on correctly-updating boxes.
if ($IfStale) {
    $binDir   = (& uv tool dir --bin 2>$null)
    $checkBin = if ($binDir) { Join-Path $binDir "canopy-runner.exe" } else { $null }
    if (-not ($checkBin -and (Test-Path $checkBin))) {
        $cmd = Get-Command canopy-runner -ErrorAction SilentlyContinue
        $checkBin = if ($cmd) { $cmd.Source } else { $null }
    }
    if (-not $checkBin) { Write-Host "$(Stamp) -IfStale: no installed runner to check."; exit 0 }
    if (-not (Test-Path $Config)) { Write-Host "$(Stamp) -IfStale: no config at $Config."; exit 0 }

    $answer = (& $checkBin update-check --config $Config 2>&1 | Select-Object -Last 1)
    $parts  = "$answer".Trim() -split '\s+'
    $status = $parts[0]
    $expected = if ($parts.Count -gt 1) { $parts[1] } else { "" }
    switch ($status) {
        "stale" {
            $short = if ($expected.Length -ge 12) { $expected.Substring(0,12) } else { $expected }
            Write-Host "$(Stamp) -IfStale: behind -- installing $short"
            $Ref = $expected
        }
        # All "do nothing", and all exit 0: this runs on a timer, so a non-zero
        # exit for the ordinary case would train everyone to ignore the log.
        { $_ -in @("current","busy","unknown") } {
            Write-Host "$(Stamp) -IfStale: $status -- nothing to do."; exit 0
        }
        default {
            # The installed runner does not understand `update-check`, i.e. it
            # predates auto-update. Say so -- "nothing to do" forever would
            # otherwise look exactly like a healthy up-to-date box.
            Write-Warning "$(Stamp) -IfStale: update-check unavailable from $checkBin (answered: '$answer')."
            Write-Warning "    This runner predates auto-update; run install-runner.ps1 once by hand."
            exit 0
        }
    }
}

# --- resolve the ref + the runner-source provenance --------------------------
Info "fetching $Repo"
& git -C $Repo fetch --quiet origin
if ($LASTEXITCODE -ne 0) { Write-Host "    (fetch failed -- using local refs)" }

& git -C $Repo rev-parse --verify --quiet "$Ref^{commit}" *> $null
if ($LASTEXITCODE -ne 0) {
    if ($IfStale) { Write-Host "    ref $Ref not in this clone yet; will retry."; exit 0 }
    Fail "no such ref: $Ref"
}

# The provenance the runner reports and the server compares against: the last
# commit that touched the runner's OWN source, NOT the repo HEAD (which moves on
# every canopy-web commit and would mark every runner stale on a CSS change).
$RunnerSha   = (& git -C $Repo log -1 --format=%H $Ref -- $RUNNER_SRC).Trim()
$CommittedAt = (& git -C $Repo log -1 --format=%ct $Ref -- $RUNNER_SRC).Trim()
$BuiltAt     = Stamp
if ($RunnerSha) {
    Info "ref $Ref | runner source at $($RunnerSha.Substring(0,12))"
} else {
    # Say so rather than installing an anonymous runner: empty is fail-safe (the
    # supervisor stays silent) but silence is indistinguishable from working.
    Write-Warning "could not resolve the runner source sha (shallow clone?). This runner will"
    Write-Warning "report unknown provenance and the staleness alert will never fire for it."
}

$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("canopy-runner-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
try {
    Info "exporting $Ref"
    $tar = Join-Path $Tmp "src.tar"
    & git -C $Repo archive --output $tar $Ref
    if ($LASTEXITCODE -ne 0) { Fail "git archive failed" }
    # bsdtar ships in Windows 10 1803+ as tar.exe.
    & tar -x -f $tar -C $Tmp
    if ($LASTEXITCODE -ne 0) { Fail "tar extraction failed (is tar.exe present? Windows 10 1803+)" }

    # Stamp build provenance into the TEMP tree only -- never the working checkout.
    $buildInfo = @"
"""Build provenance, stamped by install-runner.ps1. Generated -- do not edit."""
from __future__ import annotations

SHA = "$RunnerSha"
BUILT_AT = "$BuiltAt"
COMMITTED_AT = $(if ($CommittedAt) { $CommittedAt } else { 0 })
"@
    Set-Content -Path (Join-Path $Tmp "$RUNNER_SRC/_build_info.py") -Value $buildInfo -Encoding UTF8

    Info "building wheels"
    $dist = Join-Path $Tmp "dist"
    foreach ($pkg in @("packages/canopy_cron", "packages/canopy_transcript", "runner/canopy_runner")) {
        & uv build --quiet --wheel -o $dist (Join-Path $Tmp $pkg)
        if ($LASTEXITCODE -ne 0) { Fail "wheel build failed for $pkg" }
    }

    $wheel = Get-ChildItem -Path $dist -Filter "canopy_runner-*.whl" | Select-Object -First 1
    if (-not $wheel) { Fail "no canopy_runner wheel produced in $dist" }
    Info "installing $($wheel.Name)"
    # --find-links supplies canopy-cron / canopy-transcript while the real index
    # still serves croniter and the realtime extra's websocket-client. The runner
    # itself is named by direct file:// URL so no index can shadow it.
    $uri = "file:///" + ($wheel.FullName -replace '\\','/')
    & uv tool install --force --find-links $dist "canopy-runner[realtime] @ $uri"
    if ($LASTEXITCODE -ne 0) { Fail "uv tool install failed" }
} finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}

# Ask uv where it PUT the executable rather than trusting PATH order -- an older
# canopy-runner earlier on PATH would otherwise be what the task points at, and
# the daemon would keep running the version this script just replaced.
$binDir = (& uv tool dir --bin 2>$null)
$Bin = if ($binDir) { Join-Path $binDir "canopy-runner.exe" } else { $null }
if (-not ($Bin -and (Test-Path $Bin))) {
    $cmd = Get-Command canopy-runner -ErrorAction SilentlyContinue
    $Bin = if ($cmd) { $cmd.Source } else { $null }
}
if (-not ($Bin -and (Test-Path $Bin))) { Fail "installed, but the canopy-runner executable was not found" }

Info "provisioning the CDP sidecar's node deps"
& $Bin install-sidecar
if ($LASTEXITCODE -ne 0) { Write-Warning "install-sidecar failed -- emdash control will not work until node/npm are available" }

if ($NoTasks) { Info "done (Scheduled Tasks untouched)"; exit 0 }

# --- Scheduled Tasks ---------------------------------------------------------
# Register-ScheduledTask is idempotent via -Force, which replaces an existing
# definition in place. That is the Task Scheduler analogue of the plist's
# bootout/bootstrap cycle -- and unlike that cycle it has no window in which the
# job is unregistered, so a failure cannot leave the box with NO runner task.
function Register-CanopyTask {
    param(
        [Parameter(Mandatory)] [string]   $Name,
        [Parameter(Mandatory)] [string]   $CommandLine,   # passed to cmd.exe /c
        [Parameter(Mandatory)] [object[]] $Triggers,
        [switch] $RestartOnFailure
    )
    # cmd.exe wrapper: Task Scheduler has no StandardOutPath, so without this the
    # logs macOS gets for free simply never exist on Windows.
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$CommandLine`""
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = if ($RestartOnFailure) {
        # The KeepAlive analogue. ExecutionTimeLimit 0 = never kill a long-running
        # daemon; without it Task Scheduler stops the task after 3 days by default.
        New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
    } else {
        New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew
    }
    Register-ScheduledTask -TaskName $Name -TaskPath $TASK_FOLDER -Action $action `
        -Trigger $Triggers -Principal $principal -Settings $settings -Force | Out-Null
    Info "registered $TASK_FOLDER\$Name"
}

$runnerLog  = Join-Path $CanopyDir "runner.log"
$updaterLog = Join-Path $CanopyDir "updater.log"

Register-CanopyTask -Name $RUNNER_TASK -RestartOnFailure `
    -CommandLine "`"$Bin`" run --config `"$Config`" >> `"$runnerLog`" 2>&1" `
    -Triggers @(New-ScheduledTaskTrigger -AtLogOn)

if (-not $NoAutoUpdate) {
    # A COPY of this installer, frozen at the last install -- so a stray
    # `git checkout` in the repo cannot change the script this timer runs. The
    # repo remains the source of the BUILD (-Repo, for `git archive`).
    $stage2 = Join-Path $CanopyDir "canopy-runner-update.ps1"
    Copy-Item -Path $PSCommandPath -Destination $stage2 -Force

    # Not AtLogOn: the installer restarts the runner, and login is not a moment
    # to bounce a working daemon. Repetition runs for the life of the session.
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(30) `
        -RepetitionInterval (New-TimeSpan -Minutes 30)
    $ps = (Get-Command powershell.exe).Source
    Register-CanopyTask -Name $UPDATER_TASK -Triggers @($trigger) `
        -CommandLine "`"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$stage2`" -IfStale -Repo `"$Repo`" >> `"$updaterLog`" 2>&1"
}

# Start the runner now so a first install does not wait for the next logon.
& schtasks /Run /TN "$TASK_FOLDER\$RUNNER_TASK" *> $null
Info "done -- logs at $runnerLog"
