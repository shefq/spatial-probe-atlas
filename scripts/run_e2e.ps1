[CmdletBinding()]
param([switch]$Headed)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\tools\common.ps1")

$repoRoot = Get-SpaRepositoryRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$playwright = Join-Path $repoRoot "tests\e2e\node_modules\.bin\playwright.cmd"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Run setup.bat first; .venv is missing." }
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "frontend\dist\index.html") -PathType Leaf)) { throw "Run setup.bat first; frontend/dist is missing." }
if (-not (Test-Path -LiteralPath $playwright -PathType Leaf)) { throw "Run npm.cmd --prefix tests\e2e ci and install the local Chromium browser first." }

$testCases = @(
    "one-time bootstrap establishes the local run session",
    "project creation reaches Camera Setup through the production UI",
    "invalid bootstrap credentials do not grant a run session",
    "replay atlas reaches review and a checksummed export"
)
$failed = $false
$previous = @{
    PYTHONPATH = $env:PYTHONPATH
    SPA_DATA_ROOT = $env:SPA_DATA_ROOT
    SPA_HOST = $env:SPA_HOST
    SPA_PORT = $env:SPA_PORT
    SPA_BOOTSTRAP_TOKEN = $env:SPA_BOOTSTRAP_TOKEN
    SPA_E2E_BASE_URL = $env:SPA_E2E_BASE_URL
    SPA_E2E_BOOTSTRAP_TOKEN = $env:SPA_E2E_BOOTSTRAP_TOKEN
    SPA_COMPUTE_PROFILE = $env:SPA_COMPUTE_PROFILE
}

try {
    foreach ($testCase in $testCases) {
        $port = Get-SpaAvailablePort -PreferredPort 9876 -RangeSize 30
        $id = [Guid]::NewGuid().ToString("N")
        $testRoot = Join-Path ([IO.Path]::GetTempPath()) "SpatialProbeAtlas-E2E-$id"
        $expectedParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + "\"
        $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
        if (-not $resolvedTestRoot.StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase) -or -not ([IO.Path]::GetFileName($resolvedTestRoot)).StartsWith("SpatialProbeAtlas-E2E-")) {
            throw "Refusing unsafe E2E data root: $resolvedTestRoot"
        }
        New-Item -ItemType Directory -Path $resolvedTestRoot | Out-Null
        $token = "e2e-$id"
        $env:PYTHONPATH = Join-Path $repoRoot "backend\src"
        $env:SPA_DATA_ROOT = $resolvedTestRoot
        $env:SPA_HOST = "127.0.0.1"
        $env:SPA_PORT = [string]$port
        $env:SPA_BOOTSTRAP_TOKEN = $token
        $env:SPA_E2E_BOOTSTRAP_TOKEN = $token
        $env:SPA_E2E_BASE_URL = "http://127.0.0.1:$port"
        $env:SPA_COMPUTE_PROFILE = "cpu"
        & $python -m spatial_probe_atlas.migrations
        if ($LASTEXITCODE -ne 0) { throw "E2E migration failed." }
        $server = $null
        try {
            $server = Start-Process -FilePath $python -ArgumentList @("-m", "spatial_probe_atlas.main") -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru
            $ready = $false
            $deadline = [DateTime]::UtcNow.AddSeconds(30)
            while ([DateTime]::UtcNow -lt $deadline) {
                if ($server.HasExited) { break }
                if (Test-SpaReadyEndpoint -BaseUrl $env:SPA_E2E_BASE_URL -TimeoutSeconds 2) { $ready = $true; break }
                Start-Sleep -Milliseconds 250
            }
            if (-not $ready) { throw "E2E server did not become ready for '$testCase'." }
            $arguments = @("test", "application.spec.ts", "--grep", $testCase)
            if ($Headed) { $arguments += "--headed" }
            & $playwright @arguments
            if ($LASTEXITCODE -ne 0) { $failed = $true }
        } finally {
            if ($null -ne $server -and -not $server.HasExited) {
                Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
                try { Wait-Process -Id $server.Id -Timeout 10 -ErrorAction SilentlyContinue } catch { }
            }
            if (Test-Path -LiteralPath $resolvedTestRoot) { Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force }
        }
    }
} finally {
    foreach ($name in $previous.Keys) {
        if ($null -eq $previous[$name]) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$name" -Value $previous[$name]
        }
    }
}
if ($failed) { exit 1 }
exit 0

