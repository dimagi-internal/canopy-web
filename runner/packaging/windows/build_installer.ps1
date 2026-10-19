<#
.SYNOPSIS
  Compile Canopy-Runner-Setup-windows-x86_64.exe from a windows-x86_64 payload.

    pwsh runner/packaging/windows/build_installer.ps1 -Payload build\payload-windows-x86_64 -OutDir build\dist

  Uses Inno Setup 6's ISCC.exe: $env:ISCC if set, else the usual install
  locations, else `choco install innosetup` (GitHub's windows images have
  Chocolatey). Signing is NOT done here — the workflow signs the payload's
  executables before this runs and the installer after, only when its secrets exist.
#>
param(
  [Parameter(Mandatory = $true)][string]$Payload,
  [Parameter(Mandatory = $true)][string]$OutDir
)
$ErrorActionPreference = 'Stop'

$Payload = (Resolve-Path $Payload).Path
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$release = Get-Content (Join-Path $Payload 'release.json') -Raw | ConvertFrom-Json
if ($release.platform -ne 'windows-x86_64') { throw "payload is $($release.platform), not windows-x86_64" }
$sha12 = $release.sha.Substring(0, 12)
$appVersion = "1.0.$($release.committed_at)"

function Find-Iscc {
  if ($env:ISCC -and (Test-Path $env:ISCC)) { return $env:ISCC }
  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
  )
  foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
  $onPath = Get-Command iscc.exe -ErrorAction SilentlyContinue
  if ($onPath) { return $onPath.Source }
  return $null
}

$iscc = Find-Iscc
if (-not $iscc) {
  Write-Host "==> Inno Setup not found - installing it with Chocolatey"
  choco install innosetup --no-progress -y | Out-Host
  $iscc = Find-Iscc
  if (-not $iscc) { throw "ISCC.exe not found even after choco install innosetup" }
}
Write-Host "==> $iscc"

$iss = Join-Path $PSScriptRoot 'canopy-runner.iss'
& $iscc "/DPayloadDir=$Payload" "/DSha12=$sha12" "/DAppVersion=$appVersion" "/DOutputDir=$OutDir" /Q $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

$exe = Join-Path $OutDir 'Canopy-Runner-Setup-windows-x86_64.exe'
if (-not (Test-Path $exe)) { throw "ISCC reported success but $exe is missing" }
$size = (Get-Item $exe).Length
Write-Host ("==> {0} ({1:N1} MB)" -f $exe, ($size / 1MB))
