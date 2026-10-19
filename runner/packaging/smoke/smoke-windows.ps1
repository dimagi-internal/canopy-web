<#
.SYNOPSIS
  Smoke-test the Windows installer end to end: install, exercise, uninstall.

    pwsh runner/packaging/smoke/smoke-windows.ps1 -Dist build\dist [-RunSetup]

  Installs silently with /UPDATE (so the installer does not run the interactive
  `canopy-runner setup`) into the current user's %LOCALAPPDATA%\Programs\Canopy Runner
  — PrivilegesRequired=lowest leaves it nowhere else — then checks the `current`
  junction and the bundled runtime, optionally runs `canopy-runner setup --no-pair`
  (-RunSetup: registers the two Scheduled Tasks; meant for a throwaway CI VM), then
  uninstalls silently and checks the tasks and files are gone.
#>
param(
  [Parameter(Mandatory = $true)][string]$Dist,
  [switch]$RunSetup
)
$ErrorActionPreference = 'Stop'
$Dist = (Resolve-Path $Dist).Path
$Exe = Join-Path $Dist 'Canopy-Runner-Setup-windows-x86_64.exe'
$Record = Get-Content (Join-Path $Dist 'windows-x86_64.json') -Raw | ConvertFrom-Json
$Versions = Get-Content (Join-Path $PSScriptRoot '..\versions.json') -Raw | ConvertFrom-Json
$App = Join-Path $env:LOCALAPPDATA 'Programs\Canopy Runner'
$script:Fails = 0

function Pass($m) { Write-Host "PASS  $m" }
function Fail($m) { Write-Host "FAIL  $m"; $script:Fails++ }
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

Write-Host ("== {0} ({1:N1} MB) signed={2}" -f $Exe, ((Get-Item $Exe).Length / 1MB), $Record.signed)
Check "installer sha256 matches the record" {
  Assert ((Get-FileHash $Exe -Algorithm SHA256).Hash.ToLower() -eq $Record.installer.sha256) "mismatch"
}
if ($Record.signed) {
  Check "installer Authenticode signature is valid" {
    $sig = Get-AuthenticodeSignature $Exe
    Assert ($sig.Status -eq 'Valid') "status $($sig.Status)"
    $sig.SignerCertificate.Subject
  }
}

Write-Host "==> install (silent, /UPDATE: no setup)"
$p = Start-Process -FilePath $Exe -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/UPDATE', "/LOG=$env:RUNNER_TEMP\canopy-install.log" -Wait -PassThru
if ($p.ExitCode -ne 0) { Get-Content "$env:RUNNER_TEMP\canopy-install.log" -Tail 40 -ErrorAction SilentlyContinue; throw "installer exited $($p.ExitCode)" }

$Current = Join-Path $App 'current'
Check "current is a junction to versions\<sha12>" {
  $item = Get-Item $Current -Force
  Assert ($item.LinkType -eq 'Junction') "LinkType=$($item.LinkType)"
  $target = @($item.Target)[0]
  $want = $Record.sha.Substring(0, 12)
  Assert ($target -like "*\versions\$want") "points at $target"
  $target
}
Check "nothing written under HKLM for the app" {
  $hklm = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
    Where-Object { (Get-ItemProperty $_.PSPath).DisplayName -eq 'Canopy Runner' }
  Assert (-not $hklm) "found an HKLM uninstall entry"
  $hkcu = Get-ChildItem 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall' |
    Where-Object { (Get-ItemProperty $_.PSPath).DisplayName -eq 'Canopy Runner' }
  Assert $hkcu "no HKCU uninstall entry"
  "HKCU only"
}

