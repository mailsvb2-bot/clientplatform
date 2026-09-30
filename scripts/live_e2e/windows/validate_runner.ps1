$ErrorActionPreference = "Stop"

$os = Get-CimInstance Win32_OperatingSystem
if ($null -eq $os) {
    throw "windows_operating_system_unavailable"
}
$caption = [string]$os.Caption
$architecture = [string]$os.OSArchitecture
if ($caption -notmatch "(?i)Windows 11") {
    throw "windows_11_required: $caption"
}
if ($architecture -notmatch "(?i)64") {
    throw "windows_x64_required: $architecture"
}

$current = Get-Process -Id $PID
if ($null -eq $current -or [int]$current.SessionId -le 0) {
    throw "interactive_user_session_required"
}

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
    if ([string]::IsNullOrWhiteSpace($path)) {
        throw "missing_environment:$($entry.Key)"
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "desktop_application_missing:$($entry.Key)"
    }
}

$runner = Get-Process -Name "Runner.Listener" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $runner) {
    throw "github_actions_runner_not_running"
}
if ([int]$runner.SessionId -le 0) {
    throw "github_actions_runner_must_not_run_as_service"
}

Write-Output (
    "CLIENTPLATFORM_WINDOWS_11_RUNNER_OK " +
    "caption=" + ($caption -replace "\s+", "_") + " " +
    "arch=" + ($architecture -replace "\s+", "_") + " " +
    "session=" + $sessionName
)
