$ErrorActionPreference = 'Stop'
$runtimeDir = Join-Path $PSScriptRoot 'runtime'
$toolsDir = Join-Path $PSScriptRoot 'tools'
if (Test-Path -LiteralPath (Join-Path $toolsDir 'uv.exe') -PathType Leaf) {
    $env:PATH = "$toolsDir;$env:PATH"
}
$commit = 'd288ef4df8a42f6286c5506b7185f64430071490'
$sourceDir = Join-Path $runtimeDir "LarkAgentX-runtime\LarkAgentX-$commit"
$astrbotPython = Join-Path $runtimeDir '.venv\Scripts\python.exe'
$archive = Join-Path $runtimeDir "LarkAgentX-$commit.zip"
$download = Join-Path $runtimeDir 'LarkAgentX.download.zip'
$sha256 = 'A3033114963D98E6A1FCD8D0904ADE8F4DEB3ED34C0E2B2637986762C85F1F1B'
$url = "https://codeload.github.com/cv-cat/LarkAgentX/zip/$commit"

if (-not (Test-Path -LiteralPath $astrbotPython -PathType Leaf)) {
    throw "AstrBot Python environment not found: $astrbotPython"
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceDir 'pyproject.toml') -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        curl.exe -fL --retry 2 --connect-timeout 15 --max-time 180 -o $download $url
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Move-Item -LiteralPath $download -Destination $archive
    }
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($actual -ne $sha256) { throw "LarkAgentX archive hash mismatch: $actual" }

    $tempDir = Join-Path $runtimeDir 'LarkAgentX-extract-pinned-commit'
    if (Test-Path -LiteralPath $tempDir) { throw "Extraction directory exists: $tempDir" }
    Expand-Archive -LiteralPath $archive -DestinationPath $tempDir
    $sourceRoot = Get-ChildItem -LiteralPath $tempDir -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } | Select-Object -First 1
    if (-not $sourceRoot) { throw 'Pinned LarkAgentX archive did not contain pyproject.toml' }
    New-Item -ItemType Directory -Force -Path (Split-Path $sourceDir -Parent) | Out-Null
    if (Test-Path -LiteralPath $sourceDir) { throw "Source directory exists without project metadata: $sourceDir" }
    Move-Item -LiteralPath $sourceRoot.FullName -Destination $sourceDir
}

uv pip install --python $astrbotPython 'protobuf==5.29.3' 'protobuf3-to-dict==0.1.5'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$env:PYTHONPATH = $sourceDir
& $astrbotPython -c "import larkx; from larkx.auth import LarkAuth, QrLogin; from larkx.client import LarkClient; print('LarkAgentX', larkx.__version__, 'import OK')"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "LarkAgentX pinned source ready: $sourceDir"
