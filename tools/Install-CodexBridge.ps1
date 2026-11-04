param(
    [Parameter(Mandatory = $true)][string]$SourcePath,
    [string]$ServiceName = 'ModelHarbor'
)
$ErrorActionPreference = 'Stop'

# Run from an administrator PowerShell. No user login files are copied.
$source = Get-Item -LiteralPath $SourcePath
if ($source.Extension -ne '.exe' -or $source.PSIsContainer) { throw 'SourcePath must be codex.exe.' }
$signature = Get-AuthenticodeSignature -LiteralPath $source.FullName
$publisher = if ($signature.SignerCertificate) {
    $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
} else { '' }
if ($signature.Status -ne 'Valid' -or $publisher -notin @('OpenAI', 'OpenAI OpCo, LLC')) {
    throw 'The executable must have a valid OpenAI Authenticode signature.'
}

function Protect-BridgePath([string]$Path, [bool]$Directory) {
    $acl = if ($Directory) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544'))
    $inheritance = if ($Directory) { [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier $sid
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', $inheritance, 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$programFiles = [Environment]::GetFolderPath('ProgramFiles')
$bridgeRoot = Join-Path $programFiles 'ModelHarbor'
$bridgeDirectory = Join-Path $bridgeRoot 'Codex'
foreach ($directory in @($bridgeRoot, $bridgeDirectory)) {
    if (Test-Path -LiteralPath $directory) {
        if ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'The installation directory must not be a symbolic link or junction.'
        }
    } else { New-Item -ItemType Directory -Path $directory | Out-Null }
    Protect-BridgePath $directory $true
}
$destination = Join-Path $bridgeDirectory 'codex.exe'
if (Test-Path -LiteralPath $destination) {
    if ((Get-Item -LiteralPath $destination).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'The destination must not be a symbolic link.'
    }
}
Copy-Item -LiteralPath $source.FullName -Destination $destination -Force
Protect-BridgePath $destination $false
if ((Get-FileHash -LiteralPath $source.FullName).Hash -ne (Get-FileHash -LiteralPath $destination).Hash) {
    throw 'The copied executable did not match its source.'
}
$installedSignature = Get-AuthenticodeSignature -LiteralPath $destination
$installedPublisher = if ($installedSignature.SignerCertificate) {
    $installedSignature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
} else { '' }
if ($installedSignature.Status -ne 'Valid' -or $installedPublisher -notin @('OpenAI', 'OpenAI OpCo, LLC')) {
    throw 'The installed copy did not retain a valid OpenAI signature.'
}
$serviceKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\' + $ServiceName + '\Parameters'
if (Test-Path -LiteralPath $serviceKey) {
    $existing = @((Get-ItemProperty -LiteralPath $serviceKey -Name AppEnvironmentExtra -ErrorAction SilentlyContinue).AppEnvironmentExtra)
    $updated = @($existing | Where-Object { $_ -and $_ -notlike 'MODELHARBOR_CODEX_PATH=*' })
    $updated += 'MODELHARBOR_CODEX_PATH=' + $destination
    New-ItemProperty -LiteralPath $serviceKey -Name AppEnvironmentExtra -PropertyType MultiString -Value $updated -Force | Out-Null
}
Write-Output ('Installed trusted Codex CLI: ' + $destination)
Write-Output 'Restart ModelHarbor to apply the service environment. This script does not restart it.'
