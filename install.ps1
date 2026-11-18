param(
    [string]$AstrBotRoot = (Join-Path $PSScriptRoot 'runtime\AstrBot'),
    [switch]$Replace
)

$ErrorActionPreference = 'Stop'
function Get-PluginHash([string]$Path) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)) }
    finally { $stream.Dispose(); $hasher.Dispose() }
}
if (-not (Test-Path -LiteralPath (Join-Path $AstrBotRoot '.astrbot') -PathType Leaf)) {
    $defaultRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'runtime\AstrBot'))
    if ([IO.Path]::GetFullPath($AstrBotRoot) -ne $defaultRoot) {
        throw 'The custom AstrBot directory is not initialized. Run astrbot init in that directory first.'
    }
    & (Join-Path $PSScriptRoot 'install_astrbot.ps1')
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
if (Test-Path -LiteralPath $destination) {
    $existingChanged = Get-ChildItem -LiteralPath $source -File | Where-Object {
        $target = Join-Path $destination $_.Name
        (Test-Path -LiteralPath $target -PathType Leaf) -and (Get-PluginHash $_.FullName) -ne (Get-PluginHash $target)
    }
    if ($existingChanged) {
        $backup = Join-Path $resolvedRoot ('data\plugin_data\astrbot_plugin_message_bridge\source-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        Get-ChildItem -LiteralPath $destination -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $backup }
    }
}

New-Item -ItemType Directory -Force -Path $destination | Out-Null
$pluginFiles = @(
    '__init__.py',
    'bridge_core.py',
    'bridge_rules.example.json',
    'larkx_bridge.py',
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
foreach ($file in $pluginFiles) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $destination -Force
}
Write-Output "Installed plugin: $destination"
Write-Output 'Reload AstrBot plugin astrbot_plugin_message_bridge to create its local rules file.'
