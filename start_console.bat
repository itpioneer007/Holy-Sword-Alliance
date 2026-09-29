@echo off
rem ============================================================
rem  圣剑脚本 控制台一键启动
rem  用法: 双击本文件 -> 启动服务并自动打开浏览器
rem  注意: 关闭本窗口 = 停止控制台服务
rem ============================================================
cd /d "%~dp0"

rem ---- 若已有控制台在 5050 运行, 直接开浏览器 ----
curl -s -o nul -m 2 http://127.0.0.1:5050/api/status
if %errorlevel%==0 (
    echo [已运行] 控制台已在 5050 端口运行, 正在打开浏览器...
    start http://127.0.0.1:5050
    timeout /t 3 /nobreak >nul
    exit /b 0
)

echo [启动] 正在启动圣剑脚本控制台 (首次加载约 6-12 秒, 就绪后自动打开浏览器)...
rem ---- 服务就绪后由 server.py 自动打开浏览器 (SJ_OPEN_BROWSER=1) ----
set SJ_OPEN_BROWSER=1
"C:\Users\19507\.workbuddy\binaries\python\envs\sj_bot\Scripts\python.exe" -m sj_bot.server

pause
