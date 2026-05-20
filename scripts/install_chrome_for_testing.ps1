param(
    [string]$Channel = "Stable",
    [string]$Version = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$installRoot = Join-Path $root "browsers\chrome-for-testing"
$chromeExe = Join-Path $installRoot "chrome-win64\chrome.exe"
$zipPath = Join-Path $env:TEMP ("chrome-for-testing-win64-{0}.zip" -f $PID)

if ((Test-Path $chromeExe) -and -not $Force) {
    Write-Host "Chrome for Testing already installed:"
    Write-Host $chromeExe
    exit 0
}

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null

if ($Version) {
    $downloadUrl = "https://storage.googleapis.com/chrome-for-testing-public/$Version/win64/chrome-win64.zip"
} else {
    $metadataUrl = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
    Write-Host "Fetching Chrome for Testing metadata..."
    $metadata = Invoke-RestMethod -Uri $metadataUrl
    $channelData = $metadata.channels.$Channel
    if (-not $channelData) {
        throw "Unknown channel '$Channel'. Use Stable, Beta, Dev, or Canary."
    }
    $download = $channelData.downloads.chrome | Where-Object { $_.platform -eq "win64" } | Select-Object -First 1
    if (-not $download) {
        throw "No win64 Chrome for Testing download found for channel '$Channel'."
    }
    $Version = $channelData.version
    $downloadUrl = $download.url
}

Write-Host "Downloading Chrome for Testing $Version ($Channel)..."
Write-Host $downloadUrl
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    & $curl.Source -L --fail --retry 3 --output $zipPath $downloadUrl
    if ($LASTEXITCODE -ne 0) {
        throw "curl.exe failed with exit code $LASTEXITCODE"
    }
} else {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath
}
if (-not (Test-Path $zipPath) -or (Get-Item $zipPath).Length -lt 1000000) {
    throw "Chrome for Testing download failed or produced an unexpectedly small archive: $zipPath"
}

$extractRoot = Join-Path $env:TEMP "chrome-for-testing-extract"
if (Test-Path $extractRoot) {
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null

Write-Host "Extracting..."
Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force

$extractedChromeDir = Join-Path $extractRoot "chrome-win64"
if (-not (Test-Path (Join-Path $extractedChromeDir "chrome.exe"))) {
    throw "Downloaded archive did not contain chrome-win64\chrome.exe."
}

$targetChromeDir = Join-Path $installRoot "chrome-win64"
if (Test-Path $targetChromeDir) {
    Remove-Item -LiteralPath $targetChromeDir -Recurse -Force
}
Move-Item -LiteralPath $extractedChromeDir -Destination $targetChromeDir

Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Chrome for Testing installed:"
Write-Host $chromeExe
Write-Host ""
Write-Host "Use this in config/schedule.yaml:"
Write-Host "chromium:"
Write-Host "  executable_path: $chromeExe"
