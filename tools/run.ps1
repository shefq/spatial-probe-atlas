[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [ValidateRange(10, 180)][int]$ReadyTimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-SpaRepositoryRoot
Import-SpaEnvironmentFile -Path (Join-Path $repoRoot ".env.local")

function Stop-RunFailure {
    param([string]$Message)
    Write-SpaStatus -Level FAILED -Message $Message
    Write-Host "Run doctor.bat for a complete, non-destructive diagnosis."
    exit 1
}

function Test-SetupHashes {
    param($Marker)
    if ($null -eq $Marker.hashes) { return $false }
    foreach ($property in $Marker.hashes.PSObject.Properties) {
        $path = Join-Path $repoRoot $property.Name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
        if ((Get-SpaFileHash -Path $path) -ne [string]$property.Value) { return $false }
    }
    return $true
}

function Test-CriticalModels {
    param([string]$DataRoot, [string]$EffectiveProfile)
    $manifestPath = Join-Path $repoRoot "models\manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $false }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        foreach ($asset in @($manifest.assets)) {
            $required = $true
            if ($asset.PSObject.Properties.Name -contains "required") { $required = [bool]$asset.required }
            $profiles = @($asset.profiles)
            if ($profiles.Count -gt 0 -and $profiles -notcontains $EffectiveProfile -and $profiles -notcontains "all") { continue }
            if (-not $required) { continue }
            if ([string]::IsNullOrWhiteSpace($asset.sha256)) { return $false }
            $filename = if ($asset.PSObject.Properties.Name -contains "filename") { $asset.filename } else { "$($asset.id).bin" }
            if ([IO.Path]::GetFileName($filename) -ne $filename) { return $false }
            $path = Join-Path (Join-Path $DataRoot "models") $filename
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
            if ((Get-SpaFileHash -Path $path) -ne $asset.sha256) { return $false }
        }
        return $true
    } catch { return $false }
}

$markerPath = Join-Path $repoRoot ".setup-complete.json"
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    Stop-RunFailure "Setup is incomplete. Run setup.bat first."
}
try { $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json } catch { Stop-RunFailure ".setup-complete.json is invalid. Run setup.bat again." }
if (-not (Test-SetupHashes -Marker $marker)) {
    Stop-RunFailure "Installed source or lock files changed after setup. Run setup.bat again."
}

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { Stop-RunFailure "The local Python environment is missing. Run setup.bat." }
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "frontend\dist\index.html") -PathType Leaf)) { Stop-RunFailure "The frontend production build is missing. Run setup.bat." }

try { $dataRoot = Get-SpaDataRoot -RepositoryRoot $repoRoot } catch { Stop-RunFailure $_.Exception.Message }
if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) { Stop-RunFailure "The configured data root is missing. Run setup.bat." }
$installedProfile = if ([string]::IsNullOrWhiteSpace([string]$marker.effective_compute_profile)) { "cpu" } else { [string]$marker.effective_compute_profile }
if (-not (Test-CriticalModels -DataRoot $dataRoot -EffectiveProfile $installedProfile)) { Stop-RunFailure "A critical model asset or checksum is invalid. Run setup.bat." }

$env:PYTHONPATH = Join-Path $repoRoot "backend\src"
$env:SPA_DATA_ROOT = $dataRoot
$migrationCheck = & $venvPython -m spatial_probe_atlas.migrations --check 2>&1
if ($LASTEXITCODE -ne 0) {
    Stop-RunFailure ("Database migrations are not ready: " + ($migrationCheck -join " "))
}

$instance = Get-SpaInstanceInfo -DataRoot $dataRoot
if ($null -ne $instance -and -not [string]::IsNullOrWhiteSpace($instance.Url) -and (Test-SpaReadyEndpoint -BaseUrl $instance.Url)) {
    Write-SpaStatus -Level SUCCESS -Message "Spatial Probe Atlas is already running at $($instance.Url)."
    if (-not $NoBrowser) { Start-Process $instance.Url | Out-Null }
    exit 0
}

