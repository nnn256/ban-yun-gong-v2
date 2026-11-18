[CmdletBinding()]
param(
    [string]$AstrBotRoot = (Join-Path $PSScriptRoot 'runtime\AstrBot'),
    [string]$ArchivePath
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$astrbotPath = [IO.Path]::GetFullPath($AstrBotRoot)
$pluginData = Join-Path $astrbotPath 'data\plugin_data\pulids_snowluma_go_to_astrbot'
$destination = Join-Path $pluginData 'snowluma'
$cacheDir = Join-Path $projectRoot 'runtime\SnowLuma'
$archiveName = 'SnowLuma-v2.4.2-win-x64.zip'
$expectedHash = '0912926c5e73260f35b000278a8658493fc9452f36782c32fa86f0e825bad81a'
$downloadUrl = 'https://github.com/TomyJan/SnowLuma/releases/download/v2.4.2/SnowLuma-v2.4.2-win-x64.zip'
$requiredFiles = @('node.exe', 'index.mjs', 'launcher.bat', 'release.json',
    'native\snowluma-win32-x64.dll', 'native\snowluma-win32-x64.node',
    'native\websocket-win32-x64.node', 'native\ffmpeg\ffmpegAddon.win32.x64.node')

function Test-SnowLumaPackage([string]$Directory) {
    foreach ($relative in $requiredFiles) {
        $candidate = Join-Path $Directory $relative
        if (-not [IO.File]::Exists($candidate) -or ([IO.FileInfo]$candidate).Length -eq 0) { return $false }
    }
    try {
        $release = [IO.File]::ReadAllText((Join-Path $Directory 'release.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
        return $release.version -eq '2.4.2' -and $release.target -eq 'win32-x64'
    } catch { return $false }
}

function Get-ArchiveHash([string]$Path) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hasher.Dispose() }
}

if (Test-SnowLumaPackage $destination) {
    Write-Output "SnowLuma v2.4.2 is already complete at $destination; configuration and data preserved."
    exit 0
}
if (Test-Path -LiteralPath $destination) {
    throw "SnowLuma target exists but is incomplete or a different version; no files were changed: $destination"
}

$archive = $null
if ($ArchivePath) {
    $archive = [IO.Path]::GetFullPath($ArchivePath)
    if (-not [IO.File]::Exists($archive)) { throw "SnowLuma archive not found: $archive" }
} else {
    foreach ($candidate in @((Join-Path $cacheDir $archiveName), (Join-Path $projectRoot ('downloads\' + $archiveName)))) {
        if ([IO.File]::Exists($candidate)) { $archive = $candidate; break }
    }
}

if (-not $archive) {
    New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
    $download = Join-Path $cacheDir ('SnowLuma.download.' + [Guid]::NewGuid().ToString('N') + '.zip')
    & curl.exe -fL --retry 2 --connect-timeout 15 --max-time 180 -o $download $downloadUrl
    if ($LASTEXITCODE -ne 0) { throw "SnowLuma download failed; retry uses a new temporary file. Partial file: $download" }
    if ((Get-ArchiveHash $download) -ne $expectedHash) { throw "SnowLuma download SHA-256 mismatch; no extraction. File: $download" }
    $archive = Join-Path $cacheDir $archiveName
    if (Test-Path -LiteralPath $archive) { throw "SnowLuma archive appeared during download; no cache was overwritten: $archive" }
    Move-Item -LiteralPath $download -Destination $archive
}
if ((Get-ArchiveHash $archive) -ne $expectedHash) { throw "SnowLuma archive SHA-256 mismatch; no extraction: $archive" }

New-Item -ItemType Directory -Force -Path $pluginData | Out-Null
$stage = Join-Path $pluginData ('.snowluma-extract-' + [Guid]::NewGuid().ToString('N'))
$stageFull = [IO.Path]::GetFullPath($stage)
$stagePrefix = $stageFull.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
New-Item -ItemType Directory -Path $stageFull | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archive)
try {
    foreach ($entry in $zip.Entries) {
        $target = [IO.Path]::GetFullPath((Join-Path $stageFull $entry.FullName))
        if (-not $target.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'SnowLuma archive contains a path outside its extraction directory.'
        }
    }
} finally { $zip.Dispose() }

# A retry always has an isolated staging directory; account data is never merged.
try {
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stageFull)
    if (-not (Test-SnowLumaPackage $stageFull)) { throw 'SnowLuma package validation failed after extraction.' }
    if (Test-Path -LiteralPath $destination) { throw 'SnowLuma destination appeared during extraction; it was not overwritten.' }
    Move-Item -LiteralPath $stageFull -Destination $destination
} catch {
    throw "SnowLuma installation was not published. Recovery staging directory: $stageFull. $($_.Exception.Message)"
}
Write-Output "SnowLuma v2.4.2 verified and installed at $destination. No program was launched."
