#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [string]$PythonPath = "",
    [switch]$SkipDependencies,
    [switch]$ForceService,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$ServiceName = "ZGateway"
$WatchdogTask = "ZGatewayWatchdog"
if (-not $InstallRoot) { $InstallRoot = $PSScriptRoot }

function Fail([string]$Message) { throw "[ZGateway] $Message" }

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail "Run this script from an elevated PowerShell window."
    }
}

function Get-PythonVersion([string]$Path, [object[]]$PrefixArgs = @()) {
    try {
        $text = (& $Path @PrefixArgs --version 2>&1 | Out-String).Trim()
        if ($text -match "Python\s+(\d+)\.(\d+)") {
            return [version]::new([int]$Matches[1], [int]$Matches[2])
        }
    } catch { return $null }
    return $null
}

function Find-Python {
    if ($PythonPath) {
        $candidate = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
        $version = Get-PythonVersion $candidate
        if ($version -and $version -ge [version]::new(3, 10)) {
            return @{ Path = $candidate; PrefixArgs = @() }
        }
        Fail "PythonPath must point to Python 3.10 or newer."
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @("-3.12", "-3.11", "-3.10")) {
            $version = Get-PythonVersion $launcher.Source @($selector)
            if ($version -and $version -ge [version]::new(3, 10)) {
                return @{ Path = $launcher.Source; PrefixArgs = @($selector) }
            }
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $version = Get-PythonVersion $python.Source
        if ($version -and $version -ge [version]::new(3, 10)) {
            return @{ Path = $python.Source; PrefixArgs = @() }
        }
    }
    Fail "Python 3.10 or newer was not found. Install Python or pass -PythonPath."
}

function Invoke-Python([string]$Path, [object[]]$Arguments) {
    & $Path @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "Python command failed with exit code $LASTEXITCODE." }
}

Assert-Administrator
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot -ErrorAction Stop).Path
Set-Location -LiteralPath $InstallRoot
$configPath = Join-Path $InstallRoot "config.yaml"
$exampleConfigPath = Join-Path $InstallRoot "config.example.yaml"
$createdConfig = $false
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $exampleConfigPath -PathType Leaf)) {
        Fail "config.yaml is missing and config.example.yaml was not found."
    }
    Copy-Item -LiteralPath $exampleConfigPath -Destination $configPath
    $createdConfig = $true
    Write-Warning "Created config.yaml from config.example.yaml. Edit upstream URLs and keys before starting production traffic."
}

$required = @(
    "gateway.py", "config.yaml", "config.example.yaml", "requirements.txt", "app\config.py",
    "app\web\static\index.html", "tools\nssm-2.24\win64\nssm.exe",
    "tools\watchdog.bat"
)
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $relative) -PathType Leaf)) {
        Fail "Required package file is missing: $relative"
    }
}

$configPath = Join-Path $InstallRoot "config.yaml"
$dataPath = Join-Path $InstallRoot "data"
$nssm = Join-Path $InstallRoot "tools\nssm-2.24\win64\nssm.exe"
$gateway = Join-Path $InstallRoot "gateway.py"
$watchdog = Join-Path $InstallRoot "tools\watchdog.bat"
$stdoutLog = Join-Path $dataPath "service.log"
$stderrLog = Join-Path $dataPath "service.err.log"
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null

& icacls.exe $InstallRoot /inheritance:r | Out-Null
& icacls.exe $InstallRoot /grant:r "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Could not apply deployment directory permissions." }

$venvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$venvPythonw = Join-Path $InstallRoot ".venv\Scripts\pythonw.exe"
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $runtimeVersion = Get-PythonVersion $venvPython
    if (-not $runtimeVersion -or $runtimeVersion -lt [version]::new(3, 10)) {
        Remove-Item -LiteralPath (Join-Path $InstallRoot ".venv") -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $bootstrap = Find-Python
    Write-Host "Creating isolated Python environment with $($bootstrap.Path)."
    Invoke-Python $bootstrap.Path (@($bootstrap.PrefixArgs) + @("-m", "venv", (Join-Path $InstallRoot ".venv")))
}
$runtimeVersion = Get-PythonVersion $venvPython
if (-not $runtimeVersion -or $runtimeVersion -lt [version]::new(3, 10)) {
    Fail "The deployment Python environment is not Python 3.10 or newer."
}
if (-not $SkipDependencies) {
    Write-Host "Installing gateway dependencies. Internet access or a configured package mirror is required."
    Invoke-Python $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $InstallRoot "requirements.txt"))
}
Invoke-Python $venvPython @("-c", "from app.config import load_config; c=load_config(r'$configPath'); print('validated ' + str(len(c.stations)) + ' configured seed stations')")
if ($createdConfig) {
    Fail "Edit config.yaml with real upstream settings, then rerun the installer. No service was registered."
}

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    $existingApp = (& $nssm get $ServiceName Application 2>$null | Out-String).Trim()
    if (-not $ForceService -and $existingApp -and ($existingApp -notlike "$InstallRoot*")) {
        Fail "Service $ServiceName already points outside this package. Re-run with -ForceService only after reviewing it."
    }
    if ($existingService.Status -ne "Stopped") {
        & $nssm stop $ServiceName | Out-Null
        Start-Sleep -Seconds 2
    }
    Write-Host "Updating existing Windows service $ServiceName."
    & $nssm set $ServiceName Application $venvPython | Out-Null
    & $nssm set $ServiceName AppParameters "`"$gateway`" --config `"$configPath`"" | Out-Null
} else {
    Write-Host "Registering Windows service $ServiceName."
    & $nssm install $ServiceName $venvPython "`"$gateway`" --config `"$configPath`"" | Out-Null
}
& $nssm set $ServiceName AppDirectory $InstallRoot | Out-Null
& $nssm set $ServiceName AppStdout $stdoutLog | Out-Null
& $nssm set $ServiceName AppStderr $stderrLog | Out-Null
& $nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $nssm set $ServiceName AppExit Default Restart | Out-Null
& $nssm set $ServiceName AppRestartDelay 5000 | Out-Null
& $nssm set $ServiceName Description "ZGateway OpenAI-compatible multi-upstream gateway" | Out-Null

Write-Host "Registering five-minute watchdog task."
& schtasks.exe /Create /TN $WatchdogTask /TR ('"' + $watchdog + '"') /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST /F | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Could not register the watchdog task." }

$tray = Join-Path $InstallRoot "tools\tray.py"
if ((Test-Path -LiteralPath $tray -PathType Leaf) -and (Test-Path -LiteralPath $venvPythonw -PathType Leaf)) {
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name "ZGatewayTray" -Value "`"$venvPythonw`" `"$tray`""
}

if (-not $NoStart) {
    Write-Host "Starting $ServiceName."
    & $nssm start $ServiceName | Out-Null
    $healthy = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 3
            if ($response.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
    }
    if (-not $healthy) { Fail "Service did not answer /healthz within 30 seconds. Check data\service.err.log." }
}
Write-Host "ZGateway installation completed at $InstallRoot."
Write-Host "Dashboard: http://127.0.0.1:8000/static/index.html"
