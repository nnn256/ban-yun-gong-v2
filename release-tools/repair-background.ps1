param(
    [Parameter(Mandatory=$true)][string]$ArchivePath,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [int]$Port = 18766
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
$repairVersion = [version]'2.0.9'
$baseUrl = "http://127.0.0.1:$Port"

function Add-RepairLog([string]$Line) {
    [IO.File]::AppendAllText($LogPath, $Line + [Environment]::NewLine, $utf8)
}

function Get-RunningWorkbench {
    $status = Invoke-RestMethod -Uri "$baseUrl/api/status" -MaximumRedirection 0 -TimeoutSec 5
    $root = [string]$status.workbench_root
    if ($root -notmatch '^[a-zA-Z]:[\\/]' -or $status.version -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') {
        throw '当前端口不是可识别的搬运工V2工作台。'
    }
    $root = [IO.Path]::GetFullPath($root).TrimEnd('\')
    if (-not (Test-Path -LiteralPath (Join-Path $root 'start-workbench.ps1') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $root 'workbench_server.py') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $root 'astrbot_plugin_message_bridge\metadata.yaml') -PathType Leaf) -or
        (Get-Content -LiteralPath (Join-Path $root 'astrbot_plugin_message_bridge\metadata.yaml') -Raw) -notmatch '(?m)^name:\s*astrbot_plugin_message_bridge\s*$') {
        throw '找不到这套工作台对应的安装文件。'
    }
    return @{ root = $root; version = [version]$status.version; background = $status.background }
}

function Confirm-BackgroundSave($Current) {
    # Reuse the saved choices, including an existing opt-out.
    $values = @{}
    foreach ($name in @('enabled', 'keep_awake', 'autostart')) {
        if ($Current.background.$name -isnot [bool]) { throw '工作台返回的后台设置不完整。' }
        $values[$name] = $Current.background.$name
    }
    $body = $values | ConvertTo-Json -Compress
    $saved = Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/api/background/config" -Method Post `
        -Body $body -ContentType 'application/json' -MaximumRedirection 0 -TimeoutSec 20
    if ([int]$saved.StatusCode -ne 200) { throw ('保存返回 HTTP ' + $saved.StatusCode) }
    $reloaded = Get-RunningWorkbench
    if ($reloaded.root -ne $Current.root -or $reloaded.version -ne $Current.version) {
        throw '保存检查期间工作台发生变化，请重试。'
    }
    foreach ($name in $values.Keys) {
        if ($reloaded.background.$name -isnot [bool] -or $reloaded.background.$name -ne $values[$name]) {
            throw '重新读取的后台设置与保存值不同。'
        }
    }
    Add-RepairLog 'POST /api/background/config: HTTP 200; saved preferences match the next status response.'
}

$userMessage = "未能识别端口 $Port 上的工作台。请在报错的电脑上先打开原工作台，再双击本修复程序。"
try {
    $current = Get-RunningWorkbench
    Add-RepairLog ("Running version {0}; installation {1}; port {2}." -f $current.version, $current.root, $Port)
    $needsUpdate = $current.version -lt $repairVersion
    if (-not $needsUpdate) {
        $userMessage = '已经找到工作台，但后台设置仍未通过保存检查。请查看本次修复日志。'
        try { Confirm-BackgroundSave $current }
        catch {
            if ($current.version -eq $repairVersion -and $_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) {
                Add-RepairLog 'Current version reports 2.0.9 but the save route returns HTTP 404; reapplying the package.'
                $needsUpdate = $true
            }
            else { throw }
        }
    }

    if ($needsUpdate) {
        $userMessage = '未能完成原目录更新。请查看本次修复日志。'
        $payload = Join-Path (Split-Path -Parent $ArchivePath) 'payload'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $payload)
        $env:MOVER_WORKBENCH_PORT = [string]$Port
        $updateScript = Join-Path $payload 'update-and-start.ps1'
        $updateArguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
            $updateScript, '-TargetRoot', $current.root, '-NoBrowser', '-SkipDesktopActions')
        try {
            # Native installers can write normal progress to stderr. Log it without promoting it
            # to a failure in this PowerShell process; the updater owns its own error handling.
            $ErrorActionPreference = 'Continue'
            & (Join-Path $PSHOME 'powershell.exe') @updateArguments 2>&1 | ForEach-Object { Add-RepairLog ([string]$_) }
            $updateExit = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = 'Stop' }
        if ($updateExit -ne 0) { throw "Updater exited with code $updateExit." }

        $userMessage = '新版安装后没有通过后台设置保存检查。请查看本次修复日志。'
        $updated = Get-RunningWorkbench
        if ($updated.root -ne $current.root -or $updated.version -ne $repairVersion) {
            throw "需要原安装目录实际运行 $repairVersion，检测到 $($updated.version)。"
        }
        Confirm-BackgroundSave $updated
        $current = $updated
    }
    [IO.File]::WriteAllText($ResultPath, [string]$current.version, $utf8)
    exit 0
}
catch {
    Add-RepairLog ('Repair stopped: ' + $_.Exception.Message)
    if ($_.Exception.Response -is [Net.HttpWebResponse]) {
        Add-RepairLog ('HTTP status: ' + [int]$_.Exception.Response.StatusCode)
    }
    [IO.File]::WriteAllText($ResultPath, $userMessage, $utf8)
    exit 1
}
