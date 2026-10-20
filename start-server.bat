@echo off
chcp 65001 >nul
cd /d "%~dp0server"
echo ========================================
echo  ticket-radar 本地服务启动器
echo  地址: http://localhost:3000
echo  注意: 保持此窗口打开，关闭即停止服务
echo ========================================
echo.

REM 优先用系统 PATH 里的 node，找不到再用 WorkBuddy 管理的 node
where node >nul 2>nul
if %errorlevel%==0 (
  echo 使用系统 node
  node api.js
) else (
  echo 使用 WorkBuddy 管理的 node
  "C:\Users\zemojix\.workbuddy\binaries\node\versions\22.22.2-2\node.exe" api.js
)

echo.
echo 服务已停止（窗口关闭或出错）。
pause
