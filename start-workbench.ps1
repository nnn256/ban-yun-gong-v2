$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$runtimeDir = Join-Path $projectRoot 'runtime'
$toolsDir = Join-Path $projectRoot 'tools'
$bundledUv = Join-Path $toolsDir 'uv.exe'
if (Test-Path -LiteralPath $bundledUv -PathType Leaf) {
    $env:PATH = "$toolsDir;$env:PATH"
}
$astrbotRoot = Join-Path $runtimeDir 'AstrBot'
$pythonExe = Join-Path $runtimeDir '.venv\Scripts\python.exe'
$astrbotExe = Join-Path $runtimeDir '.venv\Scripts\astrbot.exe'
$serverScript = Join-Path $projectRoot 'workbench_server.py'
$logDir = Join-Path $runtimeDir 'logs'
$workbenchPort = 18766
$baseUrl = "http://127.0.0.1:$workbenchPort"

# Check the fixed port before downloading runtimes so a second extracted copy
# cannot silently attach its launch request to another install's dashboard.
$probe = [System.Net.Sockets.TcpClient]::new()
$alreadyRunning = $false
try { $probe.Connect('127.0.0.1', $workbenchPort); $alreadyRunning = $true }
catch { }
finally { $probe.Dispose() }

if ($alreadyRunning) {
    try {
        $existing = Invoke-RestMethod -Uri "$baseUrl/api/status" -TimeoutSec 3
    }
    catch {
        throw "端口 $workbenchPort 已被其他程序占用，未启动搬运工V2。"
    }
    $sameInstall = $false
    if ($existing.workbench_root) {
        $sameInstall = [System.IO.Path]::GetFullPath([string]$existing.workbench_root).TrimEnd('\') -eq $projectRoot.TrimEnd('\')
    }
    elseif ($existing.settings.astrbot_root) {
        $expectedAstrBot = [System.IO.Path]::GetFullPath($astrbotRoot).TrimEnd('\')
        $currentAstrBot = [System.IO.Path]::GetFullPath([string]$existing.settings.astrbot_root).TrimEnd('\')
        $sameInstall = $expectedAstrBot -eq $currentAstrBot
    }
    if (-not $sameInstall) {
        throw "端口 $workbenchPort 上运行着另一个搬运工V2目录。请先停止旧工作台/AstrBot，再启动此便携版。"
    }
}
else {
    if (-not (Test-Path -LiteralPath $astrbotExe -PathType Leaf)) {
        & (Join-Path $projectRoot 'install_astrbot.ps1')
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    $larkxSource = Join-Path $runtimeDir 'LarkAgentX-runtime\LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490'
    if (-not (Test-Path -LiteralPath (Join-Path $larkxSource 'pyproject.toml') -PathType Leaf)) {
        & (Join-Path $projectRoot 'install_larkagentx.ps1')
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $env:LARKX_SOURCE = $larkxSource
    $env:LARKX_HOME = Join-Path $runtimeDir 'LarkAgentX-data'
    $env:LARKX_AGENT_BACKEND = 'none'
    $env:MOVER_WORKBENCH_PORT = [string]$workbenchPort
    Start-Process -FilePath $pythonExe -ArgumentList @($serverScript) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'workbench.stdout.log') -RedirectStandardError (Join-Path $logDir 'workbench.stderr.log') | Out-Null
}

$ready = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    try {
        $status = Invoke-RestMethod -Uri "$baseUrl/api/status" -TimeoutSec 2
        $ready = $true
        break
    }
    catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) { throw "搬运工V2 工作台未启动，请检查 $logDir" }

if (-not $status.astrbot.running) {
    if (-not (Test-Path -LiteralPath $astrbotExe -PathType Leaf)) {
        & (Join-Path $projectRoot 'install_astrbot.ps1')
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    try { $null = Invoke-RestMethod -Uri "$baseUrl/api/plugin/install" -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 20 } catch { throw "桥接插件安装失败：$($_.Exception.Message)" }
    $result = Invoke-RestMethod -Uri "$baseUrl/api/astrbot/start" -Method Post -ContentType 'application/json' -Body '{"reset_password":false}' -TimeoutSec 30
    if ($result.started -ne $true) { throw 'AstrBot 未能启动；请查看工作台错误提示和 runtime 日志。' }
}

Start-Process $baseUrl
Write-Output "搬运工V2 工作台: $baseUrl"
Write-Output "AstrBot 管理页: http://127.0.0.1:6185/"
