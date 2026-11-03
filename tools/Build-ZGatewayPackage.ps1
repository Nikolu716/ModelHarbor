#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$OutputPath = "",
    [switch]$Force
)
$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
if (-not $OutputPath) { $OutputPath = Join-Path $ProjectRoot ("ModelHarbor-{0}.zip" -f (Get-Date -Format "yyyyMMdd")) }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
if ((Test-Path -LiteralPath $OutputPath) -and -not $Force) { throw "Output already exists: $OutputPath. Use -Force only after reviewing it." }
$staging = Join-Path ([IO.Path]::GetTempPath()) ("ModelHarbor-package-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
try {
    $copyFiles = @(
        "gateway.py", "cli.py", "config.example.yaml", "requirements.txt", "README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
        "Install-ZGateway.ps1", "Uninstall-ZGateway.ps1", "Verify-ZGateway.ps1",
        "tools\Build-ZGatewayPackage.ps1", "tools\install.cmd", "tools\watchdog.bat", "tools\tray.py",
        "tools\zgateway-service.cmd", "tools\nssm-2.24\win64\nssm.exe",
        "tools\nssm-2.24\README.txt"
    )
    foreach ($relative in $copyFiles) {
        $source = Join-Path $ProjectRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Required source file is missing: $relative" }
        $destination = Join-Path $staging $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    # Build from reviewed, tracked app files only; ignore all local captures/data.
    $appFiles = @(& git -C $ProjectRoot ls-files -- app)
    if ($LASTEXITCODE -ne 0 -or $appFiles.Count -eq 0) { throw 'Build packages from a Git checkout with reviewed app files.' }
    foreach ($relative in $appFiles) {
        if ($relative -notmatch '^app/' -or $relative -match '(^|/)\.\.(/|$)') { throw 'Unexpected package path.' }
        $destination = Join-Path $staging $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $relative) -Destination $destination -Force
    }
    foreach ($relative in @('docs\native-protocols.md', 'docs\opencode-go.md', 'docs\dashboard-settings-and-statistics.md',
                            'docs\screenshots\overview.png', 'docs\screenshots\usage.png', 'docs\screenshots\requests.png', 'docs\screenshots\visual-tests.png')) {
        $destination = Join-Path $staging $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $relative) -Destination $destination -Force
    }
    $readme = @"
ModelHarbor Windows deployment package
==================================

The package includes the NSSM executable and its public-domain notice.
Runtime configuration and data are created on the target machine and are
intentionally not included.

Install from an elevated PowerShell window:
  Set-ExecutionPolicy -Scope Process Bypass
  .\Install-ZGateway.ps1

The installer creates .venv, installs requirements.txt, registers the
ModelHarbor Windows service, registers the five-minute watchdog, and starts the
service. Python 3.11+ and package-download access are required unless the
requirements are already available through a local package mirror.

Verify without exposing credentials:
  .\Verify-ZGateway.ps1
  .\Verify-ZGateway.ps1 -ApiKey '<gateway api key>'

The public package contains a configuration template only. Keep the copied
config.yaml and the entire data directory private, restrict the deployment
directory, and rotate keys if the package or the original HTTP dashboard was
exposed to an untrusted network.
"@
    [IO.File]::WriteAllText((Join-Path $staging "MIGRATION-README.txt"), $readme, [Text.UTF8Encoding]::new($false))
    $manifestLines = @()
    Get-ChildItem -LiteralPath $staging -File -Recurse | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($staging.Length + 1).Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $manifestLines += "$hash  $relative"
    }
    [IO.File]::WriteAllLines((Join-Path $staging "SHA256SUMS.txt"), $manifestLines, [Text.UTF8Encoding]::new($false))
    New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
    if (Test-Path -LiteralPath $OutputPath) { Remove-Item -LiteralPath $OutputPath -Force }
    Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $OutputPath -CompressionLevel Optimal
    $size = (Get-Item -LiteralPath $OutputPath).Length
    Write-Host "Created $OutputPath ($size bytes)."
} finally {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $cleanupPath = [IO.Path]::GetFullPath($staging)
    if (-not $cleanupPath.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $cleanupPath) -notlike 'ModelHarbor-package-*') {
        throw "Refusing cleanup outside the temporary package directory."
    }
    if (Test-Path -LiteralPath $cleanupPath) { Remove-Item -LiteralPath $cleanupPath -Recurse -Force }
}
