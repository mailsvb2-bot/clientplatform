param(
    [Parameter(Mandatory = $true)]
    [string]$PlanPath
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class ClientPlatformWin32 {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
"@

function Get-RequiredEnv([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "missing_environment:$Name"
    }
    return $value.Trim()
}

function Get-OptionalEnv([string]$Name, [string]$Default = "") {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Get-MainProcess([string]$ProcessName, [string]$ExeEnvName) {
    $process = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } |
        Select-Object -First 1
    if ($null -eq $process) {
        $exe = Get-RequiredEnv $ExeEnvName
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            throw "desktop_executable_missing:$ExeEnvName"
        }
        Start-Process -FilePath $exe | Out-Null
        $deadline = [DateTime]::UtcNow.AddSeconds(45)
        do {
            Start-Sleep -Milliseconds 500
            $process = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
                Where-Object { $_.MainWindowHandle -ne 0 } |
                Select-Object -First 1
        } while (($null -eq $process) -and ([DateTime]::UtcNow -lt $deadline))
    }
    if ($null -eq $process) {
        throw "desktop_window_missing:$ProcessName"
    }
    [ClientPlatformWin32]::ShowWindowAsync($process.MainWindowHandle, 9) | Out-Null
    [ClientPlatformWin32]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
    Start-Sleep -Milliseconds 500
    return $process
}

function Get-AutomationSnapshot([System.Diagnostics.Process]$Process) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($Process.MainWindowHandle)
    if ($null -eq $root) { throw "automation_root_missing" }
    $items = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $names = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $items.Count; $i++) {
        try {
            $name = $items.Item($i).Current.Name
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $names.Add(($name -replace "\s+", " ").Trim())
            }
        } catch { }
    }
    $joined = [string]::Join([Environment]::NewLine, $names)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($joined)
        $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
    return [pscustomobject]@{
        Count = $names.Count
        Hash = $hash
        Text = $joined
        HasClientPlatform = ($joined -match "(?i)ClientPlatform")
    }
}

function Send-ClipboardText([string]$Text) {
    $previous = $null
    try { $previous = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch { }
    try {
        Set-Clipboard -Value $Text
        [System.Windows.Forms.SendKeys]::SendWait("^v")
        Start-Sleep -Milliseconds 150
    } finally {
        if ($null -ne $previous) {
            try { Set-Clipboard -Value $previous } catch { }
        }
    }
}

function Open-Conversation(
    [System.Diagnostics.Process]$Process,
    [string]$Conversation
) {
    [ClientPlatformWin32]::SetForegroundWindow($Process.MainWindowHandle) | Out-Null
    [System.Windows.Forms.SendKeys]::SendWait("^k")
    Start-Sleep -Milliseconds 300
    Send-ClipboardText $Conversation
    Start-Sleep -Milliseconds 700
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    Start-Sleep -Seconds 1
}

function Focus-MessageEditor([System.Diagnostics.Process]$Process) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($Process.MainWindowHandle)
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    )
    $items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    for ($i = $items.Count - 1; $i -ge 0; $i--) {
        try {
            $element = $items.Item($i)
            if ($element.Current.IsEnabled -and -not $element.Current.IsOffscreen) {
                $element.SetFocus()
                Start-Sleep -Milliseconds 150
                return
            }
        } catch { }
    }
    throw "message_editor_not_found"
}

function Send-ChatMessage(
    [System.Diagnostics.Process]$Process,
    [string]$Text
) {
    [ClientPlatformWin32]::SetForegroundWindow($Process.MainWindowHandle) | Out-Null
    Focus-MessageEditor $Process
    Send-ClipboardText $Text
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
}

function Get-TextOccurrenceCount([string]$Text, [string]$Expected) {
    if ([string]::IsNullOrWhiteSpace($Expected)) { return 0 }
    $pattern = [Regex]::Escape($Expected)
    return [Regex]::Matches($Text, $pattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase).Count
}

