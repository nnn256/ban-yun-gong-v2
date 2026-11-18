param(
    [string]$ArchivePath,
    [string]$OutputExe
)
$ErrorActionPreference = 'Stop'
$releaseFolder = Join-Path (Split-Path -Parent $PSScriptRoot) 'artifacts\release-v2.0.9'
# Use only the published archive that already passed the manifest and isolated-upgrade checks.
$expectedHash = 'CF156356E1FA0C1FC3FD0BF3AF0FF4A8CD951E7846CC7E39D091DD2207CC77FF'
if (-not $ArchivePath) { $ArchivePath = Join-Path $releaseFolder (([char]0x642C).ToString()+[char]0x8FD0+[char]0x5DE5+'V2-2.0.9.zip') }
if (-not $OutputExe) { $OutputExe = Join-Path $releaseFolder 'MoverV2-Background-Repair.exe' }
$ArchivePath = [IO.Path]::GetFullPath($ArchivePath)
$OutputExe = [IO.Path]::GetFullPath($OutputExe)
if ((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash -ne $expectedHash) { throw 'Unexpected 2.0.9 ZIP checksum.' }

$compiler = @('Framework64','Framework') | ForEach-Object { Join-Path $env:WINDIR "Microsoft.NET\$_\v4.0.30319\csc.exe" } |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $compiler) { throw 'Windows .NET Framework C# compiler not found.' }
$hashFile = [IO.Path]::GetTempFileName()
try {
    [IO.File]::WriteAllText($hashFile, $expectedHash, [Text.Encoding]::ASCII)
    $source = Join-Path $PSScriptRoot 'background_repair.cs'
    $script = Join-Path $PSScriptRoot 'repair-background.ps1'
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseInput([IO.File]::ReadAllText($script), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw $errors[0].Message }
    & $compiler /nologo /codepage:65001 /target:winexe /optimize+ /r:System.Windows.Forms.dll /r:System.Drawing.dll `
        "/resource:$ArchivePath,release.zip" "/resource:$script,repair.ps1" "/resource:$hashFile,release.sha256" "/out:$OutputExe" $source
    if ($LASTEXITCODE -ne 0) { throw 'Repair executable compilation failed.' }
    $check = Start-Process -FilePath $OutputExe -ArgumentList '--check' -PassThru -Wait -WindowStyle Hidden
    if ($check.ExitCode -ne 0) { throw 'Embedded archive validation failed.' }
    Get-FileHash -LiteralPath $OutputExe -Algorithm SHA256 | Format-List
}
finally { Remove-Item -LiteralPath $hashFile -Force }
