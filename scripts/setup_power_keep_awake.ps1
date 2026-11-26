# --- setup_power_keep_awake.ps1 ---
# Backs up the current power plan + hibernate state to ..\power-backup.
# Applies anti-sleep settings for stable auto-join work:
# - hibernate OFF
# - sleep on AC = Never
# - lid close on AC = Do nothing
#
# Optional switches:
# -IncludeBattery disables sleep and lid sleep on battery too.
# -KeepDisplayOn keeps the display on as well.
# -ForceBackup overwrites the existing backup.
#
# The script relaunches itself as administrator through UAC.

param(
  [switch]$IncludeBattery,
  [switch]$KeepDisplayOn,
  [switch]$ForceBackup
)

function Ensure-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $pri = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $pri.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($IncludeBattery) { $args += "-IncludeBattery" }
    if ($KeepDisplayOn) { $args += "-KeepDisplayOn" }
    if ($ForceBackup) { $args += "-ForceBackup" }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $args
    exit
  }
}

Ensure-Admin
$ErrorActionPreference = "Stop"

$backupDir = Join-Path (Split-Path $PSScriptRoot -Parent) "power-backup"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

function Get-ActiveScheme {
  $output = (powercfg /getactivescheme 2>&1) | Out-String
  if ($output -match 'GUID.*?:\s*([0-9A-Fa-f-]{36})\s*\((.+?)\)') {
    return [pscustomobject]@{ Guid = $matches[1]; Name = $matches[2] }
  }
  throw "Unexpected powercfg output: $output"
}

function Get-HibernateEnabled {
  $path = "HKLM:\SYSTEM\CurrentControlSet\Control\Power"
  $value = (Get-ItemProperty -Path $path -Name "HibernateEnabled" -ErrorAction SilentlyContinue).HibernateEnabled
  if ($null -eq $value) {
    $value = (Get-ItemProperty -Path $path -Name "HibernateEnabledDefault" -ErrorAction SilentlyContinue).HibernateEnabledDefault
  }
  if ($null -eq $value) {
    return [int](Test-Path "$env:SystemDrive\hiberfil.sys")
  }
  return [int]$value
}

$powPath = Join-Path $backupDir "scheme_backup.pow"
$metaPath = Join-Path $backupDir "backup.json"

if ((Test-Path $metaPath) -and (Test-Path $powPath) -and -not $ForceBackup) {
  Write-Host "Existing backup found: $backupDir"
  Write-Host "Keeping it unchanged. Use -ForceBackup to overwrite it."
} else {
  $activeScheme = Get-ActiveScheme
  Write-Host "Backup scheme: $($activeScheme.Guid) ($($activeScheme.Name)) -> $powPath"
  powercfg -export "$powPath" $activeScheme.Guid

  $hibernateEnabled = Get-HibernateEnabled
  $meta = @{
    ActiveSchemeGuid    = $activeScheme.Guid
    ActiveSchemeName    = $activeScheme.Name
    HibernateWasEnabled = [bool]($hibernateEnabled -eq 1)
    SchemeBackupPath    = $powPath
    BackupTime          = (Get-Date)
  }
  $meta | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $metaPath
  Write-Host "Meta saved: $metaPath"
}

Write-Host "Disable hibernate..."
powercfg /hibernate off | Out-Null

Write-Host "Sleep timeout (AC) -> Never..."
powercfg /change standby-timeout-ac 0 | Out-Null

if ($IncludeBattery) {
  Write-Host "Sleep timeout (battery) -> Never..."
  powercfg /change standby-timeout-dc 0 | Out-Null
}

if ($KeepDisplayOn) {
  Write-Host "Display timeout (AC) -> Never..."
  powercfg /change monitor-timeout-ac 0 | Out-Null

  if ($IncludeBattery) {
    Write-Host "Display timeout (battery) -> Never..."
    powercfg /change monitor-timeout-dc 0 | Out-Null
  }
}

try {
  Write-Host "Lid close (AC) -> Do nothing..."
  powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null

  if ($IncludeBattery) {
    Write-Host "Lid close (battery) -> Do nothing..."
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
  }

  powercfg /setactive SCHEME_CURRENT | Out-Null
} catch {
  Write-Warning "Cannot set lid action. Skipping."
}

Write-Host ""
Write-Host "Done. Backup in: $backupDir"
Write-Host "To restore, run: .\scripts\restore_power_from_backup.ps1"
