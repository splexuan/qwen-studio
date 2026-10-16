@echo off
rem ============================================================================
rem  环境自检：硬件档位 / 权重就位 / 音色库完整性
rem  GBK + CRLF，别加 chcp。
rem ============================================================================
cd /d "%~dp0"

set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" (
    if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
)

set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "MODELSCOPE_OFFLINE=1"

if not exist "%PY%" (
    echo [错误] 没找到 Python 运行时。
    pause
    exit /b 1
)

echo ================== 硬件与档位 ==================
"%PY%" -s "%~dp0app\hardware.py"
echo.
echo ================== 音色库自检 ==================
"%PY%" -s "%~dp0app\voices.py" doctor
echo.
echo ================== 权重就位情况 ==================
"%PY%" -s -c "import sys; sys.path.insert(0,'app'); import config as c; m=c.missing_models(); print('缺少权重：' + (', '.join(m) if m else '无，5 套全部就位'))"
echo.
pause
