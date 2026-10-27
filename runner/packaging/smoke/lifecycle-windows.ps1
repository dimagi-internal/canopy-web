<#
.SYNOPSIS
  The Windows runner's whole life on a throwaway VM: install, setup, run, update
  over the RUNNING copy (twice), uninstall.

    pwsh runner/packaging/smoke/lifecycle-windows.ps1 -DistA <dir> -DistB <dir> -DistC <dir>

  Each -Dist* holds Canopy-Runner-Setup-windows-x86_64.exe and its
  windows-x86_64.json record (manifest.py record); A, B and C are the same payload
  stamped with three different shas (release.json), so each is a distinct version
  to the installer and to self-update. Run by .github/workflows/runner-windows.yml.

  What it covers, in order:
    1. A first install, the way a person's runs: no /UPDATE, so the installer's own
       [Run] step runs `canopy-runner setup`. A throwaway ~\.canopy\runner.json is
       written first, pointing at an unreachable canopy-web (127.0.0.1:9), so setup
       finds the box "paired" and goes straight to registering the tasks — no pairing,
       no network, no auth. (Today this is a KNOWN BUG, checked strictly: see step 1.)
    2. `canopy-runner setup --no-pair` again, captured (the path every update runs).
    3. Both \Canopy\ tasks as setup_cmd.register_script registers them: the runner
       with TWO triggers (AtLogOn + a 5-minute repetition) and IgnoreNew; the updater
       on a 30-minute repetition; both running current\python\pythonw.exe -I -X utf8.
    4. The daemon reaches Running, writes ~\.canopy\runner.log, and its loop turns
       (the heartbeat file keeps moving) against a server it cannot reach.
    5. The updater task runs (self-update --if-stale against the unreachable server),
       writes updater.log, and leaves the daemon up.
    6. Update A -> B through the updater's own code, self_update.install_windows (the
       installer with /UPDATE over the running copy, setup --no-pair, /End + /Run,
       prune), run from current\python like the updater task. current -> B, A kept
       (it was running), and the daemon is Running again with a NEW startup line in
       runner.log — within the 5-minute repeat trigger's window if /Run lost the race.
    7. Update B -> C with the bare installer /UPDATE over the running daemon:
       current -> C, A pruned, B kept (previous), the daemon untouched.
    8. Uninstall: tasks ended and deleted, versions\ and current gone, no process
       left running from the install dir, ~\.canopy kept.

  Not covered (needs a real canopy-web): pairing, claiming a turn, downloading a
  release (`self-update` without a reachable server never gets past "is there a
  newer one?"), signed builds.
#>
param(
  [Parameter(Mandatory = $true)][string]$DistA,
  [Parameter(Mandatory = $true)][string]$DistB,
  [Parameter(Mandatory = $true)][string]$DistC,
  # The repeat trigger fires every 5 minutes; a restart that lost the /End-/Run race
  # comes back on the next one.
  [int]$RestartTimeoutSeconds = 390
)
$ErrorActionPreference = 'Stop'

$ExeName = 'Canopy-Runner-Setup-windows-x86_64.exe'
function Read-Dist($dir) {
  $dir = (Resolve-Path $dir).Path
  $rec = Get-Content (Join-Path $dir 'windows-x86_64.json') -Raw | ConvertFrom-Json
  [pscustomobject]@{ Exe = (Join-Path $dir $ExeName); Sha = $rec.sha; Sha12 = $rec.sha.Substring(0, 12) }
}
$A = Read-Dist $DistA; $B = Read-Dist $DistB; $C = Read-Dist $DistC
if (@($A.Sha12, $B.Sha12, $C.Sha12) | Group-Object | Where-Object Count -gt 1) {
  throw "A, B and C must be three different versions ($($A.Sha12) $($B.Sha12) $($C.Sha12))"
}

$App = Join-Path $env:LOCALAPPDATA 'Programs\Canopy Runner'
$Current = Join-Path $App 'current'
$Canopy = Join-Path $env:USERPROFILE '.canopy'
$Config = Join-Path $Canopy 'runner.json'
$RunnerLog = Join-Path $Canopy 'runner.log'
$UpdaterLog = Join-Path $Canopy 'updater.log'
$Heartbeat = Join-Path $Canopy 'heartbeat'
$RunnerTask = 'canopy-runner'
$UpdaterTask = 'canopy-runner-updater'
$Logs = if ($env:RUNNER_TEMP) { Join-Path $env:RUNNER_TEMP 'lifecycle-logs' } else { Join-Path $env:TEMP 'lifecycle-logs' }
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$script:Fails = 0

function Pass($m) { Write-Host "PASS  $m" }
function Fail($m) { Write-Host "FAIL  $m"; Write-Output "::error::$m"; $script:Fails++ }
function Check($desc, [scriptblock]$block) {
  try {
    $out = & $block 2>&1 | Out-String
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "exit $LASTEXITCODE`: $out" }
    $first = ($out.Trim() -split "`n")[0]
    Pass ("$desc" + $(if ($first) { " - $first" } else { "" }))
  } catch { Fail "$desc - $($_.Exception.Message)" }
  $global:LASTEXITCODE = 0
}
function Assert($cond, $msg) { if (-not $cond) { throw $msg } }
function Step($m) { Write-Host ""; Write-Host "==> $m" }

