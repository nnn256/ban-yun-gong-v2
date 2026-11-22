param(
    [string]$AstrBotRoot = (Join-Path $PSScriptRoot 'runtime\AstrBot'),
    [switch]$Replace,
    [string]$ResultPath
)

$ErrorActionPreference = 'Stop'
function Get-PluginHash([string]$Path) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)) }
    finally { $stream.Dispose(); $hasher.Dispose() }
}
function Write-PluginJson([string]$Path, [object]$Value) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($fullPath)) | Out-Null
    $temporary = $fullPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 8 -Compress), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $fullPath -PathType Leaf) { [IO.File]::Replace($temporary, $fullPath, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $fullPath) }
    }
    finally { if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force } }
}
if (-not (Test-Path -LiteralPath (Join-Path $AstrBotRoot '.astrbot') -PathType Leaf)) {
    $defaultRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'runtime\AstrBot'))
    if ([IO.Path]::GetFullPath($AstrBotRoot) -ne $defaultRoot) {
        throw 'The custom AstrBot directory is not initialized. Run astrbot init in that directory first.'
    }
    & (Join-Path $PSScriptRoot 'install_astrbot.ps1') -ResultPath $ResultPath
    if ($LASTEXITCODE -ne 0) { throw "AstrBot initialization failed ($LASTEXITCODE)." }
    return
}
$resolvedRoot = (Resolve-Path -LiteralPath $AstrBotRoot).Path
$pluginsDir = Join-Path $resolvedRoot 'data\plugins'
$source = Join-Path $PSScriptRoot 'astrbot_plugin_message_bridge'
$destination = Join-Path $pluginsDir 'astrbot_plugin_message_bridge'

if (-not (Test-Path -LiteralPath $pluginsDir -PathType Container)) {
    throw "AstrBot plugin folder not found: $pluginsDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $source 'metadata.yaml') -PathType Leaf)) {
    throw "Plugin source is incomplete: $source"
}
$pluginFiles = @(
    '__init__.py',
    'build_identity.py',
    'bridge_core.py',
    'bridge_rules.example.json',
    'larkx_bridge.py',
    'larkx_inbox.py',
    'larkx_history.py',
    'larkx_receive.py',
    'larkx_media.py',
    'larkx_merge.py',
    'larkx_richtext.py',
    'qq_emojis.py',
    'qq_transport.py',
    'runtime_status.py',
    'snowluma_watchdog.py',
    'event_log.py',
    'main.py',
    'metadata.yaml'
)
# Compare before copying: a later prepare pass sees matching files even while
# AstrBot still has the old Python modules loaded. Missing files count too.
foreach ($file in $pluginFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)) {
        throw "Plugin source is incomplete; missing file: $file"
    }
}
$changedFiles = @($pluginFiles | Where-Object {
    $target = Join-Path $destination $_
    -not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-PluginHash (Join-Path $source $_)) -ne (Get-PluginHash $target)
})
$pendingPath = Join-Path $resolvedRoot 'data\plugin_data\astrbot_plugin_message_bridge\source-restart.pending.json'
if ($changedFiles.Count -gt 0) {
    # Commit the restart obligation before any file is overwritten. A failed
    # setup or PrepareOnly must not erase it on the next, now-equal comparison.
    Write-PluginJson $pendingPath @{
        schema = 1; generation = [guid]::NewGuid().ToString('N')
        astrbot_root = $resolvedRoot; changed_files = @($changedFiles)
    }
    if (Test-Path -LiteralPath $destination -PathType Container) {
        $backup = Join-Path $resolvedRoot ('data\plugin_data\astrbot_plugin_message_bridge\source-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        Get-ChildItem -LiteralPath $destination -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $backup }
    }
}
New-Item -ItemType Directory -Force -Path $destination | Out-Null
foreach ($file in $pluginFiles) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $destination -Force
}
if ($ResultPath) {
    Write-PluginJson $ResultPath @{
        astrbot_root = $resolvedRoot; changed = ($changedFiles.Count -gt 0)
        changed_files = @($changedFiles); restart_pending = (Test-Path -LiteralPath $pendingPath -PathType Leaf)
    }
}
Write-Output "Installed plugin: $destination"
Write-Output 'Reload AstrBot plugin astrbot_plugin_message_bridge to create its local rules file.'
