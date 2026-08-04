[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$ReleasePackage,
    [string]$ReleaseUri,
    [string]$Sha256,
    [switch]$Offline
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-SpaRepositoryRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$stagingBase = Join-Path $repoRoot ".update-staging"
$rollbackBase = Join-Path $repoRoot ".rollback"
$updateId = (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$staging = Join-Path $stagingBase $updateId
$rollback = Join-Path $rollbackBase $updateId
$deployed = New-Object System.Collections.Generic.List[object]
$venvMoved = $false
$distMoved = $false
$completed = $false

function Assert-UpdateChildPath {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Parent)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + "\"
    if (-not $fullPath.StartsWith($fullParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe update path outside its allowed root: $fullPath"
    }
    return $fullPath
}

function Test-ActiveWork {
    param([string]$DataRoot)
    $instance = Get-SpaInstanceInfo -DataRoot $DataRoot
    if ($null -ne $instance -and $instance.Url -and (Test-SpaReadyEndpoint -BaseUrl $instance.Url)) {
        return "a healthy application instance is running"
    }
    $database = Join-Path $DataRoot "app.db"
    if (-not (Test-Path -LiteralPath $database -PathType Leaf) -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { return $null }
    $code = @'
import json, sqlite3, sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
active = []
for table, states in {
    "sessions": ("preflight", "running", "paused", "degraded", "stopping"),
    "jobs": ("queued", "admitted", "processing", "cancelling"),
}.items():
    if table not in tables:
        continue
    columns = {r[1] for r in db.execute(f"pragma table_info({table})")}
    state_column = "state" if "state" in columns else ("status" if "status" in columns else None)
    if state_column:
        marks = ",".join("?" for _ in states)
        count = db.execute(f"select count(*) from {table} where {state_column} in ({marks})", states).fetchone()[0]
        if count:
            active.append(f"{count} active {table}")
db.close()
print(json.dumps(active))
'@
    $result = & $venvPython -c $code $database 2>$null
    if ($LASTEXITCODE -eq 0) {
        $active = $result | ConvertFrom-Json
        if (@($active).Count -gt 0) { return (@($active) -join ", ") }
    }
    return $null
}

function Copy-UpdateMetadataBackup {
    param([string]$DataRoot, [string]$Destination)
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $database = Join-Path $DataRoot "app.db"
    if (Test-Path -LiteralPath $database -PathType Leaf) {
        $backupDatabase = Join-Path $Destination "app.db"
        $code = "import sqlite3,sys; s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()"
        & $venvPython -c $code $database $backupDatabase
        if ($LASTEXITCODE -ne 0) { throw "Database online-backup failed." }
    }
    $settings = Join-Path $DataRoot "settings.json"
    if (Test-Path -LiteralPath $settings -PathType Leaf) { Copy-Item -LiteralPath $settings -Destination (Join-Path $Destination "settings.json") }
    $projectRoot = Join-Path $DataRoot "projects"
    if (Test-Path -LiteralPath $projectRoot -PathType Container) {
        foreach ($manifest in Get-ChildItem -LiteralPath $projectRoot -File -Filter "*manifest*.json" -Recurse -ErrorAction SilentlyContinue) {
            $relative = $manifest.FullName.Substring($projectRoot.Length + 1)
            $target = Assert-UpdateChildPath -Path (Join-Path (Join-Path $Destination "project-manifests") $relative) -Parent $Destination
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Copy-Item -LiteralPath $manifest.FullName -Destination $target
        }
    }
}

if ([string]::IsNullOrWhiteSpace($ReleasePackage) -and [string]::IsNullOrWhiteSpace($ReleaseUri)) {
    Write-Host "Usage: update.bat -ReleasePackage C:\path\spatial-probe-atlas.zip [-Sha256 <64-hex>]"
    Write-Host "   or: update.bat -ReleaseUri https://.../release.zip -Sha256 <64-hex>"
    exit 2
}
if ($ReleasePackage -and $ReleaseUri) { Write-SpaStatus -Level FAILED -Message "Choose a local package or a release URI, not both."; exit 2 }
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { Write-SpaStatus -Level FAILED -Message "Run setup.bat before updating."; exit 1 }

try {
    $dataRoot = Get-SpaDataRoot -RepositoryRoot $repoRoot
    $active = Test-ActiveWork -DataRoot $dataRoot
    if ($active) { throw "Update refused because $active. Finalize/cancel work and close the app first." }

    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    New-Item -ItemType Directory -Path $rollback -Force | Out-Null
    $archive = Join-Path $staging "release.zip"
    if ($ReleasePackage) {
        $sourcePackage = [IO.Path]::GetFullPath($ReleasePackage)
        if (-not (Test-Path -LiteralPath $sourcePackage -PathType Leaf)) { throw "Release package not found: $sourcePackage" }
        Copy-Item -LiteralPath $sourcePackage -Destination $archive
        if ([string]::IsNullOrWhiteSpace($Sha256)) {
            $sidecar = "$sourcePackage.sha256"
            if (Test-Path -LiteralPath $sidecar -PathType Leaf) { $Sha256 = ((Get-Content -LiteralPath $sidecar -Raw).Trim() -split '\s+')[0] }
        }
    } else {
        if ($Offline) { throw "Offline mode requires -ReleasePackage." }
        if (-not $ReleaseUri.StartsWith("https://", [StringComparison]::OrdinalIgnoreCase)) { throw "Remote releases require HTTPS." }
        if ([string]::IsNullOrWhiteSpace($Sha256)) { throw "Remote releases require -Sha256." }
        Invoke-WebRequest -UseBasicParsing -Uri $ReleaseUri -OutFile $archive
    }
    if ([string]::IsNullOrWhiteSpace($Sha256) -or $Sha256 -notmatch '^[a-fA-F0-9]{64}$') {
        throw "Supply a trusted SHA-256 with -Sha256 or an adjacent .sha256 sidecar."
    }
    $actualHash = Get-SpaFileHash -Path $archive
    if ($actualHash -ne $Sha256.ToLowerInvariant()) { throw "Release checksum mismatch. Expected $Sha256, got $actualHash." }
    Write-SpaStatus -Level PASS -Message "Release archive checksum verified."

    $free = (Get-PSDrive -Name (([IO.Path]::GetPathRoot($repoRoot)).TrimEnd('\').TrimEnd(':'))).Free
    if ($free -lt ((Get-Item -LiteralPath $archive).Length * 3 + 2GB)) { throw "Insufficient free space for staged update and rollback reserve." }

    $extract = Join-Path $staging "extract"
    Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
    $packageRoot = $extract
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "release-manifest.json"))) {
        $children = @(Get-ChildItem -LiteralPath $extract -Directory)
        if ($children.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $children[0].FullName "release-manifest.json"))) { $packageRoot = $children[0].FullName }
    }
    $releaseManifestPath = Join-Path $packageRoot "release-manifest.json"
    if (-not (Test-Path -LiteralPath $releaseManifestPath -PathType Leaf)) { throw "Release package is missing release-manifest.json." }
    $releaseManifest = Get-Content -LiteralPath $releaseManifestPath -Raw | ConvertFrom-Json
    if ($releaseManifest.schema_version -ne 1 -or [string]::IsNullOrWhiteSpace($releaseManifest.version)) { throw "Unsupported release manifest." }

    $allowedRootFiles = @("README.md", "LICENSE", "ARCHITECTURE.md", ".env.example", "setup.bat", "run.bat", "doctor.bat", "update.bat")
    $allowedDirectories = @("backend", "frontend", "schemas", "models", "tools", "scripts", "docs", "tests")
    foreach ($file in @($releaseManifest.files)) {
        $relative = [string]$file.path
        if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative) -or $relative.Contains("..")) { throw "Unsafe release path: $relative" }
        $segments = $relative -split '[\\/]'
        if ($segments.Count -eq 1) {
            if ($allowedRootFiles -notcontains $relative) { throw "Release cannot write root path: $relative" }
        } elseif ($allowedDirectories -notcontains $segments[0]) {
            throw "Release cannot write directory: $($segments[0])"
        }
        $source = Assert-UpdateChildPath -Path (Join-Path $packageRoot $relative) -Parent $packageRoot
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Manifest file is missing: $relative" }
        if ((Get-SpaFileHash -Path $source) -ne ([string]$file.sha256).ToLowerInvariant()) { throw "Manifest checksum mismatch: $relative" }
    }

    Copy-UpdateMetadataBackup -DataRoot $dataRoot -Destination (Join-Path $rollback "data")
    $installBackup = Join-Path $rollback "install"
    foreach ($file in @($releaseManifest.files)) {
        $relative = [string]$file.path
        $target = Assert-UpdateChildPath -Path (Join-Path $repoRoot $relative) -Parent $repoRoot
        $backup = Assert-UpdateChildPath -Path (Join-Path $installBackup $relative) -Parent $installBackup
        $existed = Test-Path -LiteralPath $target -PathType Leaf
        if ($existed) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup
        }
        $deployed.Add([pscustomobject]@{ relative = $relative; existed = $existed })
    }

    $oldVenv = Join-Path $rollback ".venv"
    if (Test-Path -LiteralPath (Join-Path $repoRoot ".venv") -PathType Container) {
        Move-Item -LiteralPath (Join-Path $repoRoot ".venv") -Destination $oldVenv
        $venvMoved = $true
    }
    $oldDist = Join-Path $rollback "frontend-dist"
    if (Test-Path -LiteralPath (Join-Path $repoRoot "frontend\dist") -PathType Container) {
        Move-Item -LiteralPath (Join-Path $repoRoot "frontend\dist") -Destination $oldDist
        $distMoved = $true
    }

    foreach ($file in @($releaseManifest.files)) {
        $source = Assert-UpdateChildPath -Path (Join-Path $packageRoot $file.path) -Parent $packageRoot
        $target = Assert-UpdateChildPath -Path (Join-Path $repoRoot $file.path) -Parent $repoRoot
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
    Write-SpaStatus -Level INFO -Message "Installing release $($releaseManifest.version) with the new lockfiles..."
    $bootstrapProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        ("`"{0}`"" -f (Join-Path $repoRoot "tools\bootstrap.ps1")),
        "-NonInteractive", "-AcceptRuntimeDownloads"
    ) -Wait -PassThru -NoNewWindow
    if ($bootstrapProcess.ExitCode -ne 0) { throw "New release setup failed with exit code $($bootstrapProcess.ExitCode)." }

    $metadata = [ordered]@{
        schema_version = 1
        update_id = $updateId
        installed_version = $releaseManifest.version
        release_sha256 = $actualHash
        completed_at = [DateTime]::UtcNow.ToString("o")
        rollback_directory = $rollback
    }
    [IO.File]::WriteAllText((Join-Path $rollback "update.json"), ($metadata | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    $completed = $true
    Write-SpaStatus -Level SUCCESS -Message "Updated to Spatial Probe Atlas $($releaseManifest.version)."
    Write-Host "Rollback retained at: $rollback"
    exit 0
} catch {
    Write-SpaStatus -Level FAILED -Message $_.Exception.Message
    if (Test-Path -LiteralPath $rollback -PathType Container) {
        Write-SpaStatus -Level INFO -Message "Restoring the prior application files and environment..."
        foreach ($item in $deployed) {
            $target = Assert-UpdateChildPath -Path (Join-Path $repoRoot $item.relative) -Parent $repoRoot
            $backup = Assert-UpdateChildPath -Path (Join-Path (Join-Path $rollback "install") $item.relative) -Parent (Join-Path $rollback "install")
            if ($item.existed -and (Test-Path -LiteralPath $backup -PathType Leaf)) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                Copy-Item -LiteralPath $backup -Destination $target -Force
            } elseif (-not $item.existed -and (Test-Path -LiteralPath $target -PathType Leaf)) {
                Remove-Item -LiteralPath $target -Force
            }
        }
        if ($venvMoved -and (Test-Path -LiteralPath (Join-Path $rollback ".venv"))) {
            if (Test-Path -LiteralPath (Join-Path $repoRoot ".venv")) { Remove-Item -LiteralPath (Join-Path $repoRoot ".venv") -Recurse -Force }
            Move-Item -LiteralPath (Join-Path $rollback ".venv") -Destination (Join-Path $repoRoot ".venv")
        }
        if ($distMoved -and (Test-Path -LiteralPath (Join-Path $rollback "frontend-dist"))) {
            if (Test-Path -LiteralPath (Join-Path $repoRoot "frontend\dist")) { Remove-Item -LiteralPath (Join-Path $repoRoot "frontend\dist") -Recurse -Force }
            Move-Item -LiteralPath (Join-Path $rollback "frontend-dist") -Destination (Join-Path $repoRoot "frontend\dist")
        }
    }
    Write-Host "The data backup and update diagnostics remain at: $rollback"
    exit 1
} finally {
    if ($completed -and (Test-Path -LiteralPath $staging)) {
        $safeStaging = Assert-UpdateChildPath -Path $staging -Parent $stagingBase
        Remove-Item -LiteralPath $safeStaging -Recurse -Force
    }
}

