param(
    [string]$OutputExe = (Join-Path $PSScriptRoot 'artifacts/portable/搬运工V2.exe'),
    [string]$UvOutput = (Join-Path $PSScriptRoot 'artifacts/portable/tools/uv.exe')
)

$ErrorActionPreference = 'Stop'
$uvVersion = '0.13.8'
$uvArchiveHash = '529E78A9CEC6CD7E3E014F7ECD8BDA0C113969D4ED0F2EFEEDB85C30943325B8'
$uvExecutableHash = '03FA174894B9D0CBE08762D40D1C2C64D10E875BD56F1A1E334EE1E00D60CDBE'
$uvArchiveName = 'uv-x86_64-pc-windows-msvc.zip'
$artifactDir = Join-Path $PSScriptRoot 'artifacts'
$cacheDir = Join-Path $artifactDir '.installer-cache'
$archive = Join-Path $cacheDir $uvArchiveName
$extract = Join-Path $cacheDir 'portable-uv-extract'
$compilerCandidates = @(
    (Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'),
    (Join-Path $env:WINDIR 'Microsoft.NET/Framework/v4.0.30319/csc.exe')
)
$compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $compiler) { throw '未找到 Windows .NET Framework C# 编译器。' }

New-Item -ItemType Directory -Force -Path $cacheDir,(Split-Path $OutputExe -Parent),(Split-Path $UvOutput -Parent) | Out-Null
$bundledUv = Join-Path $PSScriptRoot 'tools/uv.exe'
$reuseBundledUv = (Test-Path -LiteralPath $bundledUv -PathType Leaf) -and ((Get-FileHash -LiteralPath $bundledUv -Algorithm SHA256).Hash -eq $uvExecutableHash)
$archiveHash = $null
if ($reuseBundledUv) {
    # The executable digest is pinned to uv 0.13.8 from the verified official
    # archive; rebuilding a restored release need not download it again.
    $uvSource = $bundledUv
}
else {
if ((Test-Path -LiteralPath $archive -PathType Leaf) -and (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $uvArchiveHash) {
    Remove-Item -LiteralPath $archive -Force
}
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    $previous = Get-ChildItem -LiteralPath $artifactDir -Directory -Filter '.installer-build-*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path (Join-Path $_.FullName 'downloads') $uvArchiveName } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($previous) {
        Copy-Item -LiteralPath $previous -Destination $archive
    }
    else {
        $url = "https://github.com/astral-sh/uv/releases/download/$uvVersion/$uvArchiveName"
        & curl.exe -fLsS --retry 2 --connect-timeout 15 --max-time 900 -o $archive $url
        if ($LASTEXITCODE -ne 0) { throw "Official uv download failed (curl exit $LASTEXITCODE)." }
    }
}
$archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($archiveHash -ne $uvArchiveHash) { throw "uv archive SHA-256 mismatch: $archiveHash" }

if (-not (Test-Path -LiteralPath (Join-Path $extract 'uv.exe') -PathType Leaf)) {
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
}
$uvSource = Join-Path $extract 'uv.exe'
}
if ((Get-FileHash -LiteralPath $uvSource -Algorithm SHA256).Hash -ne $uvExecutableHash) {
    throw 'uv executable SHA-256 mismatch.'
}
Copy-Item -LiteralPath $uvSource -Destination $UvOutput -Force
$uvVersionOutput = (& $UvOutput --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $uvVersionOutput -notmatch "^uv $([regex]::Escape($uvVersion)) ") {
    throw "Bundled uv version check failed: $uvVersionOutput"
}

foreach ($license in @('LICENSE-APACHE', 'LICENSE-MIT')) {
    $target = Join-Path (Split-Path $UvOutput -Parent) "$license.txt"
    $existingNotice = Join-Path $PSScriptRoot "tools/$license.txt"
    if (-not (Test-Path -LiteralPath $target -PathType Leaf) -and (Test-Path -LiteralPath $existingNotice -PathType Leaf)) {
        Copy-Item -LiteralPath $existingNotice -Destination $target
    }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        $url = "https://api.github.com/repos/astral-sh/uv/contents/$license"
        $response = Invoke-RestMethod -Uri $url -Headers @{ 'User-Agent' = 'MoverV2-Portable' } -TimeoutSec 30
        if (-not $response.content) { throw "Could not retrieve the uv $license notice." }
        [System.IO.File]::WriteAllBytes($target, [Convert]::FromBase64String(($response.content -replace '\s', '')))
    }
}

$arguments = @(
    '/nologo', '/target:winexe', '/platform:x64', '/codepage:65001', '/optimize+',
    '/reference:System.Windows.Forms.dll',
    "/out:$OutputExe",
    (Join-Path $PSScriptRoot 'portable_launcher.cs')
)
& $compiler @arguments
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $OutputExe -PathType Leaf)) {
    throw 'C# portable launcher compilation failed.'
}
$checkRoot = Join-Path $artifactDir ('.portable-launcher-check-' + [guid]::NewGuid().ToString('N'))
try {
    $checkTools = Join-Path $checkRoot 'tools'
    New-Item -ItemType Directory -Force -Path $checkTools | Out-Null
    Copy-Item -LiteralPath $OutputExe -Destination (Join-Path $checkRoot '搬运工V2.exe')
    $checkFiles = @(
        'update-and-start.ps1', 'updater.py', 'start-workbench.ps1', 'python-runtime.ps1',
        'workbench_server.py', 'astrbot_plugin_message_bridge\metadata.yaml'
    )
    foreach ($relativePath in $checkFiles) {
        $checkFile = Join-Path $checkRoot $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path $checkFile -Parent) | Out-Null
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $relativePath) -Destination $checkFile
    }
    Copy-Item -LiteralPath $UvOutput -Destination $checkTools
    Copy-Item -LiteralPath (Join-Path (Split-Path $UvOutput -Parent) 'LICENSE-APACHE.txt') -Destination $checkTools
    Copy-Item -LiteralPath (Join-Path (Split-Path $UvOutput -Parent) 'LICENSE-MIT.txt') -Destination $checkTools
    $check = Start-Process -FilePath (Join-Path $checkRoot '搬运工V2.exe') -ArgumentList '--check' -WindowStyle Hidden -Wait -PassThru
    if ($check.ExitCode -ne 0) { throw "Portable launcher self-check failed: $($check.ExitCode)" }
}
finally {
    $resolvedCheck = [System.IO.Path]::GetFullPath($checkRoot)
    $resolvedArtifacts = [System.IO.Path]::GetFullPath($artifactDir).TrimEnd('\') + '\'
    if ($resolvedCheck.StartsWith($resolvedArtifacts, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedCheck)) {
        Remove-Item -LiteralPath $resolvedCheck -Recurse -Force
    }
}

[PSCustomObject]@{
    Launcher = [System.IO.Path]::GetFullPath($OutputExe)
    Uv = $uvVersionOutput
    VerifiedUvArchiveSHA256 = $archiveHash
    VerifiedUvExecutableSHA256 = $uvExecutableHash
    LauncherSHA256 = (Get-FileHash -LiteralPath $OutputExe -Algorithm SHA256).Hash
} | Format-List