$preferredPort = 8765
if (-not [string]::IsNullOrWhiteSpace($env:SPA_PORT)) {
    $parsedPort = 0
    if (-not [int]::TryParse($env:SPA_PORT, [ref]$parsedPort) -or $parsedPort -lt 1024 -or $parsedPort -gt 65525) {
        Stop-RunFailure "SPA_PORT must be an integer from 1024 through 65525."
    }
    $preferredPort = $parsedPort
}
try { $port = Get-SpaAvailablePort -PreferredPort $preferredPort -RangeSize 11 } catch { Stop-RunFailure $_.Exception.Message }

$env:PYTHONPATH = Join-Path $repoRoot "backend\src"
$env:SPA_HOST = "127.0.0.1"
$env:SPA_PORT = [string]$port
$env:SPA_DATA_ROOT = $dataRoot
if ([string]::IsNullOrWhiteSpace($env:SPA_LOG_LEVEL)) { $env:SPA_LOG_LEVEL = "INFO" }
if ([string]::IsNullOrWhiteSpace($env:SPA_COMPUTE_PROFILE)) { $env:SPA_COMPUTE_PROFILE = "auto" }
$env:SPA_BOOTSTRAP_TOKEN = New-SpaRunSecret

$baseUrl = "http://127.0.0.1:$port"
$escapedToken = [Uri]::EscapeDataString($env:SPA_BOOTSTRAP_TOKEN)
$bootstrapUrl = "$baseUrl/bootstrap?token=$escapedToken"
$appLog = Join-Path $dataRoot "logs\app.jsonl"
$jobLog = Join-Path $dataRoot "logs\jobs.jsonl"

Write-SpaStatus -Level INFO -Message "Starting Spatial Probe Atlas on loopback port $port..."
Write-Host "Data root: $dataRoot"
Write-Host "Compute preference: $($env:SPA_COMPUTE_PROFILE) (the backend reports the verified effective mode)"
Write-Host "App log: $appLog"
Write-Host "Job log: $jobLog"

$argumentList = @("-m", "spatial_probe_atlas.main")
$process = $null
try {
    $process = Start-Process -FilePath $venvPython -ArgumentList $argumentList -WorkingDirectory $repoRoot -NoNewWindow -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) { break }
        if (Test-SpaReadyEndpoint -BaseUrl $baseUrl -TimeoutSeconds 2) { $ready = $true; break }
        Start-Sleep -Milliseconds 400
    }
    if (-not $ready) {
        if ($process.HasExited) { throw "The backend exited before readiness (exit code $($process.ExitCode))." }
        throw "Readiness timed out after $ReadyTimeoutSeconds seconds."
    }

    Write-SpaStatus -Level SUCCESS -Message "Spatial Probe Atlas is ready at $baseUrl"
    Write-Host "Bootstrap URL: $bootstrapUrl"
    if (-not $NoBrowser) { Start-Process $bootstrapUrl | Out-Null }
    Write-Host "Press Ctrl+C to request graceful shutdown."
    Wait-Process -Id $process.Id
    exit $process.ExitCode
} catch {
    Write-SpaStatus -Level FAILED -Message $_.Exception.Message
    Write-Host "Run doctor.bat and inspect $appLog."
    exit 1
} finally {
    $env:SPA_BOOTSTRAP_TOKEN = $null
    if ($null -ne $process -and -not $process.HasExited) {
        Write-SpaStatus -Level INFO -Message "Waiting up to 15 seconds for the owned backend process to shut down..."
        try { Wait-Process -Id $process.Id -Timeout 15 -ErrorAction Stop } catch { }
        if (-not $process.HasExited) {
            Write-SpaStatus -Level WARNING -Message "Graceful shutdown timed out; terminating only process $($process.Id)."
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

