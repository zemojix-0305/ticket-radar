@echo off
chcp 65001 >nul
title ticket-radar 本地服务（自动重启）
cd /d "D:\Amyapp\ticket-radar\server"
echo ========================================
echo  ticket-radar 本地服务
echo  地址: http://localhost:3000/api
echo  关闭此窗口 = 停止服务
echo  进程意外退出会自动重启（等 3 秒）
echo ========================================
echo.

:loop
"C:\Users\zemojix\.workbuddy\binaries\node\versions\22.22.2-2\node.exe" api.js
echo.
echo [%date% %time%] 服务已退出，3 秒后自动重启（想彻底停就关掉这个窗口）
timeout /t 3 /nobreak >nul
goto loop
