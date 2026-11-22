$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = $env:MOVER_VERIFY_PYTHON
if (-not $pythonExe) { $pythonExe = Join-Path $projectRoot 'runtime\.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    if ($env:MOVER_VERIFY_PYTHON) { throw 'MOVER_VERIFY_PYTHON must name an existing Python executable.' }
    $pythonExe = 'py'
    $pythonArgs = @('-3', '-B')
}
else {
    $pythonArgs = @('-B')
}

Push-Location $projectRoot
try {
    $env:PYTHONUTF8 = '1'
    & $pythonExe @pythonArgs -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    & $pythonExe @pythonArgs -c "import ast, pathlib; root=pathlib.Path.cwd(); files=[*root.glob('*.py'), *(root/'astrbot_plugin_message_bridge').glob('*.py')]; [ast.parse(p.read_text(encoding='utf-8-sig'), filename=str(p)) for p in files]; print(f'Parsed {len(files)} Python files.')"
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
