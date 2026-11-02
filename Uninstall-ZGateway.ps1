#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = $PSScriptRoot,
    [switch]$ForceService
)
$ErrorActionPreference = "Stop"
$ServiceName = "ModelHarbor"
$LegacyServiceName = "ZGateway"
$WatchdogTask = "ModelHarborWatchdog"
function Fail([string]$Message) { throw "[ModelHarbor] $Message" }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run this script from an elevated PowerShell window."
}
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot -ErrorAction Stop).Path
$nssm = Join-Path $InstallRoot "tools\nssm-2.24\win64\nssm.exe"
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
foreach ($serviceName in @($ServiceName, $LegacyServiceName)) {
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if (-not $service) { continue }
    if (-not (Test-Path -LiteralPath $nssm -PathType Leaf)) { Fail "NSSM is missing; review the service before removing it manually." }
    $existingDirectory = (& $nssm get $serviceName AppDirectory 2>$null | Out-String).Trim().TrimEnd('\')
    if (-not $ForceService -and $existingDirectory -and ($existingDirectory -ine $InstallRoot.TrimEnd('\'))) {
        Fail "Service $serviceName points outside this package. Re-run with -ForceService only after reviewing it."
    }
    & $nssm stop $serviceName | Out-Null
    & $nssm remove $serviceName confirm | Out-Null
    Write-Host "Removed service $serviceName."
}
& schtasks.exe /Delete /TN $WatchdogTask /F 2>$null | Out-Null
& schtasks.exe /Delete /TN "ZGatewayWatchdog" /F 2>$null | Out-Null
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
foreach ($runName in @("ModelHarborTray", "ZGatewayTray")) {
    $trayValue = (Get-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue).$runName
    if ($trayValue -and $trayValue -like "*$InstallRoot*") {
        Remove-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue
    }
}
Write-Host "ModelHarbor service registration removed. Package files were left in place."
