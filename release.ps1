$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Die($msg) { throw $msg }

# --- git presence (optional) ---
$HasGit = $true
try { git --version | Out-Null } catch { $HasGit = $false }

$AllowUntagged = $env:ALLOW_UNTAGGED
if ([string]::IsNullOrEmpty($AllowUntagged)) { $AllowUntagged = "0" }

$AllowDirty = $env:ALLOW_DIRTY
if ([string]::IsNullOrEmpty($AllowDirty)) { $AllowDirty = "0" }

$Version = ""
$PrevTag = ""

if ($HasGit) {
  if ($AllowDirty -ne "1") {
    $dirty = git status --porcelain
    if ($dirty) { Die "Working tree is dirty. Commit changes before release (or set ALLOW_DIRTY=1)." }
  }

  try {
    $Version = (git describe --tags --exact-match 2>$null).Trim()
  } catch {
    if ($AllowUntagged -eq "1") {
      $sha = (git rev-parse --short HEAD).Trim()
      $Version = "dev-$sha"
    } else {
      Die "HEAD is not tagged. Create a release tag (e.g., git tag -a v16.0.0 -m '...')"
    }
  }

  try {
    $PrevTag = (git describe --tags --abbrev=0 "$($Version)^" 2>$null).Trim()
  } catch {
    $PrevTag = ""
  }
} else {
  $Version = $env:RELEASE_VERSION
  if ([string]::IsNullOrEmpty($Version)) { $Version = "manual" }
  $PrevTag = ""
}

$ProjectDir = Get-Location
$ProjectName = Split-Path -Leaf $ProjectDir
$Parent = Split-Path -Parent $ProjectDir
$OutDir = Join-Path $Parent "dist"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$ArchiveName = "metr_${Version}_prod_clean.zip"
$ChangelogName = "CHANGELOG_${Version}.md"

function Clean-Artifacts {
  Get-ChildItem -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  Get-ChildItem -Recurse -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -in @(".pytest_cache", ".ruff_cache") } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  Get-ChildItem -Recurse -File -Filter "*.pyc" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  Get-ChildItem -Recurse -File -Filter "*.pyo" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  if (Test-Path (Join-Path $ProjectDir "logs")) {
    Get-ChildItem -Path (Join-Path $ProjectDir "logs") -Recurse -File -Filter "*.log" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
  }
  Remove-Item (Join-Path $ProjectDir "=3.9,") -Force -ErrorAction SilentlyContinue
  @(
    "data.db",
    "data.db-journal",
    "data.db-wal",
    "data.db-shm",
    "data\data.db",
    "data\data.db-journal",
    "data\data.db-wal",
    "data\data.db-shm"
  ) | ForEach-Object {
    $p = Join-Path $ProjectDir $_
    if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue }
  }
}

function Assert-CleanTree {
  $leftPycache = Get-ChildItem -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($leftPycache) { Die "__pycache__ found after cleanup. Refusing to build a dirty release." }

  $leftDevCache = Get-ChildItem -Recurse -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -in @(".pytest_cache", ".ruff_cache") } | Select-Object -First 1
  if ($leftDevCache) { Die "dev/test cache directory found after cleanup. Refusing to build a dirty release." }

  $leftLog = if (Test-Path (Join-Path $ProjectDir "logs")) {
    Get-ChildItem -Path (Join-Path $ProjectDir "logs") -Recurse -File -Filter "*.log" -ErrorAction SilentlyContinue | Select-Object -First 1
  } else { $null }
  if ($leftLog) { Die "runtime log files found after cleanup. Refusing to build a dirty release." }

  if (Test-Path (Join-Path $ProjectDir "=3.9,")) {
    Die "Suspicious temporary file '=3.9,' found. Refusing to build a dirty release."
  }
  if ((Test-Path (Join-Path $ProjectDir "data.db")) -or (Test-Path (Join-Path $ProjectDir "data\data.db"))) {
    Die "Runtime DB artifact found. Refusing to package user data."
  }
}

Write-Host "▶ Release version: $Version"
Write-Host "▶ Previous tag:    " -NoNewline
if ($PrevTag) { Write-Host $PrevTag } else { Write-Host "(none found)" }

Write-Host "▶ Cleaning caches, bytecode, logs and local DB artifacts..."
Clean-Artifacts
Assert-CleanTree

