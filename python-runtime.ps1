# Shared by update verification and AstrBot setup; no platform data is a repair target.

function ConvertTo-MoverNativeArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-MoverRuntimeCommand {
    param([string]$Executable, [string[]]$Arguments, [int]$TimeoutMilliseconds = 10000,
          [hashtable]$Environment = @{})
    $process = New-Object Diagnostics.Process
    $process.StartInfo.FileName = $Executable
    $process.StartInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-MoverNativeArgument $_ }) -join ' ')
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $process.StartInfo.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    foreach ($key in $Environment.Keys) { $process.StartInfo.EnvironmentVariables[$key] = [string]$Environment[$key] }
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try { $process.Kill(); $process.WaitForExit(3000) | Out-Null } catch { }
            return @{ExitCode=-1; Output=''; Error='Runtime command timed out.'}
        }
        return @{ExitCode=$process.ExitCode; Output=$stdout.GetAwaiter().GetResult(); Error=$stderr.GetAwaiter().GetResult()}
    }
    catch { return @{ExitCode=-1; Output=''; Error='Runtime process could not be started.'} }
    finally { $process.Dispose() }
}

function Test-MoverPython {
    param([string]$Executable, [string[]]$Arguments = @(), [switch]$Application)
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    $condition = 'sys.version_info >= (3,12)'
    if ($Application) { $condition = 'sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8' }
    $probe = 'import sys,struct,json,hashlib,zipfile,pathlib,ctypes; assert ' + $condition + '; print("MOVER_PYTHON_READY")'
    $result = Invoke-MoverRuntimeCommand -Executable $Executable -Arguments (@($Arguments) + @('-I','-B','-c',$probe))
    return $result.ExitCode -eq 0 -and $result.Output.Trim() -ceq 'MOVER_PYTHON_READY'
}

function Get-MoverUv([string]$ProjectRoot) {
    $bundled = Join-Path $ProjectRoot 'tools\uv.exe'
    if (Test-Path -LiteralPath $bundled -PathType Leaf) { return $bundled }
    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw 'Bundled tools\uv.exe is missing. Fully extract the release ZIP or reinstall the application.'
}

function Get-MoverManagedPython([string]$ProjectRoot) {
    $uv = Get-MoverUv $ProjectRoot
    $managedRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot 'runtime\python'))
    $environment = @{UV_PYTHON_INSTALL_DIR=$managedRoot; PYTHONUTF8='1'; PYTHONIOENCODING='utf-8'}
    $findArgs = @('python','find','--no-config','--system','--managed-python','--no-python-downloads','--resolve-links','3.12.13')
    for ($attempt=0; $attempt -lt 3; $attempt++) {
        $found = Invoke-MoverRuntimeCommand -Executable $uv -Arguments $findArgs -Environment $environment -TimeoutMilliseconds 20000
        $path = $found.Output.Trim()
        if ($found.ExitCode -eq 0 -and $path -and [IO.Path]::IsPathRooted($path) -and
            [IO.Path]::GetFullPath($path).StartsWith($managedRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -and
            (Test-MoverPython -Executable $path -Application)) { return $path }
        if ($attempt -eq 2) { break }
        Write-Host 'Preparing this installation''s Python 3.12 runtime. Network access may be required...'
        $installArgs = @('python','install','--no-config','--no-bin','--no-registry','3.12.13')
        if ($attempt -eq 1) { $installArgs += '--reinstall' }
        $installed = Invoke-MoverRuntimeCommand -Executable $uv -Arguments $installArgs -Environment $environment -TimeoutMilliseconds 600000
        if ($installed.ExitCode -ne 0) {
            throw 'Python runtime preparation failed. Check network access and runtime\logs; saved logins and rules were retained.'
        }
    }
    throw 'Python runtime still cannot execute after repair. Saved logins and rules were retained.'
}

function Get-MoverVerificationPython([string]$SourceRoot, [string]$TargetRoot) {
    foreach ($root in @($TargetRoot, $SourceRoot) | Select-Object -Unique) {
        $candidate = Join-Path $root 'runtime\.venv\Scripts\python.exe'
        if (Test-MoverPython -Executable $candidate) { return @{Executable=$candidate; Arguments=@()} }
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            Write-Host 'An existing Python entry cannot run; selecting a healthy runtime before verifying the package.'
        }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher -and (Test-MoverPython -Executable $launcher.Source -Arguments @('-3'))) {
        return @{Executable=$launcher.Source; Arguments=@('-3')}
    }
    return @{Executable=(Get-MoverManagedPython $SourceRoot); Arguments=@()}
}

function Initialize-MoverApplicationPython([string]$ProjectRoot) {
    $python = Join-Path $ProjectRoot 'runtime\.venv\Scripts\python.exe'
    if (Test-MoverPython -Executable $python -Application) { return $python }
    $base = Get-MoverManagedPython $ProjectRoot
    $uv = Get-MoverUv $ProjectRoot
    $venv = [IO.Path]::GetFullPath((Join-Path $ProjectRoot 'runtime\.venv'))
    foreach ($path in @($venv, (Join-Path $venv 'Scripts'))) {
        if (Test-Path -LiteralPath $path) {
            $item = Get-Item -LiteralPath $path -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Runtime repair cannot traverse a linked environment directory.' }
        }
    }
    Write-Host 'Repairing the local Python environment; account data and routing rules are retained...'
    # Rewrite the interpreter launchers/config at their final path, retaining site-packages.
    # No recursive deletion, environment relocation or global Python registration.
    $created = Invoke-MoverRuntimeCommand -Executable $uv -Arguments @('venv','--no-config','--python',$base,'--allow-existing',$venv) -Environment @{UV_VENV_CLEAR='false'} -TimeoutMilliseconds 60000
    if ($created.ExitCode -ne 0 -or -not (Test-MoverPython -Executable $python -Application)) {
        throw 'Local Python repair failed. Close this installation and retry. Saved logins and rules were retained.'
    }
    return $python
}