$Rt = $Current
$Py = Join-Path $Rt 'python\python.exe'
$Shim = Join-Path $Rt 'bin\canopy-runner.cmd'
$release = Get-Content (Join-Path $Rt 'release.json') -Raw | ConvertFrom-Json
Check "release.json is windows-x86_64 at the record's sha" {
  Assert ($release.platform -eq 'windows-x86_64' -and $release.sha -eq $Record.sha) "$($release.platform) $($release.sha)"
}
Check "canopy-runner --version names the stamped sha" {
  $v = & cmd /c "`"$Shim`" --version" 2>&1 | Out-String
  Assert ($v -match "rev $($Record.sha.Substring(0, 12))") $v
  $v.Trim()
}
Check "bundled python imports the runner and its deps" {
  & $Py -I -B -c "import canopy_runner, canopy_acp, canopy_cron, canopy_transcript, cryptography, websocket; from canopy_runner import _build_info as b; assert b.SHA == '$($Record.sha)'; assert getattr(b, 'PLATFORM', None) == 'windows-x86_64', b; print('ok')"
}
Check "bundled pythonw.exe present" { Assert (Test-Path (Join-Path $Rt 'python\pythonw.exe')) "missing" }
Check "bundled node is $($Versions.node.version)" {
  $nv = (& (Join-Path $Rt 'node\node.exe') --version).Trim()
  Assert ($nv -eq $Versions.node.version) $nv
  $nv
}
Check "CDP sidecar deps installed" {
  $cdp = (& $Py -I -B -c "import canopy_runner, pathlib; print(pathlib.Path(canopy_runner.__file__).parent / 'cdp')").Trim()
  Assert (Test-Path (Join-Path $cdp 'node_modules\playwright-core\package.json')) "no playwright-core under $cdp"
  Push-Location $cdp
  try { & (Join-Path $Rt 'node\node.exe') --input-type=module -e "await import('playwright-core'); console.log('ok')" } finally { Pop-Location }
}
Check "ACP adapter resolvable by canopy_acp" {
  $env:PATH = "$(Join-Path $Rt 'node');$(Join-Path $Rt 'acp');$env:PATH"
  & $Py -I -B -c "from canopy_acp.client import find_adapter; a = find_adapter(); assert a, a; print(a[-1])"
}

$tasks = @('\Canopy\canopy-runner', '\Canopy\canopy-runner-updater')
$hasSetup = $false
& cmd /c "`"$Shim`" setup --help" *> $null
if ($LASTEXITCODE -eq 0) { $hasSetup = $true }
$global:LASTEXITCODE = 0
$paired = Test-Path (Join-Path $env:USERPROFILE '.canopy\runner.json')
if ($RunSetup -and $hasSetup -and $paired) {
  Write-Host "==> canopy-runner setup --no-pair (registers the Scheduled Tasks)"
  & cmd /c "`"$Shim`" setup --no-pair"
  $setupExit = $LASTEXITCODE
  $global:LASTEXITCODE = 0
  if ($setupExit -ne 0) { Fail "setup --no-pair exited $setupExit" } else { Pass "setup --no-pair" }
  foreach ($t in $tasks) {
    & schtasks /Query /TN $t *> $null
    if ($LASTEXITCODE -eq 0) { Pass "task $t registered" } else { Fail "task $t not registered" }
    $global:LASTEXITCODE = 0
  }
} else {
  # `setup --no-pair` refuses an unpaired box (no runner.json), and a CI VM is never
  # paired — so the uninstaller's task removal is tested against stand-in tasks of
  # the same names, which a per-user schtasks can create without admin.
  if (-not $hasSetup) { Write-Host "SKIP  canopy-runner setup is not in this build" }
  elseif (-not $paired) { Write-Host "SKIP  setup --no-pair needs a paired box (no ~\.canopy\runner.json)" }
  else { Write-Host "SKIP  setup not run (pass -RunSetup on a throwaway machine)" }
  foreach ($t in $tasks) {
    & schtasks /Create /TN $t /TR "cmd /c exit 0" /SC ONCE /ST 23:59 /F *> $null
    if ($LASTEXITCODE -eq 0) { Pass "stand-in task $t created" } else { Fail "could not create stand-in task $t" }
    $global:LASTEXITCODE = 0
  }
}

Write-Host "==> uninstall (silent)"
$unins = Join-Path $App 'unins000.exe'
$p = Start-Process -FilePath $unins -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
# The uninstaller copies itself to %TEMP% and returns at once; wait for that copy.
for ($i = 0; $i -lt 60 -and (Test-Path (Join-Path $App 'versions')); $i++) { Start-Sleep -Seconds 1 }
Check "uninstall removed versions\ and current" {
  Assert (-not (Test-Path (Join-Path $App 'versions'))) "versions\ still there"
  Assert (-not (Test-Path $Current)) "current still there"
}
foreach ($t in $tasks) {
  & schtasks /Query /TN $t *> $null
  if ($LASTEXITCODE -ne 0) { Pass "task $t absent after uninstall" } else { Fail "task $t still registered" }
  $global:LASTEXITCODE = 0
}

Write-Host ""
if ($script:Fails -gt 0) { Write-Host "== $($script:Fails) check(s) FAILED"; exit 1 }
Write-Host "== all checks passed"
