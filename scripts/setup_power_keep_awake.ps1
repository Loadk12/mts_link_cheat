# --- setup_power_keep_awake.ps1 ---
# Бэкап текущего плана питания + состояния гибернации -> ..\power-backup
# Применяет anti-sleep: hibernate OFF, standby AC=Never, lid close (AC)=Do nothing
# Сам себя перезапускает с правами администратора (UAC).

function Ensure-Admin {
  $id  = [Security.Principal.WindowsIdentity]::GetCurrent()
  $pri = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $pri.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
  }
}
Ensure-Admin
$ErrorActionPreference = "Stop"

# Папка для бэкапов (на уровень выше scripts): ..\power-backup
$backupDir = Join-Path (Split-Path $PSScriptRoot -Parent) "power-backup"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

function Get-ActiveScheme {
  $o = (powercfg /getactivescheme 2>&1) | Out-String
  # Совместимо с RU: "GUID схемы питания: ..." и EN: "Power Scheme GUID: ..."
  if ($o -match 'GUID.*?:\s*([0-9A-Fa-f-]{36})\s*\((.+?)\)') {
    return [pscustomobject]@{ Guid = $matches[1]; Name = $matches[2] }
  }
  throw "Unexpected output: $o"
}
function Get-HibernateEnabled {
  # 1 = ON, 0 = OFF (без парсинга локализованного текста)
  $path = "HKLM:\SYSTEM\CurrentControlSet\Control\Power"
  $v = (Get-ItemProperty -Path $path -Name "HibernateEnabled" -ErrorAction SilentlyContinue).HibernateEnabled
  if ($null -eq $v) { $v = (Get-ItemProperty -Path $path -Name "HibernateEnabledDefault" -ErrorAction SilentlyContinue).HibernateEnabledDefault }
  if ($null -eq $v) { return [int](Test-Path "$env:SystemDrive\hiberfil.sys") }
  return [int]$v
}

# === 1) Бэкап активного плана и флага гибернации ===
$act = Get-ActiveScheme
$pow = Join-Path $backupDir "scheme_backup.pow"
Write-Host "Backup scheme: $($act.Guid) ($($act.Name)) -> $pow"
powercfg -export "$pow" $act.Guid

$hib = Get-HibernateEnabled
$meta = @{
  ActiveSchemeGuid    = $act.Guid
  ActiveSchemeName    = $act.Name
  HibernateWasEnabled = [bool]($hib -eq 1)
  SchemeBackupPath    = $pow
  BackupTime          = (Get-Date)
}
$metaPath = Join-Path $backupDir "backup.json"
$meta | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $metaPath
Write-Host "Meta saved: $metaPath"

# === 2) Применение anti-sleep ===
Write-Host "Disable hibernate..."
powercfg /hibernate off | Out-Null

Write-Host "Standby timeout (AC) -> Never..."
powercfg /change standby-timeout-ac 0 | Out-Null

# На текущем плане: крышка (AC) -> Do nothing
# SUB_BUTTONS = 4f971e89-eebd-4455-a8de-9e59040e7347
# LIDACTION   = 5ca83367-6e45-459f-a27b-476b1d01c936
try {
  Write-Host "Lid close (AC) -> Do nothing..."
  powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
  powercfg /setactive SCHEME_CURRENT | Out-Null
} catch {
  Write-Warning "Cannot set lid action (maybe desktop). Skipping."
}

Write-Host "`nDone. Backup in: $backupDir"
Write-Host "To restore run restore_power_from_backup.ps1"