function Wait-For-Response(
    [System.Diagnostics.Process]$Process,
    [object]$Before,
    [string]$Expected
) {
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $beforeExpected = Get-TextOccurrenceCount $Before.Text $Expected
    $last = Get-AutomationSnapshot $Process
    do {
        Start-Sleep -Milliseconds 500
        $last = Get-AutomationSnapshot $Process
        $afterExpected = Get-TextOccurrenceCount $last.Text $Expected
        if (
            $last.Hash -ne $Before.Hash -and
            $afterExpected -gt $beforeExpected
        ) {
            return $last
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "provider_response_assertion_timeout expected=$Expected"
}

function Save-Screenshot([string]$Path) {
    $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Open-BrowserSurface([string]$Browser, [string]$Url) {
    $exeName = if ($Browser -eq "edge") { "CLIENTPLATFORM_E2E_EDGE_EXE" } else { "CLIENTPLATFORM_E2E_CHROME_EXE" }
    $processName = if ($Browser -eq "edge") { "msedge" } else { "chrome" }
    $exe = Get-RequiredEnv $exeName
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "browser_executable_missing:$exeName"
    }
    Start-Process -FilePath $exe -ArgumentList @("--new-window", $Url) | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $process = $null
    do {
        Start-Sleep -Milliseconds 500
        $process = Get-Process -Name $processName -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 } |
            Sort-Object StartTime -Descending |
            Select-Object -First 1
    } while (($null -eq $process) -and ([DateTime]::UtcNow -lt $deadline))
    if ($null -eq $process) { throw "browser_window_missing:$Browser" }
    [ClientPlatformWin32]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
    Start-Sleep -Seconds 3
    return $process
}

$plan = Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
$evidenceDir = [IO.Path]::GetFullPath([string]$plan.evidence_dir)
New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null
$channel = [string]$plan.channel
$journey = [string]$plan.journey_id
$results = @()

if ($channel -eq "telegram") {
    $processName = Get-OptionalEnv "CLIENTPLATFORM_E2E_TELEGRAM_PROCESS" "Telegram"
    $process = Get-MainProcess $processName "CLIENTPLATFORM_E2E_TELEGRAM_EXE"
    Open-Conversation $process (Get-RequiredEnv "CLIENTPLATFORM_E2E_TELEGRAM_CHAT")
    foreach ($probe in $plan.probes) {
        $before = Get-AutomationSnapshot $process
        $message = [string]$probe.input
        $expected = [string]$probe.expect
        Send-ChatMessage $process $message
        $after = Wait-For-Response $process $before $expected
        $results += [pscustomobject]@{
            probe = [string]$probe.id
            input = $message
            expected = $expected
            before = $before.Count
            after = $after.Count
            hash = $after.Hash
        }
    }
}
elseif ($channel -eq "max") {
    $processName = Get-OptionalEnv "CLIENTPLATFORM_E2E_MAX_PROCESS" "MAX"
    $process = Get-MainProcess $processName "CLIENTPLATFORM_E2E_MAX_EXE"
    Open-Conversation $process (Get-RequiredEnv "CLIENTPLATFORM_E2E_MAX_CHAT")
    foreach ($probe in $plan.probes) {
        $before = Get-AutomationSnapshot $process
        $message = [string]$probe.input
        $expected = [string]$probe.expect
        Send-ChatMessage $process $message
        $after = Wait-For-Response $process $before $expected
        $results += [pscustomobject]@{
            probe = [string]$probe.id
            input = $message
            expected = $expected
            before = $before.Count
            after = $after.Count
            hash = $after.Hash
        }
    }
}
elseif ($channel -eq "vk") {
    $browser = (Get-OptionalEnv "CLIENTPLATFORM_E2E_VK_BROWSER" "edge").ToLowerInvariant()
    if ($browser -notin @("edge", "chrome")) { throw "unsupported_vk_browser" }
    $process = Open-BrowserSurface $browser (Get-RequiredEnv "CLIENTPLATFORM_E2E_VK_CHAT_URL")
    foreach ($probe in $plan.probes) {
        $before = Get-AutomationSnapshot $process
        Focus-MessageEditor $process
        $message = [string]$probe.input
        $expected = [string]$probe.expect
        Send-ClipboardText $message
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
        $after = Wait-For-Response $process $before $expected
        $results += [pscustomobject]@{
            probe = [string]$probe.id
            input = $message
            expected = $expected
            before = $before.Count
            after = $after.Count
            hash = $after.Hash
        }
    }
}
elseif ($channel -eq "cockpit_edge") {
    $process = Open-BrowserSurface "edge" (Get-RequiredEnv "CLIENTPLATFORM_E2E_COCKPIT_URL")
    $snapshot = Get-AutomationSnapshot $process
    $expected = [string]$plan.probes[0].expect
    if ((Get-TextOccurrenceCount $snapshot.Text $expected) -lt 1) {
        throw "cockpit_expected_text_missing expected=$expected"
    }
    $results += [pscustomobject]@{ probe = [string]$plan.probes[0].id; input = "open"; expected = $expected; before = 0; after = $snapshot.Count; hash = $snapshot.Hash }
}
elseif ($channel -eq "cockpit_chrome") {
    $process = Open-BrowserSurface "chrome" (Get-RequiredEnv "CLIENTPLATFORM_E2E_COCKPIT_URL")
    $snapshot = Get-AutomationSnapshot $process
    $expected = [string]$plan.probes[0].expect
    if ((Get-TextOccurrenceCount $snapshot.Text $expected) -lt 1) {
        throw "cockpit_expected_text_missing expected=$expected"
    }
    $results += [pscustomobject]@{ probe = [string]$plan.probes[0].id; input = "open"; expected = $expected; before = 0; after = $snapshot.Count; hash = $snapshot.Hash }
}
else {
    throw "unsupported_channel:$channel"
}

$safeName = ($journey + "-" + $channel) -replace "[^a-zA-Z0-9_.-]", "_"
$screenshot = Join-Path $evidenceDir ($safeName + ".png")
Save-Screenshot $screenshot
$resultPath = Join-Path $evidenceDir ($safeName + ".json")
[pscustomobject]@{
    journey_id = $journey
    channel = $channel
    status = "ok"
    actions = $results
    screenshot = [IO.Path]::GetFileName($screenshot)
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resultPath -Encoding UTF8

Write-Output "CLIENTPLATFORM_DESKTOP_PROBE_OK journey=$journey channel=$channel asserted_probes=$($results.Count)"
