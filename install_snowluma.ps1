$ErrorActionPreference = 'Stop'
$runtimeDir = Join-Path $PSScriptRoot 'runtime\SnowLuma'
$archive = Join-Path $runtimeDir 'SnowLuma-v2.4.2-win-x64.zip'
$destination = Join-Path $runtimeDir 'app'
$downloadUrl = 'https://github.com/TomyJan/SnowLuma/releases/download/v2.4.2/SnowLuma-v2.4.2-win-x64.zip'
$expectedHash = '0912926c5e73260f35b000278a8658493fc9452f36782c32fa86f0e825bad81a'

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    $download = Join-Path $runtimeDir 'SnowLuma.download.zip'
    curl.exe -fL --retry 2 -o $download $downloadUrl
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $downloadHash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($downloadHash -ne $expectedHash) {
        throw "SnowLuma SHA-256 mismatch. expected=$expectedHash actual=$downloadHash"
    }
    Move-Item -LiteralPath $download -Destination $archive
}

$actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "SnowLuma archive SHA-256 mismatch. expected=$expectedHash actual=$actualHash"
}

if (-not (Test-Path -LiteralPath (Join-Path $destination 'launcher.bat') -PathType Leaf)) {
    if (Test-Path -LiteralPath $destination) {
        throw "SnowLuma extraction directory exists without launcher.bat: $destination"
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $destination
}
if (-not (Test-Path -LiteralPath (Join-Path $destination 'node.exe') -PathType Leaf)) {
    throw "SnowLuma package is incomplete: $destination"
}
Write-Output "SnowLuma v2.4.2 verified and installed at $destination"