Write-Host "▶ Running strict validator (prod)..."
$env:APP_ENV = "prod"
$env:VALIDATOR_RELEASE_MODE = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
$tempDb = Join-Path ([System.IO.Path]::GetTempPath()) ("clientplatform_release_db_" + [guid]::NewGuid().ToString("N") + ".sqlite")
$env:CLIENTPLATFORM_DB_PATH = $tempDb
python scripts/validate_project.py
if ($LASTEXITCODE -ne 0) { Die "Validator failed" }

Write-Host "▶ Running smoke checks (no polling)..."
python scripts/smoke.py
if ($LASTEXITCODE -ne 0) { Die "Smoke checks failed" }
Remove-Item $tempDb, "$tempDb-journal", "$tempDb-wal", "$tempDb-shm" -Force -ErrorAction SilentlyContinue
Remove-Item Env:CLIENTPLATFORM_DB_PATH -ErrorAction SilentlyContinue

Write-Host "▶ Re-checking cleanliness after validator/smoke..."
Clean-Artifacts
Assert-CleanTree

Write-Host "▶ Generating changelog..."
$changelogPath = Join-Path $OutDir $ChangelogName
"# Changelog $Version`n" | Set-Content -Encoding UTF8 $changelogPath

if ($HasGit -and $PrevTag) {
  "Changes since **$PrevTag**:`n" | Add-Content -Encoding UTF8 $changelogPath
  git log --no-merges --pretty=format:"- %s (%h)" "$PrevTag..$Version" | Add-Content -Encoding UTF8 $changelogPath
  "`n`n## Diff stats`n" | Add-Content -Encoding UTF8 $changelogPath
  git diff --stat "$PrevTag..$Version" | Add-Content -Encoding UTF8 $changelogPath
} elseif ($HasGit) {
  "First tagged release (no previous tag found).`n" | Add-Content -Encoding UTF8 $changelogPath
  git log --no-merges --pretty=format:"- %s (%h)" -n 50 | Add-Content -Encoding UTF8 $changelogPath
} else {
  "No git repository detected. Set RELEASE_VERSION to label this build." | Add-Content -Encoding UTF8 $changelogPath
}

Write-Host "▶ Preparing clean staging tree for packaging..."
$zipPath = Join-Path $OutDir $ArchiveName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("clientplatform_release_stage_" + [guid]::NewGuid().ToString("N"))
$stageProject = Join-Path $stageRoot $ProjectName
New-Item -ItemType Directory -Force -Path $stageProject | Out-Null

$excludeNames = @("__pycache__", ".git", ".venv", "venv", "dist", ".pytest_cache", ".ruff_cache")
$excludeFileNames = @("data.db", "data.db-journal", "data.db-wal", "data.db-shm", "=3.9,")
$excludeExtensions = @(".pyc", ".pyo")

Get-ChildItem -LiteralPath $ProjectDir -Force | ForEach-Object {
  if ($excludeNames -contains $_.Name) { return }
  if ($excludeFileNames -contains $_.Name) { return }
  if ($excludeExtensions -contains $_.Extension) { return }
  Copy-Item -LiteralPath $_.FullName -Destination $stageProject -Recurse -Force
}

Get-ChildItem -LiteralPath $stageProject -Recurse -Force | Where-Object {
  ($excludeNames -contains $_.Name) -or
  ($excludeFileNames -contains $_.Name) -or
  ($excludeExtensions -contains $_.Extension) -or
  ($_.PSIsContainer -eq $false -and $_.DirectoryName -like "*\logs" -and $_.Extension -eq ".log")
} | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path (Join-Path $stageProject "data\data.db")) {
  Die "Staging tree still contains data\data.db. Refusing to package dirty release."
}
if (Test-Path (Join-Path $stageProject "data.db")) {
  Die "Staging tree still contains data.db. Refusing to package dirty release."
}

Write-Host "▶ Packaging archive..."
Push-Location $stageRoot
try {
  Compress-Archive -Path $ProjectName -DestinationPath $zipPath -Force
} finally {
  Pop-Location
  if (Test-Path $stageRoot) { Remove-Item $stageRoot -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host "✅ Done:"
Write-Host "   - $zipPath"
Write-Host "   - $changelogPath"
