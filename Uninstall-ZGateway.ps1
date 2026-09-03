#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = $PSScriptRoot,
    [switch]$ForceService
)
$ErrorActionPreference = "Stop"
$ServiceName = "ZGateway"
$WatchdogTask = "ZGatewayWatchdog"
function Fail([string]$Message) { throw "[ZGateway] $Message" }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this script from an elevated PowerShell window."
}
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot -ErrorAction Stop).Path
$nssm = Join-Path $InstallRoot "tools\nssm-2.24\win64\nssm.exe"
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    if (-not (Test-Path -LiteralPath $nssm -PathType Leaf)) { Fail "NSSM is missing; review the service before removing it manually." }
    $existingApp = (& $nssm get $ServiceName Application 2>$null | Out-String).Trim()
    if (-not $ForceService -and $existingApp -and ($existingApp -notlike "$InstallRoot*")) {
        Fail "Service $ServiceName points outside this package. Re-run with -ForceService only after reviewing it."
    }
    & $nssm stop $ServiceName | Out-Null
    & $nssm remove $ServiceName confirm | Out-Null
    Write-Host "Removed service $ServiceName."
}
& schtasks.exe /Delete /TN $WatchdogTask /F 2>$null | Out-Null
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$trayValue = (Get-ItemProperty -Path $runKey -Name "ZGatewayTray" -ErrorAction SilentlyContinue).ZGatewayTray
if ($trayValue -and $trayValue -like "*$InstallRoot*") {
    Remove-ItemProperty -Path $runKey -Name "ZGatewayTray" -ErrorAction SilentlyContinue
}
Write-Host "ZGateway service registration removed. Package files were left in place."
