$ErrorActionPreference = "Stop"

$expectedOs = [Environment]::GetEnvironmentVariable("CLIENTPLATFORM_E2E_EXPECTED_WINDOWS")
if ([string]::IsNullOrWhiteSpace($expectedOs)) {
    throw "missing_environment:CLIENTPLATFORM_E2E_EXPECTED_WINDOWS"
}

$manifestPath = Join-Path $PSScriptRoot "..\..\..\config\live_e2e_manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$variants = @($manifest.runner.os_variants | Where-Object { [string]$_.id -eq $expectedOs })
if ($variants.Count -ne 1) {
    throw "unsupported_windows_variant:$expectedOs"
}
$variant = $variants[0]

$os = Get-CimInstance Win32_OperatingSystem
if ($null -eq $os) { throw "windows_operating_system_unavailable" }
$caption = [string]$os.Caption
$architecture = [string]$os.OSArchitecture
$buildNumber = 0
if (-not [int]::TryParse([string]$os.BuildNumber, [ref]$buildNumber)) {
    throw "windows_build_unavailable:$($os.BuildNumber)"
}
$captionPattern = [string]$variant.caption_pattern
if ($caption -notmatch [regex]::Escape($captionPattern)) {
    throw "windows_variant_mismatch:expected=$expectedOs actual=$caption"
}
$minimumBuild = [int]$variant.minimum_build
if ($buildNumber -lt $minimumBuild) {
    throw "windows_build_too_old:expected=$expectedOs minimum=$minimumBuild actual=$buildNumber"
}
if ($architecture -notmatch "(?i)64") { throw "windows_x64_required: $architecture" }

$current = Get-Process -Id $PID
if ($null -eq $current -or [int]$current.SessionId -le 0) { throw "interactive_user_session_required" }
$sessionName = [Environment]::GetEnvironmentVariable("SESSIONNAME")
if ([string]::IsNullOrWhiteSpace($sessionName) -or $sessionName -match "^(?i:services?)$") {
    throw "interactive_windows_session_required"
}

$required = @{
    "CLIENTPLATFORM_E2E_TELEGRAM_EXE" = "Telegram Desktop"
    "CLIENTPLATFORM_E2E_MAX_EXE" = "MAX Desktop"
    "CLIENTPLATFORM_E2E_EDGE_EXE" = "Microsoft Edge"
    "CLIENTPLATFORM_E2E_CHROME_EXE" = "Google Chrome"
}
foreach ($entry in $required.GetEnumerator()) {
    $path = [Environment]::GetEnvironmentVariable($entry.Key)
    if ([string]::IsNullOrWhiteSpace($path)) { throw "missing_environment:$($entry.Key)" }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "desktop_application_missing:$($entry.Key)" }
}

$runner = Get-Process -Name "Runner.Listener" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $runner) { throw "github_actions_runner_not_running" }
if ([int]$runner.SessionId -le 0) { throw "github_actions_runner_must_not_run_as_service" }

$identityPath = [Environment]::GetEnvironmentVariable("CLIENTPLATFORM_E2E_RUNNER_IDENTITY")
if ([string]::IsNullOrWhiteSpace($identityPath)) {
    throw "missing_environment:CLIENTPLATFORM_E2E_RUNNER_IDENTITY"
}
$identityDirectory = Split-Path -Parent $identityPath
if (-not [string]::IsNullOrWhiteSpace($identityDirectory)) {
    New-Item -ItemType Directory -Force -Path $identityDirectory | Out-Null
}
[ordered]@{
    os_id = $expectedOs
    caption = $caption
    build = $buildNumber
    architecture = $architecture
    session = $sessionName
} | ConvertTo-Json | Set-Content -LiteralPath $identityPath -Encoding UTF8

Write-Output (
    "CLIENTPLATFORM_WINDOWS_RUNNER_OK " +
    "os=" + $expectedOs + " " +
    "caption=" + ($caption -replace "\s+", "_") + " " +
    "build=" + $buildNumber + " " +
    "arch=" + ($architecture -replace "\s+", "_") + " " +
    "session=" + $sessionName
)
