@echo off
rem ============================================================================
rem  用 GitHub 下载的代码 zip 覆盖更新
rem
rem  覆盖的是代码那几兆；权重、便携运行时、你的音色库、生成记录一概不动。
rem  用法：
rem    1) 去 GitHub 仓库页 → Code → Download ZIP
rem    2) 把 zip 放到本目录（或 Downloads / 桌面），双击本文件
rem  编码说明：本文件必须 **GBK 编码 + CRLF 换行**，别加 chcp 65001。
rem  改这个文件请用 Python 以 encoding="gbk" 写回。
rem ============================================================================
cd /d "%~dp0"

set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 没找到便携 Python：
    echo         %~dp0runtime\python\python.exe
    pause
    exit /b 1
)

set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

"%PY%" "%~dp0tools\update_code.py" %*
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" echo [没更新成功] 退出码 %CODE% ，看上面的说明。
echo 更新完记得重新启动 run_web.bat。
pause
exit /b %CODE%