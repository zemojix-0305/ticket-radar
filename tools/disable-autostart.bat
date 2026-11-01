@echo off
chcp 65001 >nul
title 取消开机自动监控
echo ========================================
echo  取消 ticket-radar 开机自启
echo ========================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = [Environment]::GetFolderPath('Startup') + '\ticket-radar.lnk';" ^
  "if (Test-Path $p) { Remove-Item $p -Force; Write-Output '[ok] 已取消开机自启' } else { Write-Output '[i] 本来就没有设置过' }"

echo.
echo 注意：这只影响「开机自动启动」。
echo      当前已经开着的服务窗口还在跑，要停请直接关掉那个窗口。
echo.
pause
