[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "cuda")][string]$ComputeProfile = "auto",
    [switch]$AcceptRuntimeDownloads,
    [switch]$Offline,
    [switch]$NonInteractive,
    [switch]$SkipFrontendBuild,
    [switch]$SkipSmokeTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-SpaRepositoryRoot
$manifest = Get-SpaRuntimeManifest -RepositoryRoot $repoRoot
$setupLogDirectory = Join-Path $repoRoot ".setup-logs"
New-Item -ItemType Directory -Path $setupLogDirectory -Force | Out-Null
$setupLog = Join-Path $setupLogDirectory ("setup-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
[IO.File]::WriteAllText($setupLog, "Spatial Probe Atlas setup`r`n", [Text.UTF8Encoding]::new($false))

function Invoke-LoggedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description,
        [string]$WorkingDirectory = $repoRoot
    )
    Write-SpaStatus -Level INFO -Message $Description -LogPath $setupLog
    Push-Location $WorkingDirectory
    try {
        $prevErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $Executable @Arguments 2>&1 | Tee-Object -FilePath $setupLog -Append
        $ErrorActionPreference = $prevErrorAction
        if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
}

function Get-VerifiedDownload {
    param([Parameter(Mandatory = $true)]$Runtime, [Parameter(Mandatory = $true)][string]$Destination)

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        if ((Get-SpaFileHash -Path $Destination) -eq $Runtime.sha256) {
            Write-SpaStatus -Level PASS -Message "Using verified cached $($Runtime.filename)." -LogPath $setupLog
            return
        }
        Remove-Item -LiteralPath $Destination -Force
    }

    $offlineSource = Join-Path $repoRoot ("tools\offline\{0}" -f $Runtime.filename)
    if (Test-Path -LiteralPath $offlineSource -PathType Leaf) {
        Copy-Item -LiteralPath $offlineSource -Destination $Destination
    } elseif ($Offline) {
        throw "Offline runtime is missing: $offlineSource"
    } else {
        if (-not $AcceptRuntimeDownloads) {
            if ($NonInteractive) {
                throw "A supported runtime is missing. Re-run setup.bat -AcceptRuntimeDownloads, or provide tools\offline\$($Runtime.filename)."
            }
            $answer = Read-Host "Download verified $($Runtime.filename) from its official publisher? [y/N]"
            if ($answer -notmatch '^(y|yes)$') { throw "Runtime download declined." }
        }
        Write-SpaStatus -Level INFO -Message "Downloading $($Runtime.filename); this step is restartable." -LogPath $setupLog
        $partial = "$Destination.partial"
        try {
            Import-Module BitsTransfer -ErrorAction Stop
            Start-BitsTransfer -Source $Runtime.url -Destination $partial -DisplayName "Spatial Probe Atlas runtime"
        } catch {
            Invoke-WebRequest -UseBasicParsing -Uri $Runtime.url -OutFile $partial
        }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
    }

    $actualHash = Get-SpaFileHash -Path $Destination
    if ($actualHash -ne $Runtime.sha256) {
        Remove-Item -LiteralPath $Destination -Force
        throw "Checksum verification failed for $($Runtime.filename). Expected $($Runtime.sha256), got $actualHash."
    }
    Write-SpaStatus -Level PASS -Message "Verified $($Runtime.filename)." -LogPath $setupLog
}

