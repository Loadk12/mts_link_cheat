# Установка сервиса через NSSM. Запускай от имени администратора.
param(
  [string]$ServiceName = "NTS-AutoJoin",
  [string]$PythonPath = ".\venv\Scripts\python.exe",
  [string]$ModuleEntry = "nts_autojoin.service_main"
)
$ErrorActionPreference = "Stop"
$nssm = (Get-Command nssm -ErrorAction SilentlyContinue)
if (-not $nssm) {
  $localNssm = Join-Path (Get-Location) "scripts\nssm.exe"
  if (Test-Path $localNssm) { $nssm = $localNssm } else { Write-Error "nssm.exe не найден. Положи в scripts\\nssm.exe или добавь в PATH." }
} else { $nssm = $nssm.Source }
$workdir = (Get-Location).Path
$python = Resolve-Path $PythonPath
& $nssm install $ServiceName $python "$workdir\ -m $ModuleEntry"
& $nssm set $ServiceName AppDirectory $workdir
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout "$workdir\logs\service_stdout.log"
& $nssm set $ServiceName AppStderr "$workdir\logs\service_stderr.log"
& $nssm set $ServiceName AppRestartDelay 5000
& $nssm start $ServiceName
Write-Host "Сервис $ServiceName установлен и запущен."
