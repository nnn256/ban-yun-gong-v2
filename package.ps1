$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$verify = Join-Path $projectRoot 'verify.ps1'
& $verify
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$artifactDir = Join-Path $projectRoot 'artifacts'
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
$archive = Join-Path $artifactDir '搬运工V2.zip'
$tempArchive = Join-Path $artifactDir ('.build-' + [guid]::NewGuid().ToString('N') + '.zip')
$portableExe = Join-Path $artifactDir 'portable/搬运工V2.exe'
$portableUv = Join-Path $artifactDir 'portable/tools/uv.exe'
& (Join-Path $projectRoot 'build-portable.ps1') -OutputExe $portableExe -UvOutput $portableUv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$relativeFiles = @(
    '.gitignore',
    'README.md',
    'VERIFICATION.txt',
    'launch-workbench.cmd',
    '一键启动.cmd',
    '后台运行.vbs',
    '打开工作台.vbs',
    'build-portable.ps1',
    'updater.py',
    'update-and-start.ps1',
    '更新并启动.cmd',
    'log_store.py',
    'portable_launcher.cs',
    'install.ps1',
    'install_astrbot.ps1',
    'install_larkagentx.ps1',
    'install_snowluma.ps1',
    'start_snowluma.ps1',
    'start-workbench.ps1',
    'stop-workbench.ps1',
    'verify.ps1',
    'package.ps1',
    'background_runtime.py',
    'background_supervisor.py',
    'astrbot_runner.py',
    'workbench_server.py',
    'workbench.settings.example.json'
)
$sourceDirs = @(
    (Join-Path $projectRoot 'astrbot_plugin_message_bridge'),
    (Join-Path $projectRoot 'tests'),
    (Join-Path $projectRoot 'workbench'),
    (Join-Path $projectRoot 'vendor'),
    (Join-Path $projectRoot 'installer')
)

foreach ($sourceDir in $sourceDirs) {
    $relativeFiles += Get-ChildItem -LiteralPath $sourceDir -Recurse -File |
        Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } |
        ForEach-Object { $_.FullName.Substring($projectRoot.Length + 1) }
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open(
    $tempArchive,
    [System.IO.Compression.ZipArchiveMode]::Create
)
try {
    foreach ($relativePath in $relativeFiles) {
        $fullPath = Join-Path $projectRoot $relativePath
        $entryName = $relativePath.Replace('\', '/')
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip,
            $fullPath,
            $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
    $portableEntries = @(
        @{ Path = $portableExe; Entry = '搬运工V2.exe' },
        @{ Path = $portableExe; Entry = 'mover-v2-launcher.exe' },
        @{ Path = $portableUv; Entry = 'tools/uv.exe' },
        @{ Path = (Join-Path (Split-Path $portableUv -Parent) 'LICENSE-APACHE.txt'); Entry = 'tools/LICENSE-APACHE.txt' },
        @{ Path = (Join-Path (Split-Path $portableUv -Parent) 'LICENSE-MIT.txt'); Entry = 'tools/LICENSE-MIT.txt' }
    )
    foreach ($item in $portableEntries) {
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip,
            $item.Path,
            $item.Entry,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
}
finally {
    $zip.Dispose()
}

$manifestPython = Join-Path $projectRoot 'runtime\.venv\Scripts\python.exe'
$manifestPythonArgs = @()
if (-not (Test-Path -LiteralPath $manifestPython -PathType Leaf)) {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonLauncher) { throw 'Packaging requires the project Python runtime or py.exe to seal the release manifest.' }
    $manifestPython = $pythonLauncher.Source
    $manifestPythonArgs = @('-3')
}
& $manifestPython @manifestPythonArgs -B (Join-Path $projectRoot 'updater.py') seal $tempArchive --version 2.0.9
if ($LASTEXITCODE -ne 0) { throw 'Could not seal the exact release ZIP payload.' }

$check = [System.IO.Compression.ZipFile]::OpenRead($tempArchive)
try {
    $expectedEntries = $relativeFiles.Count + $portableEntries.Count + 1
    if ($check.Entries.Count -ne $expectedEntries) {
        throw "Archive entry count mismatch: expected $expectedEntries, got $($check.Entries.Count)"
    }
    if ($check.Entries.FullName -match '(^|/)__pycache__(/|$)|\.pyc$') {
        throw 'Archive contains Python bytecode cache files.'
    }
    if ($check.Entries.FullName -match '(^|/)runtime(/|$)|(^|/)(credentials\.json|bridge_rules\.json|cmd_config\.json|onebot\.json|workbench\.settings\.json|\.workbench-bootstrap\.dpapi|\.mover-v2-astrbot\.pid)$') {
        throw 'Archive contains local runtime data, login credentials, or user group mappings.'
    }
    if ($check.Entries.FullName -notcontains '搬运工V2.exe' -or $check.Entries.FullName -notcontains 'tools/uv.exe') {
        throw 'Portable launcher or bundled uv.exe is missing from the archive.'
    }
}
finally {
    $check.Dispose()
}

$portableZipCheck = Join-Path $artifactDir ('.portable-zip-check-' + [guid]::NewGuid().ToString('N'))
try {
    Expand-Archive -LiteralPath $tempArchive -DestinationPath $portableZipCheck
    $launcherPath = Join-Path $portableZipCheck '搬运工V2.exe'
    if (-not (Test-Path -LiteralPath (Join-Path $portableZipCheck 'start-workbench.ps1')) -or
        -not (Test-Path -LiteralPath (Join-Path $portableZipCheck 'tools/uv.exe'))) {
        throw 'Portable ZIP is missing its launcher dependency files.'
    }
    $portableCheck = Start-Process -FilePath $launcherPath -ArgumentList '--check' -WindowStyle Hidden -Wait -PassThru
    if ($portableCheck.ExitCode -ne 0) { throw "Portable ZIP launcher self-check failed: $($portableCheck.ExitCode)" }
}
finally {
    $resolvedCheck = [System.IO.Path]::GetFullPath($portableZipCheck)
    $resolvedArtifacts = [System.IO.Path]::GetFullPath($artifactDir).TrimEnd('\') + '\'
    if ($resolvedCheck.StartsWith($resolvedArtifacts, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedCheck)) {
        Remove-Item -LiteralPath $resolvedCheck -Recurse -Force
    }
}

Move-Item -LiteralPath $tempArchive -Destination $archive -Force
Get-FileHash -LiteralPath $archive -Algorithm SHA256 | Format-List
Write-Output "Created: $archive"
