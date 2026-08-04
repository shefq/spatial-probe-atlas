[CmdletBinding()]
param(
    [switch]$SetupCheck,
    [switch]$CpuMapping,
    [switch]$NoJson,
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-SpaRepositoryRoot
$checks = New-Object System.Collections.Generic.List[object]
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Add-DoctorCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][ValidateSet("PASS", "WARN", "FAIL", "SKIP")][string]$Status,
        [Parameter(Mandatory = $true)][string]$Detail,
        [string]$Impact = "",
        [string]$Fix = ""
    )
    $checks.Add([pscustomobject]@{
        name = $Name
        status = $Status
        detail = $Detail
        impact = $Impact
        fix = $Fix
    })
    $level = if ($Status -eq "WARN") { "WARNING" } elseif ($Status -eq "FAIL") { "FAILED" } else { "PASS" }
    if ($Status -eq "SKIP") { $level = "INFO" }
    Write-SpaStatus -Level $level -Message ("{0}: {1}" -f $Name, $Detail)
}

function Invoke-DoctorProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory = $repoRoot)
    Push-Location $WorkingDirectory
    try {
        $output = & $Executable @Arguments 2>&1
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($output -join "`n").Trim() }
    } catch {
        return [pscustomobject]@{ ExitCode = 1; Output = $_.Exception.Message }
    } finally {
        Pop-Location
    }
}

function Protect-DoctorText {
    param([string]$Text, [string]$DataRoot)
    if ($null -eq $Text) { return "" }
    $value = $Text
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) { $value = $value.Replace($env:USERPROFILE, "%USERPROFILE%") }
    if (-not [string]::IsNullOrWhiteSpace($DataRoot)) { $value = $value.Replace($DataRoot, "%SPA_DATA_ROOT%") }
    $value = $value.Replace($repoRoot, "%SPA_INSTALL_ROOT%")
    $value = [Text.RegularExpressions.Regex]::Replace($value, '(?i)(token|secret|cookie)\s*[=:]\s*[^\s,;]+', '$1=<redacted>')
    return $value
}

Write-Host "Spatial Probe Atlas doctor (non-destructive diagnostics)"
Write-Host ""

if ($env:OS -eq "Windows_NT" -and [Environment]::Is64BitOperatingSystem) {
    Add-DoctorCheck -Name "Windows" -Status PASS -Detail "$([Environment]::OSVersion.VersionString), 64-bit"
} else {
    Add-DoctorCheck -Name "Windows" -Status FAIL -Detail "A supported 64-bit Windows runtime was not detected." -Impact "The v1 runtime is Windows-only." -Fix "Use 64-bit Windows 10 or 11."
}

$architecturePath = Join-Path $repoRoot "ARCHITECTURE.md"
if (Test-Path -LiteralPath $architecturePath -PathType Leaf) {
    $pathNote = if ($repoRoot.Length -gt 180) { " Repository path is long ($($repoRoot.Length) characters)." } else { "" }
    $pathStatus = if ($repoRoot.Length -gt 220) { "WARN" } else { "PASS" }
    Add-DoctorCheck -Name "Repository" -Status $pathStatus -Detail "Architecture and repository are present.$pathNote" -Impact "Very long paths can break native mapping tools." -Fix "Use a shorter install directory if a native tool reports a path error."
} else {
    Add-DoctorCheck -Name "Repository" -Status FAIL -Detail "ARCHITECTURE.md is missing." -Fix "Restore the complete release directory."
}

try {
    $runtimeManifest = Get-SpaRuntimeManifest -RepositoryRoot $repoRoot
    Add-DoctorCheck -Name "Runtime manifest" -Status PASS -Detail "Python $($runtimeManifest.python.version), Node $($runtimeManifest.node.version), checksums present."
} catch {
    Add-DoctorCheck -Name "Runtime manifest" -Status FAIL -Detail $_.Exception.Message -Fix "Restore tools\runtime-manifest.json from the release."
}

$hostPython = Get-SpaPython -RepositoryRoot $repoRoot
if ($null -eq $hostPython) {
    Add-DoctorCheck -Name "Python runtime" -Status FAIL -Detail "Python 3.11 was not found." -Impact "The backend cannot run." -Fix "Run setup.bat -AcceptRuntimeDownloads."
} else {
    Add-DoctorCheck -Name "Python runtime" -Status PASS -Detail "Python $($hostPython.Version) selected."
}

$venvPythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$venvPython = if (Test-Path -LiteralPath $venvPythonPath -PathType Leaf) { $venvPythonPath } else { $null }
$dependencyProfile = "cpu"
$effectiveComputeProfile = "cpu"
if ($null -eq $venvPython) {
    Add-DoctorCheck -Name "Python environment" -Status FAIL -Detail ".venv is missing." -Impact "Installed dependencies are unavailable." -Fix "Run setup.bat."
} else {
    $pipCheck = Invoke-DoctorProcess -Executable $venvPython -Arguments @("-m", "pip", "check")
    $setupMarkerPath = Join-Path $repoRoot ".setup-complete.json"
    if (Test-Path -LiteralPath $setupMarkerPath -PathType Leaf) {
        try {
            $setupMarker = Get-Content -LiteralPath $setupMarkerPath -Raw | ConvertFrom-Json
            if ($setupMarker.PSObject.Properties.Name -contains "dependency_profile" -and $setupMarker.dependency_profile -in @("cpu", "cuda")) {
                $dependencyProfile = [string]$setupMarker.dependency_profile
            }
            if ($setupMarker.PSObject.Properties.Name -contains "effective_compute_profile" -and $setupMarker.effective_compute_profile -in @("cpu", "cuda")) {
                $effectiveComputeProfile = [string]$setupMarker.effective_compute_profile
            } else {
                $effectiveComputeProfile = $dependencyProfile
            }
        } catch { }
    }
    $lockPath = Join-Path $repoRoot "backend\requirements-$dependencyProfile.lock.txt"
    $lockScript = Join-Path $repoRoot "scripts\check_python_lock.py"
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf) -or -not (Test-Path -LiteralPath $lockScript -PathType Leaf)) {
        Add-DoctorCheck -Name "Python environment" -Status FAIL -Detail "The $dependencyProfile dependency lock or installed-vs-lock checker is missing." -Fix "Restore the complete release and run setup.bat."
    } else {
        $lockCheck = Invoke-DoctorProcess -Executable $venvPython -Arguments @($lockScript, $lockPath)
        if ($pipCheck.ExitCode -eq 0 -and $lockCheck.ExitCode -eq 0) {
            Add-DoctorCheck -Name "Python environment" -Status PASS -Detail "pip dependencies are consistent and $($lockCheck.Output) for the $dependencyProfile profile."
        } else {
            $details = @()
            if ($pipCheck.ExitCode -ne 0) { $details += $pipCheck.Output }
            if ($lockCheck.ExitCode -ne 0) { $details += $lockCheck.Output }
            Add-DoctorCheck -Name "Python environment" -Status FAIL -Detail ($details -join " | ") -Fix "Run setup.bat to restore the selected exact lock. CUDA availability is diagnosed separately and may safely fall back to CPU."
        }
    }
}

$node = Get-SpaNode -RepositoryRoot $repoRoot
if ($null -eq $node) {
    Add-DoctorCheck -Name "Node runtime" -Status FAIL -Detail "Node 22 was not found." -Impact "The frontend cannot be rebuilt." -Fix "Run setup.bat -AcceptRuntimeDownloads."
} else {
    Add-DoctorCheck -Name "Node runtime" -Status PASS -Detail "Node $($node.Version) selected."
}

$frontendIndex = Join-Path $repoRoot "frontend\dist\index.html"
if (Test-Path -LiteralPath $frontendIndex -PathType Leaf) {
    Add-DoctorCheck -Name "Frontend build" -Status PASS -Detail "Production index.html is present."
} else {
    Add-DoctorCheck -Name "Frontend build" -Status FAIL -Detail "frontend\dist\index.html is missing." -Impact "The production server cannot serve the UI." -Fix "Run setup.bat."
}

$schemaFiles = @(Get-ChildItem -LiteralPath (Join-Path $repoRoot "schemas") -Filter "*.json" -File -Recurse -ErrorAction SilentlyContinue)
$schemaErrors = New-Object System.Collections.Generic.List[string]
foreach ($schema in $schemaFiles) {
    try { Get-Content -LiteralPath $schema.FullName -Raw | ConvertFrom-Json | Out-Null } catch { $schemaErrors.Add($schema.Name) }
}
if ($schemaFiles.Count -ge 3 -and $schemaErrors.Count -eq 0) {
    Add-DoctorCheck -Name "Portable schemas" -Status PASS -Detail "$($schemaFiles.Count) JSON schemas parsed successfully."
} elseif ($schemaErrors.Count -gt 0) {
    Add-DoctorCheck -Name "Portable schemas" -Status FAIL -Detail ("Invalid JSON: " + ($schemaErrors -join ", ")) -Fix "Restore schemas from the release."
} else {
    Add-DoctorCheck -Name "Portable schemas" -Status FAIL -Detail "Required portable schemas are missing." -Fix "Restore the complete release directory."
}