function Install-LocalPython {
    $runtime = $manifest.python.windows_x64
    $downloads = Join-Path $repoRoot ".runtime\downloads"
    $target = Join-Path $repoRoot ".runtime\python"
    New-Item -ItemType Directory -Path $downloads -Force | Out-Null
    $installer = Join-Path $downloads $runtime.filename
    Get-VerifiedDownload -Runtime $runtime -Destination $installer
    Write-SpaStatus -Level INFO -Message "Installing Python $($manifest.python.version) into the repository-local runtime." -LogPath $setupLog
    $arguments = @(
        "/quiet", "InstallAllUsers=0", "TargetDir=`"$target`"", "Include_launcher=0",
        "Include_pip=1", "Include_test=0", "PrependPath=0", "Shortcuts=0"
    )
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $target "python.exe"))) {
        throw "The verified Python installer failed with exit code $($process.ExitCode)."
    }
}

function Install-LocalNode {
    $runtime = $manifest.node.windows_x64
    $downloads = Join-Path $repoRoot ".runtime\downloads"
    $target = Join-Path $repoRoot ".runtime\node"
    $staging = Join-Path $repoRoot ".runtime\node-staging"
    New-Item -ItemType Directory -Path $downloads -Force | Out-Null
    $archive = Join-Path $downloads $runtime.filename
    Get-VerifiedDownload -Runtime $runtime -Destination $archive
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Path $staging | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
    $extracted = Get-ChildItem -LiteralPath $staging -Directory | Select-Object -First 1
    if ($null -eq $extracted -or -not (Test-Path -LiteralPath (Join-Path $extracted.FullName "node.exe"))) {
        throw "The verified Node archive has an unexpected layout."
    }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Move-Item -LiteralPath $extracted.FullName -Destination $target
    Remove-Item -LiteralPath $staging -Recurse -Force
    Write-SpaStatus -Level PASS -Message "Installed Node $($manifest.node.version) into the repository-local runtime." -LogPath $setupLog
}

function Test-NvidiaCandidate {
    $command = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($null -eq $command) { return $false }
    try {
        & $command.Source --query-gpu=name,driver_version --format=csv,noheader 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Install-ModelAssets {
    param([Parameter(Mandatory = $true)][string]$EffectiveProfile, [Parameter(Mandatory = $true)][string]$DataRoot)

    $modelManifestPath = Join-Path $repoRoot "models\manifest.json"
    if (-not (Test-Path -LiteralPath $modelManifestPath -PathType Leaf)) {
        throw "Missing model manifest: $modelManifestPath"
    }
    $modelManifest = Get-Content -LiteralPath $modelManifestPath -Raw | ConvertFrom-Json
    $modelRoot = Join-Path $DataRoot "models"
    New-Item -ItemType Directory -Path $modelRoot -Force | Out-Null
    foreach ($asset in @($modelManifest.assets)) {
        $profiles = @($asset.profiles)
        if ($profiles.Count -gt 0 -and $profiles -notcontains $EffectiveProfile -and $profiles -notcontains "all") { continue }
        if ([string]::IsNullOrWhiteSpace($asset.license) -or [string]::IsNullOrWhiteSpace($asset.sha256)) {
            throw "Model '$($asset.id)' is missing immutable checksum or license metadata."
        }
        $filename = if ([string]::IsNullOrWhiteSpace($asset.filename)) { "$($asset.id).bin" } else { $asset.filename }
        if ([IO.Path]::GetFileName($filename) -ne $filename) { throw "Unsafe model filename: $filename" }
        $destination = Join-Path $modelRoot $filename
        if ((Test-Path -LiteralPath $destination) -and (Get-SpaFileHash -Path $destination) -eq $asset.sha256) {
            Write-SpaStatus -Level PASS -Message "Model '$($asset.id)' is present and verified." -LogPath $setupLog
            continue
        }
        if ($Offline) { throw "Required offline model is missing or invalid: $filename" }
        $partial = "$destination.partial"
        Invoke-WebRequest -UseBasicParsing -Uri $asset.url -OutFile $partial
        if ((Get-SpaFileHash -Path $partial) -ne $asset.sha256) {
            Remove-Item -LiteralPath $partial -Force
            throw "Checksum verification failed for model '$($asset.id)'."
        }
        Move-Item -LiteralPath $partial -Destination $destination -Force
    }
}

try {
    Write-SpaStatus -Level INFO -Message "Repository: $repoRoot" -LogPath $setupLog
    if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitOperatingSystem) {
        throw "Spatial Probe Atlas v1 setup requires 64-bit Windows 10 or 11."
    }
    if (-not (Test-SpaAtomicWrite -Directory $repoRoot)) { throw "The repository is not writable." }
    $dataRoot = Get-SpaDataRoot -RepositoryRoot $repoRoot
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    foreach ($name in @("projects", "models", "cache", "logs", "temp", "support")) {
        New-Item -ItemType Directory -Path (Join-Path $dataRoot $name) -Force | Out-Null
    }
    if (-not (Test-SpaAtomicWrite -Directory $dataRoot)) { throw "The data root does not support atomic local writes: $dataRoot" }

    $driveName = ([IO.Path]::GetPathRoot($dataRoot)).TrimEnd('\').TrimEnd(':')
    $drive = Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue
    if ($null -ne $drive -and $drive.Free -lt 20GB) {
        Write-SpaStatus -Level WARNING -Message ("Only {0:N1} GiB is free on the data-root volume; mapping warns below 20 GiB." -f ($drive.Free / 1GB)) -LogPath $setupLog
    }

    $python = Get-SpaPython -RepositoryRoot $repoRoot
    if ($null -eq $python) {
        Install-LocalPython
        $python = Get-SpaPython -RepositoryRoot $repoRoot
    }
    if ($null -eq $python) { throw "Python 3.11 could not be selected." }
    Write-SpaStatus -Level PASS -Message "Python $($python.Version): $($python.Path)" -LogPath $setupLog

    $node = Get-SpaNode -RepositoryRoot $repoRoot
    if ($null -eq $node) {
        Install-LocalNode
        $node = Get-SpaNode -RepositoryRoot $repoRoot
    }
    if ($null -eq $node -or -not (Test-Path -LiteralPath $node.Npm -PathType Leaf)) { throw "Node 22 with npm could not be selected." }
    Write-SpaStatus -Level PASS -Message "Node $($node.Version): $($node.Path)" -LogPath $setupLog

    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-LoggedCommand -Executable $python.Path -Arguments @("-m", "venv", (Join-Path $repoRoot ".venv")) -Description "Creating repository-local Python environment"
    }

    $requestedProfile = if ($env:SPA_COMPUTE_PROFILE) { $env:SPA_COMPUTE_PROFILE } else { $ComputeProfile }
    if ($requestedProfile -notin @("auto", "cpu", "cuda")) { throw "SPA_COMPUTE_PROFILE must be auto, cpu, or cuda." }
    $nvidiaCandidate = Test-NvidiaCandidate
    $dependencyProfile = if ($requestedProfile -eq "cuda" -or ($requestedProfile -eq "auto" -and $nvidiaCandidate)) { "cuda" } else { "cpu" }
    $lockPath = Join-Path $repoRoot "backend\requirements-$dependencyProfile.lock.txt"
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw "Missing dependency lock: $lockPath" }
    $lockText = Get-Content -LiteralPath $lockPath -Raw
    $pipArguments = @("-m", "pip", "install", "--disable-pip-version-check", "--no-input")
    if (-not $lockText.Contains("--hash=sha256:")) { throw "Dependency lock is not hash-pinned: $lockPath" }
    $pipArguments += "--require-hashes"
    $pipArguments += @("-r", $lockPath)
    try {
        Invoke-LoggedCommand -Executable $venvPython -Arguments $pipArguments -Description "Installing exact $dependencyProfile backend dependencies"
    } catch {
        if ($dependencyProfile -ne "cuda") { throw }
        Write-SpaStatus -Level WARNING -Message "The CUDA dependency profile could not be installed; retrying with the fully supported CPU lock." -LogPath $setupLog
        $dependencyProfile = "cpu"
        $lockPath = Join-Path $repoRoot "backend\requirements-cpu.lock.txt"
        if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw "Missing CPU dependency lock: $lockPath" }
        $lockText = Get-Content -LiteralPath $lockPath -Raw
        if (-not $lockText.Contains("--hash=sha256:")) { throw "CPU dependency lock is not hash-pinned: $lockPath" }
        $pipArguments = @(
            "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--require-hashes", "-r", $lockPath
        )
        Invoke-LoggedCommand -Executable $venvPython -Arguments $pipArguments -Description "Installing exact CPU fallback dependencies"
    }

    $effectiveProfile = "cpu"
    if ($dependencyProfile -eq "cuda") {
        $cudaProbe = & $venvPython -c "import sys;`ntry:`n import torch; ok=torch.cuda.is_available(); print('cuda' if ok else 'cpu')`nexcept Exception:`n print('cpu')" 2>$null
        if (($cudaProbe | Select-Object -Last 1).Trim() -eq "cuda") {
            $effectiveProfile = "cuda"
            Write-SpaStatus -Level PASS -Message "CUDA allocation path is available." -LogPath $setupLog
        } else {
            Write-SpaStatus -Level WARNING -Message "CUDA was not verified; the application will use the CPU-correct profile." -LogPath $setupLog
        }
    } elseif (-not $nvidiaCandidate) {
        Write-SpaStatus -Level WARNING -Message "No usable NVIDIA driver was found; CPU mode remains fully supported." -LogPath $setupLog
    }

    $env:PYTHONPATH = Join-Path $repoRoot "backend\src"
    $env:SPA_DATA_ROOT = $dataRoot
    $env:SPA_COMPUTE_PROFILE = $effectiveProfile
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot ".env.local"))) {
        Copy-Item -LiteralPath (Join-Path $repoRoot ".env.example") -Destination (Join-Path $repoRoot ".env.local")
    }

    if (-not $SkipFrontendBuild) {
        $packageLock = Join-Path $repoRoot "frontend\package-lock.json"
        if (-not (Test-Path -LiteralPath $packageLock -PathType Leaf)) { throw "frontend\package-lock.json is required for deterministic npm ci." }
        Invoke-LoggedCommand -Executable $node.Npm -Arguments @("ci", "--no-audit", "--no-fund") -Description "Installing exact frontend dependencies" -WorkingDirectory (Join-Path $repoRoot "frontend")
        Invoke-LoggedCommand -Executable $node.Npm -Arguments @("run", "build") -Description "Building the production frontend" -WorkingDirectory (Join-Path $repoRoot "frontend")
    }

    try {
        Install-ModelAssets -EffectiveProfile $effectiveProfile -DataRoot $dataRoot
    } catch {
        if ($effectiveProfile -ne "cuda") { throw }
        $effectiveProfile = "cpu"
        $env:SPA_COMPUTE_PROFILE = "cpu"
        Write-SpaStatus -Level WARNING -Message "CUDA model assets could not be verified; setup remains valid and will use the CPU-correct profile." -LogPath $setupLog
        Install-ModelAssets -EffectiveProfile "cpu" -DataRoot $dataRoot
    }
    if ($effectiveProfile -eq "cuda") {
        $cudaSmokeScript = Join-Path $repoRoot "scripts\cuda_mapping_smoke.py"
        if (-not (Test-Path -LiteralPath $cudaSmokeScript -PathType Leaf)) {
            throw "The CUDA dependency profile is selected but its smoke test is missing."
        }
        Write-SpaStatus -Level INFO -Message "Running the verified CUDA allocation, model, and mapping smoke test." -LogPath $setupLog
        Push-Location $repoRoot
        try {
            $cudaSmokeOutput = & $venvPython $cudaSmokeScript --require-ready 2>&1
            $cudaSmokeExit = $LASTEXITCODE
            $cudaSmokeOutput | Tee-Object -FilePath $setupLog -Append
        } finally {
            Pop-Location
        }
        if ($cudaSmokeExit -ne 0) {
            $effectiveProfile = "cpu"
            $env:SPA_COMPUTE_PROFILE = "cpu"
            Write-SpaStatus -Level WARNING -Message "CUDA did not pass the release smoke test; setup remains valid and will use the CPU-correct profile." -LogPath $setupLog
        } else {
            Write-SpaStatus -Level PASS -Message "CUDA mapping smoke test passed." -LogPath $setupLog
        }
    }
    Invoke-LoggedCommand -Executable $venvPython -Arguments @("-m", "spatial_probe_atlas.migrations") -Description "Applying database migrations"
    Invoke-LoggedCommand -Executable $venvPython -Arguments @("-c", "import spatial_probe_atlas; print(spatial_probe_atlas.__version__)") -Description "Checking backend import"

    foreach ($schema in Get-ChildItem -LiteralPath (Join-Path $repoRoot "schemas") -Filter "*.json" -File -Recurse) {
        Get-Content -LiteralPath $schema.FullName -Raw | ConvertFrom-Json | Out-Null
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "frontend\dist\index.html") -PathType Leaf) -and -not $SkipFrontendBuild) {
        throw "The production frontend build is missing index.html."
    }

    if (-not $SkipSmokeTests) {
        $smokeScript = Join-Path $repoRoot "scripts\cpu_mapping_smoke.py"
        if (Test-Path -LiteralPath $smokeScript -PathType Leaf) {
            Invoke-LoggedCommand -Executable $venvPython -Arguments @($smokeScript) -Description "Running deterministic CPU mapping smoke fixture"
        }
        $doctorProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @(
            "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            ("`"{0}`"" -f (Join-Path $repoRoot "tools\doctor.ps1")), "-SetupCheck", "-NoJson"
        ) -Wait -PassThru -NoNewWindow
        if ($doctorProcess.ExitCode -ne 0) { throw "Setup diagnostics reported a required failure." }
    }

    $hashInputs = @(
        "tools\runtime-manifest.json", "backend\requirements-cpu.lock.txt",
        "backend\requirements-cuda.lock.txt", "frontend\package-lock.json", "models\manifest.json"
    )
    $hashes = [ordered]@{}
    foreach ($relative in $hashInputs) {
        $path = Join-Path $repoRoot $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) { $hashes[$relative] = Get-SpaFileHash -Path $path }
    }
    foreach ($schema in Get-ChildItem -LiteralPath (Join-Path $repoRoot "schemas") -Filter "*.json" -File -Recurse) {
        $relative = $schema.FullName.Substring($repoRoot.Length + 1)
        $hashes[$relative] = Get-SpaFileHash -Path $schema.FullName
    }
    $marker = [ordered]@{
        schema_version = 1
        application_version = "1.0.0"
        completed_at = [DateTime]::UtcNow.ToString("o")
        python = @{ version = $python.Version; executable = $python.Path }
        node = @{ version = $node.Version; executable = $node.Path }
        dependency_profile = $dependencyProfile
        effective_compute_profile = $effectiveProfile
        data_root = $dataRoot
        hashes = $hashes
    }
    $markerPath = Join-Path $repoRoot ".setup-complete.json"
    [IO.File]::WriteAllText($markerPath, ($marker | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    Write-SpaStatus -Level SUCCESS -Message "Setup complete - run run.bat" -LogPath $setupLog
    Write-SpaStatus -Level INFO -Message "Effective compute mode: $effectiveProfile" -LogPath $setupLog
    Write-SpaStatus -Level INFO -Message "Setup log: $setupLog" -LogPath $setupLog
    exit 0
} catch {
    Write-SpaStatus -Level FAILED -Message $_.Exception.Message -LogPath $setupLog
    Write-SpaStatus -Level INFO -Message "Fix the reported issue and run setup.bat again. Completed verified steps are reused." -LogPath $setupLog
    Write-SpaStatus -Level INFO -Message "Setup log: $setupLog" -LogPath $setupLog
    exit 1
}


