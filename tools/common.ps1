Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-SpaRepositoryRoot {
    return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}

function Import-SpaEnvironmentFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
        $parts = $line.Split("=", 2)
        $name = $parts[0].Trim()
        if ($name -notmatch '^SPA_[A-Z0-9_]+$') { continue }
        if (Test-Path "Env:$name") { continue }
        $value = $parts[1].Trim().Trim('"').Trim("'")
        $value = [Environment]::ExpandEnvironmentVariables($value)
        Set-Item -Path "Env:$name" -Value $value
    }
}

function Get-SpaDataRoot {
    param([string]$RepositoryRoot = (Get-SpaRepositoryRoot))

    Import-SpaEnvironmentFile -Path (Join-Path $RepositoryRoot ".env.local")
    $configured = [Environment]::GetEnvironmentVariable("SPA_DATA_ROOT")
    if ([string]::IsNullOrWhiteSpace($configured)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw "LOCALAPPDATA is unavailable and SPA_DATA_ROOT is not configured."
        }
        $configured = Join-Path $env:LOCALAPPDATA "SpatialProbeAtlas"
    }
    return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($configured))
}

function Get-SpaRuntimeManifest {
    param([string]$RepositoryRoot = (Get-SpaRepositoryRoot))
    $path = Join-Path $RepositoryRoot "tools\runtime-manifest.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing runtime manifest: $path"
    }
    return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Get-SpaPython {
    param([string]$RepositoryRoot = (Get-SpaRepositoryRoot), [switch]$PreferVenv)

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($PreferVenv) { $candidates.Add((Join-Path $RepositoryRoot ".venv\Scripts\python.exe")) }
    $candidates.Add((Join-Path $RepositoryRoot ".runtime\python\python.exe"))
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) { $candidates.Add($pythonCommand.Source) }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $version = (& $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null).Trim()
            if ($version -match '^3\.11\.') {
                return [pscustomobject]@{ Path = [IO.Path]::GetFullPath($candidate); Version = $version }
            }
        } catch { }
    }
    return $null
}

function Get-SpaNode {
    param([string]$RepositoryRoot = (Get-SpaRepositoryRoot))

    $candidates = New-Object System.Collections.Generic.List[string]
    $candidates.Add((Join-Path $RepositoryRoot ".runtime\node\node.exe"))
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($null -ne $nodeCommand) { $candidates.Add($nodeCommand.Source) }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $version = (& $candidate --version 2>$null).Trim().TrimStart("v")
            if ($version -match '^22\.') {
                $npm = Join-Path (Split-Path -Parent $candidate) "npm.cmd"
                if (-not (Test-Path -LiteralPath $npm -PathType Leaf)) {
                    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
                    if ($null -ne $npmCommand) { $npm = $npmCommand.Source }
                }
                return [pscustomobject]@{ Path = [IO.Path]::GetFullPath($candidate); Npm = $npm; Version = $version }
            }
        } catch { }
    }
    return $null
}

function Get-SpaFileHash {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-SpaAtomicWrite {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $createdDirectory = $false
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
        $createdDirectory = $true
    }
    $id = [Guid]::NewGuid().ToString("N")
    $source = Join-Path $Directory ".spa-write-$id.tmp"
    $target = Join-Path $Directory ".spa-write-$id.ok"
    try {
        [IO.File]::WriteAllText($source, "spatial-probe-atlas")
        Move-Item -LiteralPath $source -Destination $target
        return (Test-Path -LiteralPath $target -PathType Leaf)
    } finally {
        if (Test-Path -LiteralPath $source) { Remove-Item -LiteralPath $source -Force }
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        if ($createdDirectory -and (Test-Path -LiteralPath $Directory)) {
            $remaining = Get-ChildItem -LiteralPath $Directory -Force -ErrorAction SilentlyContinue
            if ($null -eq $remaining) { Remove-Item -LiteralPath $Directory -Force }
        }
    }
}

function Test-SpaPortAvailable {
    param([Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port)
    $listener = $null
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) { $listener.Stop() }
    }
}

function Get-SpaAvailablePort {
    param([int]$PreferredPort = 8765, [int]$RangeSize = 11)
    for ($port = $PreferredPort; $port -lt ($PreferredPort + $RangeSize); $port++) {
        if (Test-SpaPortAvailable -Port $port) { return $port }
    }
    throw "No available loopback port in range $PreferredPort-$($PreferredPort + $RangeSize - 1)."
}

function Test-SpaReadyEndpoint {
    param([Parameter(Mandatory = $true)][string]$BaseUrl, [int]$TimeoutSeconds = 2)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$($BaseUrl.TrimEnd('/'))/api/v1/health/ready" -TimeoutSec $TimeoutSeconds
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-SpaInstanceInfo {
    param([Parameter(Mandatory = $true)][string]$DataRoot)
    $path = Join-Path $DataRoot "instance.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        $value = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        $url = $value.url
        if ([string]::IsNullOrWhiteSpace($url) -and $null -ne $value.port) {
            $url = "http://127.0.0.1:$($value.port)"
        }
        return [pscustomobject]@{ Path = $path; Value = $value; Url = $url }
    } catch {
        return [pscustomobject]@{ Path = $path; Value = $null; Url = $null }
    }
}

function New-SpaRunSecret {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return ([Convert]::ToBase64String($bytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Write-SpaStatus {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("INFO", "PASS", "SUCCESS", "WARNING", "FAILED")][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message,
        [string]$LogPath
    )
    $line = "[{0}] {1}" -f $Level, $Message
    Write-Host $line
    if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
        Add-Content -LiteralPath $LogPath -Value ("{0:o} {1}" -f [DateTime]::UtcNow, $line) -Encoding UTF8
    }
}
