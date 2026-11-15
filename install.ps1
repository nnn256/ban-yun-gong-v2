param(
    [Parameter(Mandatory = $true)]
    [string]$AstrBotRoot,
    [switch]$Replace
)

$ErrorActionPreference = 'Stop'
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
if ((Test-Path -LiteralPath $destination) -and -not $Replace) {
    throw "Plugin already exists at $destination. Re-run with -Replace after backing up local changes."
}

New-Item -ItemType Directory -Force -Path $destination | Out-Null
$pluginFiles = @(
    '__init__.py',
    'bridge_core.py',
    'bridge_rules.example.json',
    'larkx_bridge.py',
    'larkx_media.py',
    'qq_emojis.py',
    'qq_transport.py',
    'runtime_status.py',
    'main.py',
    'metadata.yaml'
)
foreach ($file in $pluginFiles) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $destination -Force
}
Write-Output "Installed plugin: $destination"
Write-Output 'Reload AstrBot plugin astrbot_plugin_message_bridge to create its local rules file.'
