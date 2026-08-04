[CmdletBinding()]
param(
    [switch]$E2E,
    [switch]$Hardware,
    [switch]$CpuMapping,
    [switch]$SkipFrontendBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\tools\common.ps1")

$repoRoot = Get-SpaRepositoryRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$node = Get-SpaNode -RepositoryRoot $repoRoot
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Run setup.bat before verification." }
if ($null -eq $node -or -not (Test-Path -LiteralPath $node.Npm -PathType Leaf)) { throw "Node 22/npm is unavailable; run setup.bat." }
$env:PYTHONPATH = Join-Path $repoRoot "backend\src"
$env:PYTHONDONTWRITEBYTECODE = "1"
$failed = $false

function Invoke-Verification {
    param([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory = $repoRoot)
    Write-Host ""
    Write-Host "== $Name =="
    Push-Location $WorkingDirectory
    try {
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[FAILED] $Name"
            $script:failed = $true
        } else {
            Write-Host "[PASS] $Name"
        }
    } finally { Pop-Location }
}

Invoke-Verification -Name "Locked Python consistency" -Executable $python -Arguments @("-m", "pip", "check")
Invoke-Verification -Name "Database migration compatibility" -Executable $python -Arguments @("-m", "spatial_probe_atlas.migrations", "--check")
Invoke-Verification -Name "Schemas and OpenAPI contract" -Executable $python -Arguments @((Join-Path $repoRoot "scripts\validate_contracts.py"))
Invoke-Verification -Name "Backend unit/API/integration/replay" -Executable $python -Arguments @("-m", "pytest", "tests", "-m", "not hardware and not slow", "-p", "no:cacheprovider")
Invoke-Verification -Name "Deterministic replay smoke" -Executable $python -Arguments @((Join-Path $repoRoot "scripts\replay_smoke.py"))
if ($CpuMapping) {
    Invoke-Verification -Name "CPU mapping smoke" -Executable $python -Arguments @((Join-Path $repoRoot "scripts\cpu_mapping_smoke.py"))
}
Invoke-Verification -Name "Frontend unit tests" -Executable $node.Npm -Arguments @("test") -WorkingDirectory (Join-Path $repoRoot "frontend")
if (-not $SkipFrontendBuild) {
    Invoke-Verification -Name "Frontend production build" -Executable $node.Npm -Arguments @("run", "build") -WorkingDirectory (Join-Path $repoRoot "frontend")
}
if ($E2E) {
    Invoke-Verification -Name "Production browser E2E" -Executable "powershell.exe" -Arguments @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $repoRoot "scripts\run_e2e.ps1"))
}
if ($Hardware) {
    if ($env:SPA_RUN_HARDWARE_TESTS -ne "1") { throw "Set SPA_RUN_HARDWARE_TESTS=1 to confirm explicit hardware-test consent." }
    Invoke-Verification -Name "Opt-in hardware tests" -Executable $python -Arguments @("-m", "pytest", "tests\hardware", "-m", "hardware", "-p", "no:cacheprovider")
}

Write-Host ""
if ($failed) { Write-Host "Verification failed."; exit 1 }
Write-Host "All selected verification passed."
exit 0