function Task($name) { Get-ScheduledTask -TaskPath '\Canopy\' -TaskName $name -ErrorAction SilentlyContinue }
function Wait-Until([int]$seconds, [scriptblock]$cond) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $seconds) {
    if (& $cond) { return [int]$sw.Elapsed.TotalSeconds }
    Start-Sleep -Seconds 2
  }
  return -1
}
function Startups { if (Test-Path $RunnerLog) { @(Select-String -Path $RunnerLog -Pattern 'canopy-runner starting').Count } else { 0 } }
function Junction-Target {
  $item = Get-Item $Current -Force
  Assert ($item.LinkType -eq 'Junction') "current is not a junction (LinkType=$($item.LinkType))"
  @($item.Target)[0]
}
function Versions { @(Get-ChildItem (Join-Path $App 'versions') -Directory -ErrorAction SilentlyContinue | ForEach-Object Name) }
function App-Processes {
  @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($App, [StringComparison]::OrdinalIgnoreCase) })
}
function Kill-Tree([int]$id) {
  Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue |
    ForEach-Object { Kill-Tree $_.ProcessId }
  Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}
function Show-Tree([int]$id, [string]$indent = '  ') {
  Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "$indent$($_.ProcessId) $($_.Name): $($_.CommandLine)"
    Show-Tree $_.ProcessId "$indent  "
  }
}
# Returns the exit code, or $null when it had to be killed (after showing what it
# was stuck in) - so the checks after it still run and say what state it left.
function Run-Installer($exe, [string[]]$extra, $log, [int]$timeoutSeconds = 600) {
  $argList = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$log") + $extra
  $p = Start-Process -FilePath $exe -ArgumentList $argList -PassThru
  $null = $p.Handle  # without it ExitCode reads back empty
  if (-not $p.WaitForExit($timeoutSeconds * 1000)) {
    Write-Host "installer still running after ${timeoutSeconds}s; its process tree:"
    Show-Tree $p.Id
    Kill-Tree $p.Id
    return $null
  }
  $p.ExitCode
}
# A command with a deadline, its output captured: @{ Exit; Out } (Exit $null = killed).
function Run-Captured([string]$file, [string[]]$argList, [int]$timeoutSeconds = 300) {
  $o = New-TemporaryFile; $e = New-TemporaryFile
  $p = Start-Process -FilePath $file -ArgumentList $argList -PassThru -NoNewWindow `
    -RedirectStandardOutput $o -RedirectStandardError $e
  $null = $p.Handle
  $exit = $null
  if ($p.WaitForExit($timeoutSeconds * 1000)) { $p.WaitForExit(); $exit = $p.ExitCode }
  else { Write-Host "still running after ${timeoutSeconds}s; its process tree:"; Show-Tree $p.Id; Kill-Tree $p.Id }
  $out = (Get-Content $o -Raw) + (Get-Content $e -Raw)
  Remove-Item $o, $e -ErrorAction SilentlyContinue
  @{ Exit = $exit; Out = $out }
}
function Dump-Logs {
  foreach ($f in @($RunnerLog, $UpdaterLog)) {
    if (Test-Path $f) {
      Copy-Item $f $Logs -Force
      Write-Host "----- tail $f"
      Get-Content $f -Tail 60 | ForEach-Object { Write-Host "  $_" }
    }
  }
  foreach ($n in @($RunnerTask, $UpdaterTask)) {
    $t = Task $n
    if ($t) {
      $i = $t | Get-ScheduledTaskInfo
      Write-Host ("----- \Canopy\{0}: State={1} LastRunTime={2} LastTaskResult=0x{3:X}" -f $n, $t.State, $i.LastRunTime, $i.LastTaskResult)
    }
  }
}

try {
  # --- 0. a throwaway "paired" config ------------------------------------------------
  Step "throwaway runner.json (canopy-web unreachable: 127.0.0.1:9)"
  if (Test-Path $App) { throw "$App already exists - this script wants a clean VM" }
  New-Item -ItemType Directory -Force -Path $Canopy | Out-Null
  @{
    base_url  = 'http://127.0.0.1:9'
    token     = 'ci-lifecycle-not-a-real-token'
    runner_id = 'ci-windows-lifecycle'
  } | ConvertTo-Json | Set-Content -Path $Config -Encoding utf8
  Pass "wrote $Config"

  # --- 1. first install: the installer runs setup itself -----------------------------
  # Inno processes [Run] before CurStepChanged(ssPostInstall); the setup entry's
  # BeforeInstall: PointCurrent makes `current` first (this used to fail: setup found
  # no current\ and `|| pause` hung on a console nobody could see).
  Step "install A ($($A.Sha12)) - a first install, no /UPDATE: the installer runs setup"
  $rc = Run-Installer $A.Exe @() (Join-Path $Logs 'install-A.log') 120
  Check "first install exited 0 (setup ran; not killed waiting on a pause)" {
    Assert ($rc -eq 0) $(if ($null -eq $rc) { 'killed after 120s - setup most likely hit `|| pause`' } else { "exit $rc" })
  }
  Check "the first install's setup registered \Canopy\$RunnerTask" { Assert (Task $RunnerTask) 'not registered' }
  Check "current is a junction to versions\$($A.Sha12)" {
    $t = Junction-Target; Assert ($t -like "*\versions\$($A.Sha12)") "points at $t"; $t
  }
  Check "current-version.txt names A" {
    $v = (Get-Content (Join-Path $App 'current-version.txt') -Raw).Trim()
    Assert ($v -eq $A.Sha12) $v
  }

  # --- 2. setup --no-pair, captured --------------------------------------------------
  Step "canopy-runner setup --no-pair (what every update runs; here it registers the tasks)"
  $Shim = Join-Path $Current 'bin\canopy-runner.cmd'
  $r = Run-Captured 'cmd.exe' @('/c', "`"`"$Shim`" setup --no-pair --config `"$Config`"`"")
  Write-Host $r.Out
  if ($r.Exit -ne 0) { Fail "setup --no-pair: $(if ($null -eq $r.Exit) { 'killed after 300s' } else { "exit $($r.Exit)" })" } else { Pass "setup --no-pair exited 0" }

  # --- 3. the tasks, as registered ---------------------------------------------------
  Step "the \Canopy\ tasks"
  Check "\Canopy\$RunnerTask has TWO triggers: AtLogOn + every 5 minutes" {
    $t = Task $RunnerTask
    $tr = @($t.Triggers)
    Assert ($tr.Count -eq 2) "$($tr.Count) trigger(s): $(($tr | ForEach-Object { $_.CimClass.CimClassName }) -join ', ')"
    $logon = @($tr | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
    $time = @($tr | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' })
    Assert ($logon.Count -eq 1) "no AtLogOn trigger"
    Assert ($logon[0].UserId) "the AtLogOn trigger names no user (means 'any user' - admin only)"
    Assert ($time.Count -eq 1) "no time trigger"
    Assert ($time[0].Repetition.Interval -eq 'PT5M') "repetition $($time[0].Repetition.Interval)"
    "AtLogOn($($logon[0].UserId)) + repeat $($time[0].Repetition.Interval)"
  }
  Check "\Canopy\$RunnerTask settings: IgnoreNew, no time limit, restarts" {
    $s = (Task $RunnerTask).Settings
    Assert ($s.MultipleInstances -eq 'IgnoreNew') "MultipleInstances=$($s.MultipleInstances)"
    Assert ($s.ExecutionTimeLimit -in @('PT0S', $null, '')) "ExecutionTimeLimit=$($s.ExecutionTimeLimit)"
    Assert ($s.RestartCount -ge 1) "RestartCount=$($s.RestartCount)"
    "IgnoreNew, limit=$($s.ExecutionTimeLimit), restart x$($s.RestartCount)"
  }
  Check "\Canopy\$RunnerTask runs current\python\pythonw.exe -I -X utf8 ... run" {
    $a = @((Task $RunnerTask).Actions)[0]
    Assert ($a.Execute -like '*\current\python\pythonw.exe') "Execute=$($a.Execute)"
    Assert ($a.Arguments -match '-I -X utf8 -m canopy_runner\.main run') "Arguments=$($a.Arguments)"
    Assert ($a.Arguments -match [regex]::Escape($RunnerLog)) "no --log-file $RunnerLog in $($a.Arguments)"
  }
  Check "\Canopy\$RunnerTask principal: this user, Interactive, Limited" {
    $p = (Task $RunnerTask).Principal
    Assert ($p.RunLevel -eq 'Limited') "RunLevel=$($p.RunLevel)"
    Assert ($p.LogonType -eq 'Interactive') "LogonType=$($p.LogonType)"
    "$($p.UserId)"
  }
  Check "\Canopy\$UpdaterTask repeats every 30 minutes and runs self-update --if-stale" {
    $t = Task $UpdaterTask
    $time = @(@($t.Triggers) | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' })
    Assert ($time.Count -eq 1) "no time trigger"
    Assert ($time[0].Repetition.Interval -eq 'PT30M') "repetition $($time[0].Repetition.Interval)"
    $a = @($t.Actions)[0]
    Assert ($a.Execute -like '*\current\python\pythonw.exe') "Execute=$($a.Execute)"
    Assert ($a.Arguments -match '-I -X utf8 -m canopy_runner\.main self-update --if-stale') "Arguments=$($a.Arguments)"
  }

  # --- 4. the daemon -----------------------------------------------------------------
  Step "the daemon (setup started it)"
  $s = Wait-Until 90 { (Task $RunnerTask).State -eq 'Running' }
  if ($s -ge 0) { Pass "\Canopy\$RunnerTask is Running (${s}s)" } else { Fail "\Canopy\$RunnerTask never reached Running (State=$((Task $RunnerTask).State))" }
  $s = Wait-Until 90 { (Startups) -ge 1 }
  if ($s -ge 0) { Pass "runner.log has its startup banner (${s}s)" } else { Fail "no 'canopy-runner starting' in $RunnerLog" }
  Check "the loop turns: the heartbeat file keeps moving" {
    $first = Wait-Until 60 { Test-Path $Heartbeat }
    Assert ($first -ge 0) "no $Heartbeat"
    $t0 = (Get-Item $Heartbeat).LastWriteTimeUtc
    $moved = Wait-Until 60 { (Get-Item $Heartbeat).LastWriteTimeUtc -gt $t0 }
    Assert ($moved -ge 0) "heartbeat stuck at $t0"
    "moved after ${moved}s"
  }
  Check "the daemon runs from the install dir" {
    $procs = App-Processes
    Assert ($procs.Count -ge 1) "no process under $App"
    ($procs | ForEach-Object { "$($_.Name) $($_.ProcessId)" }) -join ', '
  }

  # --- 5. the updater task -----------------------------------------------------------
  Step "run the updater task (self-update --if-stale; canopy-web unreachable)"
  & schtasks /Run /TN "\Canopy\$UpdaterTask" | Out-Host
  $global:LASTEXITCODE = 0
  $s = Wait-Until 180 { (Test-Path $UpdaterLog) -and (Task $UpdaterTask).State -ne 'Running' }
  if ($s -ge 0) {
    $i = Task $UpdaterTask | Get-ScheduledTaskInfo
    Pass ("updater ran and wrote updater.log ({0}s, LastTaskResult=0x{1:X})" -f $s, $i.LastTaskResult)
  } else { Fail "the updater task did not finish with an updater.log inside 180s" }
  Check "the daemon is still Running after the updater ran" { Assert ((Task $RunnerTask).State -eq 'Running') "State=$((Task $RunnerTask).State)" }

  # --- 6. A -> B through self_update.install_windows ---------------------------------
  Step "update A -> B ($($B.Sha12)) with the updater's own install_windows, over the running daemon"
  $before = Startups
  $PyCur = Join-Path $Current 'python\python.exe'
  $code = 'import sys; from pathlib import Path; from canopy_runner import self_update as s; ' +
          's.install_windows(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))'
  $env:CANOPY_RUNNER_UNDER_JOB = 'updater'
  $r = Run-Captured $PyCur @('-I', '-X', 'utf8', '-c', "`"$code`"", "`"$($B.Exe)`"", $B.Sha, "`"$App`"", "`"$Config`"") 900
  Remove-Item Env:\CANOPY_RUNNER_UNDER_JOB
  Write-Host $r.Out
  if ($r.Exit -ne 0) { Fail "install_windows: $(if ($null -eq $r.Exit) { 'killed after 900s' } else { "exit $($r.Exit)" })" } else { Pass "install_windows returned" }
  Check "current -> versions\$($B.Sha12)" { $t = Junction-Target; Assert ($t -like "*\versions\$($B.Sha12)") "points at $t"; $t }
  Check "A kept (it was running), B present" {
    $v = Versions
    Assert ($v -contains $A.Sha12) "A gone: $($v -join ', ')"
    Assert ($v -contains $B.Sha12) "B missing: $($v -join ', ')"
    $v -join ', '
  }
  $s = Wait-Until $RestartTimeoutSeconds { (Task $RunnerTask).State -eq 'Running' -and (Startups) -gt $before }
  if ($s -ge 0) { Pass "the daemon is Running again on B, a new startup in runner.log (${s}s)" }
  else { Fail "the daemon did not come back within ${RestartTimeoutSeconds}s (State=$((Task $RunnerTask).State), startups $before -> $(Startups))" }

  # --- 7. B -> C with the bare installer over the running daemon ---------------------
  Step "update B -> C ($($C.Sha12)) with the installer /UPDATE, daemon still running from B"
  $rc = Run-Installer $C.Exe @('/UPDATE') (Join-Path $Logs 'install-C.log')
  Check "installer C /UPDATE exited 0 with B running" { Assert ($rc -eq 0) $(if ($null -eq $rc) { 'killed after 600s' } else { "exit $rc" }) }
  Check "current -> versions\$($C.Sha12)" { $t = Junction-Target; Assert ($t -like "*\versions\$($C.Sha12)") "points at $t"; $t }
  Check "A pruned, B kept (previous), C present" {
    $v = Versions
    Assert (-not ($v -contains $A.Sha12)) "A still there: $($v -join ', ')"
    Assert ($v -contains $B.Sha12) "B gone: $($v -join ', ')"
    Assert ($v -contains $C.Sha12) "C missing: $($v -join ', ')"
    $v -join ', '
  }
  Check "/UPDATE left the running daemon alone" { Assert ((Task $RunnerTask).State -eq 'Running') "State=$((Task $RunnerTask).State)" }

  # --- 8. uninstall ------------------------------------------------------------------
  Step "uninstall (silent) with the daemon running"
  $unins = Join-Path $App 'unins000.exe'
  $p = Start-Process -FilePath $unins -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
  # The uninstaller copies itself to %TEMP% and returns at once; wait for that copy.
  [void](Wait-Until 120 { -not (Test-Path (Join-Path $App 'versions')) })
  Check "uninstall removed versions\ and current" {
    Assert (-not (Test-Path (Join-Path $App 'versions'))) "versions\ still there: $((Versions) -join ', ')"
    Assert (-not (Test-Path $Current)) "current still there"
  }
  foreach ($n in @($RunnerTask, $UpdaterTask)) {
    Check "\Canopy\$n deleted" { Assert (-not (Task $n)) "still registered" }
  }
  Check "no process left running from the install dir" {
    $left = Wait-Until 30 { (App-Processes).Count -eq 0 }
    Assert ($left -ge 0) "still running: $((App-Processes | ForEach-Object { "$($_.Name) $($_.ProcessId) $($_.ExecutablePath)" }) -join '; ')"
  }
  Check "~\.canopy (the person's config) kept" { Assert (Test-Path $Config) "runner.json gone" }
} catch {
  Fail "aborted: $($_.Exception.Message)"
} finally {
  Dump-Logs
}

Write-Host ""
if ($script:Fails -gt 0) { Write-Host "== $($script:Fails) check(s) FAILED"; exit 1 }
Write-Host "== all lifecycle checks passed"
