@echo off
rem ============================================================================
rem  Qwen3-TTS Studio 启动器
rem
rem  编码说明（重要）：本文件必须保存为 **GBK 编码 + CRLF 换行**，
rem  并且**不要**加 chcp 65001。
rem  实测：UTF-8 编码的中文在 cmd 里会被按 GBK 解读，中文路径被截断，
rem  报 "不是内部或外部命令"。保持 GBK 最省事。
rem  改这个文件请用 Python 以 encoding="gbk" 写回，别用普通编辑器存成 UTF-8。
rem ============================================================================
cd /d "%~dp0"

rem ---- 端口 ----
rem  跟 web/server.py 读的是同一个环境变量；这里取出来是为了把横幅和浏览器
rem  都指到同一个地方（以前横幅写死 7867，改了端口会对不上）。
set "QPORT=7867"
if defined QWENSTUDIO_PORT set "QPORT=%QWENSTUDIO_PORT%"

rem ---- 便携运行时：优先用本目录自带的 runtime\，不依赖系统装的 Python ----
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" (
    if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
)
set "ENTRY=%~dp0web\server.py"

rem ---- 干净环境 ----
rem  清掉外部 PYTHONPATH，防止别的 Python 把包搅进来；
rem  固定 UTF-8，避免日志里的中文变乱码。
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---- 全离线：权重与推理内核都随包分发，不联网取 ----
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "MODELSCOPE_OFFLINE=1"

if not exist "%PY%" (
    echo [错误] 没找到可用的 Python：
    echo         %~dp0runtime\python\python.exe
    echo         备用位置：%~dp0.venv\Scripts\python.exe
    echo.
    echo         如果你是从仓库克隆的，请先确认 runtime\ 已随包就位。
    pause
    exit /b 1
)
if not exist "%ENTRY%" (
    echo [错误] 找不到入口文件：%ENTRY%
    pause
    exit /b 1
)

echo ============================================================
echo   Qwen3-TTS Studio  -  WebUI（FastAPI + 自写前端）
echo   http://127.0.0.1:%QPORT%
echo ============================================================
echo.
echo   解释器：%PY%
echo   首次生成要加载权重，慢一次；之后同规格会复用。
echo   浏览器会自动打开；不想自动开就先设 QWENSTUDIO_NO_BROWSER=1
echo.

rem ---- 自动打开浏览器 ----
rem  **先起一个后台小助手去等端口，再启动服务**，不是固定 sleep 多少秒：
rem  首次启动要加载权重，杀软还要扫一遍 torch 的 dll，几秒到几十秒都可能，
rem  猜早了只会开出一个「无法连接」的页面。助手每 500 ms 探一次，通了才开页签，
rem  两分钟还起不来就安静退出（服务本身的报错在控制台里，不抢它的位置）。
rem  用 start /b + 隐藏窗口：不新开黑框、也不挡住服务的日志。
rem  助手那段脚本里**不能出现双引号**（cmd 的引号配对会乱），里面统一用单引号。
if "%QWENSTUDIO_NO_BROWSER%"=="1" goto :skip_browser
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "for($i=0;$i -lt 240;$i++){try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',%QPORT%);$c.Close();$ok=$true;break}catch{Start-Sleep -Milliseconds 500}};if($ok){Start-Process ('http://127.0.0.1:%QPORT%/')}" >nul 2>nul
:skip_browser

"%PY%" -s "%ENTRY%"
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" echo [异常退出] 退出码 %CODE% ，请查看上方报错信息。
echo 服务已停止。
pause
