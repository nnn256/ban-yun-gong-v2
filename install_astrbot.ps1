param([string]$ResultPath)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimeDir = Join-Path $projectRoot 'runtime'
$astrbotRoot = Join-Path $runtimeDir 'AstrBot'
$toolsDir = Join-Path $PSScriptRoot 'tools'
if (Test-Path -LiteralPath (Join-Path $toolsDir 'uv.exe') -PathType Leaf) {
    $env:PATH = "$toolsDir;$env:PATH"
}
. (Join-Path $projectRoot 'python-runtime.ps1')
$venvPython = Initialize-MoverApplicationPython $projectRoot

$probeArgs = @('-I','-B','-c','from astrbot.cli.__main__ import cli; import psutil, aiohttp, cryptography, win32crypt')
$dependencies = Invoke-MoverRuntimeCommand -Executable $venvPython -Arguments $probeArgs -TimeoutMilliseconds 30000
if ($dependencies.ExitCode -ne 0) {
    $uv = Get-MoverUv $projectRoot
    & $uv pip install --python $venvPython 'astrbot==4.29.4' --reinstall-package astrbot --reinstall-package pywin32
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $dependencies = Invoke-MoverRuntimeCommand -Executable $venvPython -Arguments $probeArgs -TimeoutMilliseconds 30000
    if ($dependencies.ExitCode -ne 0) { throw 'AstrBot dependencies still cannot load. Saved logins and rules were retained.' }
}

New-Item -ItemType Directory -Force -Path $astrbotRoot | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $astrbotRoot '.astrbot') -PathType Leaf)) {
    Push-Location $astrbotRoot
    try {
        & $venvPython -I -B -m astrbot.cli init --yes
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    finally { Pop-Location }
}

& (Join-Path $projectRoot 'install.ps1') -AstrBotRoot $astrbotRoot -Replace -ResultPath $ResultPath
Write-Output "AstrBot 4.29.4 and 搬运工V2 are ready under $astrbotRoot"
# The PowerShell-only plugin installer throws on failure and leaves LASTEXITCODE unchanged.
exit 0
