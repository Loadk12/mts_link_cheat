# --- restore_power_from_backup.ps1 ---
# Восстанавливает из ..\power-backup:
# - исходный план питания (scheme_backup.pow)
# - состояние гибернации (on/off)
# Сам себя перезапускает с правами администратора.

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

$backupDir = Join-Path (Split-Path $PSScriptRoot -Parent) "power-backup"
$metaPath  = Join-Path $backupDir "backup.json"
if (-not (Test-Path $metaPath)) { throw "Backup not found: $metaPath" }
$meta = Get-Content $metaPath -Raw | ConvertFrom-Json

$pow   = $meta.SchemeBackupPath
$guid  = $meta.ActiveSchemeGuid
$name  = $meta.ActiveSchemeName
$hibOn = [bool]$meta.HibernateWasEnabled
if (-not (Test-Path $pow)) { throw "Scheme file not found: $pow" }

Write-Host "Import scheme $guid from $pow ..."
$imported = $false
try   { powercfg -import "$pow" $guid | Out-Null; $imported = $true }
catch {
  try { powercfg -import "$pow" | Out-Null; $imported = $true } catch {}
}

# Активируем по GUID; если не вышло — найдём по имени (RU/EN)
$activated = $false
try { powercfg -setactive $guid | Out-Null; $activated = $true }
catch {
  $list = (powercfg -list 2>&1) | Out-String
  if ($list -match 'GUID.*?:\s*([0-9A-Fa-f-]{36})\s*\(' + [regex]::Escape($name) + '\)') {
    $guid2 = $matches[1]
    try { powercfg -setactive $guid2 | Out-Null; $activated = $true } catch {}
  }
}
if (-not $activated) { Write-Warning "Could not activate original scheme." }

# Восстанавливаем гибернацию
if ($hibOn) { Write-Host "Enable hibernate (as before)"; powercfg /hibernate on | Out-Null }
else        { Write-Host "Keep hibernate off (as before)"; powercfg /hibernate off | Out-Null }

Write-Host "`nRestore complete."
