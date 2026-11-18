$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimeDir = Join-Path $projectRoot 'runtime'
$astrbotRoot = Join-Path $runtimeDir 'AstrBot'
$toolsDir = Join-Path $PSScriptRoot 'tools'
if (Test-Path -LiteralPath (Join-Path $toolsDir 'uv.exe') -PathType Leaf) {
    $env:PATH = "$toolsDir;$env:PATH"
}
$venvPython = Join-Path $runtimeDir '.venv\Scripts\python.exe'
$astrbotCommand = Join-Path $runtimeDir '.venv\Scripts\astrbot.exe'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'AstrBot 需要 uv。请先安装 uv，再重新运行此脚本。'
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    uv venv --python 3.12 (Join-Path $runtimeDir '.venv')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$environmentReady = $false
if ((Test-Path -LiteralPath $venvPython -PathType Leaf) -and (Test-Path -LiteralPath $astrbotCommand -PathType Leaf)) {
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $venvPython -c "import astrbot, psutil, aiohttp, cryptography, win32crypt"
        $environmentReady = $LASTEXITCODE -eq 0
    }
    finally { $ErrorActionPreference = $previousErrorPreference }
}
if (-not $environmentReady) {
    uv pip install --python $venvPython 'astrbot==4.29.4'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

New-Item -ItemType Directory -Force -Path $astrbotRoot | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $astrbotRoot '.astrbot') -PathType Leaf)) {
    Push-Location $astrbotRoot
    try {
        & $astrbotCommand init --yes
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    finally { Pop-Location }
}

& (Join-Path $projectRoot 'install.ps1') -AstrBotRoot $astrbotRoot -Replace
if ($LASTEXITCODE -ne 0) { throw "Bridge plugin setup failed ($LASTEXITCODE)." }
Write-Output "AstrBot 4.29.4 and 搬运工V2 are ready under $astrbotRoot"
