# 沙箱 SSH 隧道守护：断了就自动重连。登录时由启动项拉起，无需人工开窗口。
$ErrorActionPreference = 'Continue'
$log = 'D:\Miaooaimzzz\xyz_agent\runtime\tunnel.log'
$target = 'root@47.101.71.56'
$forward = '18090:127.0.0.1:8080'

function Write-Log([string]$msg) {
    "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) $msg" | Out-File -FilePath $log -Append -Encoding utf8
}

while ($true) {
    Write-Log "start tunnel $forward -> $target"
    # BatchMode=yes: 只用密钥，绝不弹密码提示（无人值守前提）
    & ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes `
          -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
          -L $forward $target 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    Write-Log "tunnel exited (code=$LASTEXITCODE), retry in 10s"
    Start-Sleep -Seconds 10
}
