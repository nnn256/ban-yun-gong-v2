$ErrorActionPreference = 'Stop'
$stopped = $false
for ($port = 18766; $port -le 18776; $port++) {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/workbench/stop" -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 2
        Write-Output "搬运工V2 工作台已停止 (port $port)"
        $stopped = $true
        break
    }
    catch { }
}
if (-not $stopped) { Write-Output '搬运工V2 工作台当前未运行' }
