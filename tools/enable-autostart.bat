@echo off
chcp 65001 >nul
title 开启开机自动监控
echo ========================================
echo  让 ticket-radar 开机自动开始盯票
echo  开机后自动在后台启动服务（最小化窗口）
echo ========================================
echo.

set "ROOT=%~dp0.."
set "BAT=%ROOT%\start-server.bat"

if not exist "%BAT%" (
  echo [x] 找不到 start-server.bat，路径: %BAT%
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\ticket-radar.lnk');" ^
  "$s.TargetPath = '%BAT%';" ^
  "$s.WorkingDirectory = '%ROOT%';" ^
  "$s.WindowStyle = 7;" ^
  "$s.Description = 'ticket-radar 本地监控服务';" ^
  "$s.Save();"

if errorlevel 1 (
  echo [x] 创建失败
  pause
  exit /b 1
)

echo [ok] 已设置开机自启。
echo      下次开机后，服务会自动在后台跑（窗口最小化到任务栏）。
echo      想取消：双击 disable-autostart.bat
echo.
pause
