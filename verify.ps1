$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot 'runtime\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    $pythonExe = 'py'
    $pythonArgs = @('-3', '-B')
}
else {
    $pythonArgs = @('-B')
}

Push-Location $projectRoot
try {
    & $pythonExe @pythonArgs -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    $pythonFiles = @(
        (Join-Path $projectRoot 'astrbot_plugin_message_bridge\bridge_core.py'),
        (Join-Path $projectRoot 'astrbot_plugin_message_bridge\main.py'),
        (Join-Path $projectRoot 'workbench_server.py')
    )
    $fileList = ($pythonFiles | ForEach-Object { "r'$_'" }) -join ','
    & $pythonExe @pythonArgs -c "import ast, pathlib; [ast.parse(pathlib.Path(p).read_text(encoding='utf-8')) for p in [$fileList]]"
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    Write-Output 'Python syntax check passed.'

    if (Get-Command node -ErrorAction SilentlyContinue) {
        node --check (Join-Path $projectRoot 'workbench\app.js')
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
        Write-Output 'JavaScript syntax check passed.'
    }

    $psFiles = @(Get-ChildItem -LiteralPath $projectRoot -Filter '*.ps1' -File) + @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'installer') -Filter '*.ps1' -File)
    foreach ($script in $psFiles) {
        $tokens = $null
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $script.FullName,
            [ref]$tokens,
            [ref]$parseErrors
        ) | Out-Null
        if ($parseErrors.Count -gt 0) {
            throw "PowerShell syntax error in $($script.FullName): $($parseErrors[0].Message)"
        }
    }
    Write-Output "PowerShell syntax check passed ($($psFiles.Count) scripts)."
}
finally {
    Pop-Location
}
