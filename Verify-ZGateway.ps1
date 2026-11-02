#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$InstallRoot = "",
    [string]$ApiKey = ""
)
$ErrorActionPreference = "Stop"
if (-not $InstallRoot) { $InstallRoot = $PSScriptRoot }
$BaseUrl = $BaseUrl.TrimEnd('/')
$failed = $false
function Check([string]$Name, [scriptblock]$Action) {
    try { & $Action; Write-Host "PASS $Name" }
    catch { $script:failed = $true; Write-Host "FAIL $Name - $($_.Exception.Message)" }
}
Check "ModelHarbor service" {
    $service = Get-Service -Name "ModelHarbor" -ErrorAction Stop
    if ($service.Status -ne "Running") { throw "status is $($service.Status)" }
}
Check "/healthz" {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/healthz" -TimeoutSec 5
    $healthPattern = '"ok"\s*:\s*true'
    if ($response.StatusCode -ne 200 -or $response.Content -notmatch $healthPattern) { throw "unexpected response" }
}
Check "dashboard static page" {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/static/index.html" -TimeoutSec 5
    if ($response.StatusCode -ne 200 -or $response.Content -notmatch 'ModelHarbor') { throw "unexpected response" }
}
if ($ApiKey) {
    Check "/v1/models with supplied key" {
        $headers = @{ Authorization = "Bearer $ApiKey" }
        $response = Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri "$BaseUrl/v1/models" -TimeoutSec 15
        if ($response.StatusCode -ne 200) { throw "HTTP $($response.StatusCode)" }
    }
} else { Write-Host "SKIP /v1/models authentication check - pass -ApiKey without printing it." }
Check "runtime station configuration" {
    $stationsPath = Join-Path (Resolve-Path -LiteralPath $InstallRoot).Path "data\stations.json"
    $json = Get-Content -Raw -LiteralPath $stationsPath | ConvertFrom-Json
    if (-not $json.stations -or $json.stations.Count -lt 1) { throw "no stations found" }
}
Check "persisted retry settings" {
    $settingsPath = Join-Path (Resolve-Path -LiteralPath $InstallRoot).Path "data\gateway-settings.json"
    $settings = Get-Content -Raw -LiteralPath $settingsPath | ConvertFrom-Json
    if (-not $settings.retry) { throw "retry settings missing" }
}
if ($failed) { exit 1 }
Write-Host "ModelHarbor verification completed."
