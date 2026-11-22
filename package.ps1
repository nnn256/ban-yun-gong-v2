param([string]$OutputPath = (Join-Path $PSScriptRoot 'artifacts/搬运工V2.zip'))

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'installer/build-installer.ps1') -PortableOnly -PortablePath $OutputPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
