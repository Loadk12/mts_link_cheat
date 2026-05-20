# Run the bot in the current desktop session so Playwright's browser window is visible.
# If the Windows service is running, stop it first to avoid two bots polling Telegram.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$service = Get-Service -Name "NTS-AutoJoin" -ErrorAction SilentlyContinue
if ($service -and $service.Status -eq "Running") {
    Write-Host "Stopping NTS-AutoJoin service for visible debug run..."
    Stop-Service -Name "NTS-AutoJoin"
}

if (Test-Path ".\venv\Scripts\python.exe") {
    $python = ".\venv\Scripts\python.exe"
} else {
    $python = "python"
}

Write-Host "Starting bot in visible debug mode. Keep this window open."
Write-Host ""
Write-Host "To verify the bot browser executable from another PowerShell window, run:"
Write-Host 'Get-CimInstance Win32_Process -Filter "name = ''chrome.exe''" | Select-Object ProcessId, ExecutablePath, CommandLine | Format-List'
Write-Host "The bot browser should be under AppData\Local\ms-playwright\chromium-...\chrome-win\chrome.exe."
Write-Host ""
& $python -m nts_autojoin.service_main