$migrationModule = Join-Path $repoRoot "backend\src\spatial_probe_atlas\migrations.py"
$migrationDirectory = Join-Path $repoRoot "backend\migrations"
if ((Test-Path -LiteralPath $migrationModule) -or (Test-Path -LiteralPath $migrationDirectory)) {
    Add-DoctorCheck -Name "Migrations" -Status PASS -Detail "Migration entrypoint is present."
} else {
    Add-DoctorCheck -Name "Migrations" -Status FAIL -Detail "No database migration entrypoint was found." -Fix "Restore backend migrations from the release."
}

try {
    $computer = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
    $ramGiB = [Math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
    $cpuCount = [Environment]::ProcessorCount
    $resourceStatus = if ($ramGiB -lt 8) { "WARN" } else { "PASS" }
    Add-DoctorCheck -Name "CPU and RAM" -Status $resourceStatus -Detail "$cpuCount logical CPUs, $ramGiB GiB RAM." -Impact "Low RAM reduces point-cloud and mapping capacity." -Fix "Lower viewer and mapping budgets in Settings."
} catch {
    Add-DoctorCheck -Name "CPU and RAM" -Status WARN -Detail "Resource inventory failed: $($_.Exception.Message)"
}

$dataRoot = ""
try {
    $dataRoot = Get-SpaDataRoot -RepositoryRoot $repoRoot
    $atomicOk = Test-SpaAtomicWrite -Directory $dataRoot
    if ($atomicOk) {
        Add-DoctorCheck -Name "Data root" -Status PASS -Detail "Writable atomic local storage is available at $dataRoot."
    } else {
        Add-DoctorCheck -Name "Data root" -Status FAIL -Detail "Atomic write test failed at $dataRoot." -Fix "Select a writable local NTFS data root."
    }
    $driveName = ([IO.Path]::GetPathRoot($dataRoot)).TrimEnd('\').TrimEnd(':')
    $drive = Get-PSDrive -Name $driveName -ErrorAction Stop
    $freeGiB = [Math]::Round($drive.Free / 1GB, 1)
    $diskStatus = if ($drive.Free -lt 2GB) { "FAIL" } elseif ($drive.Free -lt 20GB) { "WARN" } else { "PASS" }
    Add-DoctorCheck -Name "Data-root disk" -Status $diskStatus -Detail "$freeGiB GiB free." -Impact "New mapping jobs need their peak estimate plus a 10 GiB reserve." -Fix "Free disk space or migrate the data root in Settings."
} catch {
    Add-DoctorCheck -Name "Data root" -Status FAIL -Detail $_.Exception.Message -Fix "Correct SPA_DATA_ROOT or run setup.bat."
}

$nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($null -eq $nvidia) {
    Add-DoctorCheck -Name "NVIDIA driver" -Status WARN -Detail "nvidia-smi is unavailable; CPU fallback will be used." -Impact "Mapping may take longer." -Fix "No action is required for CPU mode."
} else {
    $gpu = Invoke-DoctorProcess -Executable $nvidia.Source -Arguments @("--query-gpu=name,driver_version,memory.total,compute_cap", "--format=csv,noheader")
    if ($gpu.ExitCode -eq 0) {
        Add-DoctorCheck -Name "NVIDIA driver" -Status PASS -Detail $gpu.Output
    } else {
        Add-DoctorCheck -Name "NVIDIA driver" -Status WARN -Detail "nvidia-smi failed: $($gpu.Output)" -Impact "CPU fallback will be used."
    }
}

if ($null -ne $venvPython) {
    $cudaCode = @'
import json
try:
    import torch
    result = {"torch": torch.__version__, "available": torch.cuda.is_available()}
    if result["available"]:
        value = torch.ones(1, device="cuda") * 2
        result.update({"device": torch.cuda.get_device_name(0), "smoke": float(value.cpu()[0]) == 2.0})
    print(json.dumps(result))
except Exception as exc:
    print(json.dumps({"available": False, "error": type(exc).__name__}))
'@
    $cuda = Invoke-DoctorProcess -Executable $venvPython -Arguments @("-c", $cudaCode)
    if ($cuda.ExitCode -eq 0 -and $cuda.Output -match '"available": true' -and $cuda.Output -match '"smoke": true') {
        Add-DoctorCheck -Name "PyTorch CUDA" -Status PASS -Detail $cuda.Output
    } else {
        Add-DoctorCheck -Name "PyTorch CUDA" -Status WARN -Detail "CUDA smoke test unavailable; CPU fallback is active. $($cuda.Output)" -Impact "CUDA acceleration is disabled." -Fix "Use CPU mode or rerun setup with a tested CUDA lock."
    }
}

$record3dStatus = "SKIP"
$record3dDetail = "Record3D package check requires the installed Python environment."
if ($null -ne $venvPython) {
    $record3d = Invoke-DoctorProcess -Executable $venvPython -Arguments @("-c", "import importlib.util; print('present' if importlib.util.find_spec('record3d') else 'missing')")
    if ($record3d.ExitCode -eq 0 -and $record3d.Output -match 'present') {
        $record3dStatus = "PASS"
        $record3dDetail = "Record3D Python package is importable. Device takeover was intentionally not attempted."
    } else {
        $record3dStatus = "WARN"
        $record3dDetail = "Record3D package is unavailable; replay/import workflows remain usable."
    }
}
Add-DoctorCheck -Name "Record3D SDK" -Status $record3dStatus -Detail $record3dDetail -Impact "Live device capture is unavailable without the SDK." -Fix "Install the tested Record3D dependency through the release setup when hardware support is required."

$modelManifestPath = Join-Path $repoRoot "models\manifest.json"
if (-not (Test-Path -LiteralPath $modelManifestPath -PathType Leaf)) {
    Add-DoctorCheck -Name "Models" -Status FAIL -Detail "models\manifest.json is missing." -Fix "Restore the complete release directory."
} else {
    try {
        $modelManifest = Get-Content -LiteralPath $modelManifestPath -Raw | ConvertFrom-Json
        $modelErrors = New-Object System.Collections.Generic.List[string]
        $applicableModelCount = 0
        foreach ($asset in @($modelManifest.assets)) {
            $profiles = @($asset.profiles)
            if ($profiles.Count -gt 0 -and $profiles -notcontains $effectiveComputeProfile -and $profiles -notcontains "all") { continue }
            $applicableModelCount += 1
            if ([string]::IsNullOrWhiteSpace($asset.license) -or [string]::IsNullOrWhiteSpace($asset.sha256)) {
                $modelErrors.Add("$($asset.id): metadata")
                continue
            }
            $filename = if ([string]::IsNullOrWhiteSpace($asset.filename)) { "$($asset.id).bin" } else { $asset.filename }
            $path = Join-Path (Join-Path $dataRoot "models") $filename
            if (-not (Test-Path -LiteralPath $path) -or (Get-SpaFileHash -Path $path) -ne $asset.sha256) { $modelErrors.Add("$($asset.id): file/checksum") }
        }
        if ($modelErrors.Count -eq 0) {
            Add-DoctorCheck -Name "Models" -Status PASS -Detail "$applicableModelCount assets required by the effective $effectiveComputeProfile profile have valid metadata and checksums."
        } else {
            Add-DoctorCheck -Name "Models" -Status FAIL -Detail ($modelErrors -join "; ") -Fix "Run setup.bat online, or supply the verified offline assets."
        }
    } catch {
        Add-DoctorCheck -Name "Models" -Status FAIL -Detail $_.Exception.Message -Fix "Restore a valid model manifest."
    }
}

$preferredPort = 8765
if ($env:SPA_PORT -and [int]::TryParse($env:SPA_PORT, [ref]$preferredPort) -eq $false) { $preferredPort = 8765 }
$instance = if ($dataRoot) { Get-SpaInstanceInfo -DataRoot $dataRoot } else { $null }
if ($null -ne $instance -and $instance.Url -and (Test-SpaReadyEndpoint -BaseUrl $instance.Url)) {
    Add-DoctorCheck -Name "Instance and port" -Status PASS -Detail "A healthy local instance is running at $($instance.Url)."
} elseif (Test-SpaPortAvailable -Port $preferredPort) {
    $detail = "Loopback port $preferredPort is available."
    if ($null -ne $instance) { $detail += " An instance file exists but is not healthy; run.bat will let the backend resolve its lock." }
    Add-DoctorCheck -Name "Instance and port" -Status PASS -Detail $detail
} else {
    Add-DoctorCheck -Name "Instance and port" -Status WARN -Detail "Preferred port $preferredPort is busy; run.bat will scan a narrow loopback range." -Fix "Close the conflicting local application or configure another port."
}

$databasePath = if ($dataRoot) { Join-Path $dataRoot "app.db" } else { "" }
if ($databasePath -and (Test-Path -LiteralPath $databasePath -PathType Leaf) -and $null -ne $venvPython) {
    $dbCode = "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
    $database = Invoke-DoctorProcess -Executable $venvPython -Arguments @("-c", $dbCode, $databasePath)
    if ($database.ExitCode -eq 0 -and $database.Output.Trim() -eq "ok") {
        Add-DoctorCheck -Name "Database integrity" -Status PASS -Detail "SQLite integrity_check returned ok."
    } else {
        Add-DoctorCheck -Name "Database integrity" -Status FAIL -Detail $database.Output -Impact "The app must use read-only recovery." -Fix "Back up the data root and use the documented recovery workflow."
    }
} else {
    Add-DoctorCheck -Name "Database integrity" -Status SKIP -Detail "No initialized app.db exists yet. Setup/startup will create it."
}

$replayScript = Join-Path $repoRoot "scripts\replay_smoke.py"
if ($null -ne $venvPython -and (Test-Path -LiteralPath $replayScript -PathType Leaf)) {
    $env:PYTHONPATH = Join-Path $repoRoot "backend\src"
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $replay = Invoke-DoctorProcess -Executable $venvPython -Arguments @($replayScript)
    if ($replay.ExitCode -eq 0) {
        Add-DoctorCheck -Name "Replay smoke" -Status PASS -Detail $replay.Output
    } else {
        Add-DoctorCheck -Name "Replay smoke" -Status FAIL -Detail $replay.Output -Fix "Run scripts\verify.ps1 and inspect the replay failure."
    }
} else {
    Add-DoctorCheck -Name "Replay smoke" -Status SKIP -Detail "Replay smoke requires the completed local environment."
}

if ($CpuMapping) {
    $mappingScript = Join-Path $repoRoot "scripts\cpu_mapping_smoke.py"
    if ($null -ne $venvPython -and (Test-Path -LiteralPath $mappingScript -PathType Leaf)) {
        $env:PYTHONPATH = Join-Path $repoRoot "backend\src"
        $mapping = Invoke-DoctorProcess -Executable $venvPython -Arguments @($mappingScript)
        if ($mapping.ExitCode -eq 0) {
            Add-DoctorCheck -Name "CPU mapping fixture" -Status PASS -Detail $mapping.Output
        } else {
            Add-DoctorCheck -Name "CPU mapping fixture" -Status FAIL -Detail $mapping.Output -Fix "Inspect mapping dependencies and the deterministic fixture."
        }
    } else {
        Add-DoctorCheck -Name "CPU mapping fixture" -Status SKIP -Detail "CPU mapping fixture is unavailable."
    }
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = if ($dataRoot) { Join-Path $dataRoot "support" } else { Join-Path $repoRoot ".setup-logs" }
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$reportPath = Join-Path $OutputDirectory "doctor-$timestamp.txt"
$reportLines = New-Object System.Collections.Generic.List[string]
$reportLines.Add("Spatial Probe Atlas doctor report")
$reportLines.Add("Generated (UTC): $([DateTime]::UtcNow.ToString('o'))")
$reportLines.Add("")
foreach ($check in $checks) {
    $reportLines.Add(("[{0}] {1}: {2}" -f $check.status, $check.name, $check.detail))
    if ($check.impact) { $reportLines.Add("  Impact: $($check.impact)") }
    if ($check.fix) { $reportLines.Add("  Fix: $($check.fix)") }
}
[IO.File]::WriteAllLines($reportPath, $reportLines, [Text.UTF8Encoding]::new($false))

if (-not $NoJson) {
    $redacted = foreach ($check in $checks) {
        [ordered]@{
            name = $check.name
            status = $check.status
            detail = Protect-DoctorText -Text $check.detail -DataRoot $dataRoot
            impact = Protect-DoctorText -Text $check.impact -DataRoot $dataRoot
            fix = Protect-DoctorText -Text $check.fix -DataRoot $dataRoot
        }
    }
    $jsonPath = Join-Path $OutputDirectory "doctor-$timestamp.redacted.json"
    $payload = [ordered]@{ schema_version = 1; generated_at = [DateTime]::UtcNow.ToString("o"); checks = @($redacted) }
    [IO.File]::WriteAllText($jsonPath, ($payload | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    Write-Host "Redacted JSON: $jsonPath"
}

$failCount = @($checks | Where-Object status -eq "FAIL").Count
$warnCount = @($checks | Where-Object status -eq "WARN").Count
Write-Host ""
Write-Host "Report: $reportPath"
Write-Host "Result: $failCount FAIL, $warnCount WARN"
if ($failCount -gt 0) { exit 1 }
exit 0

