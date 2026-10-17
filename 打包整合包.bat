@echo off
rem ============================================================================
rem  Qwen3-TTS Studio 整合包打包器
rem
rem  编码说明（重要）：本文件必须 **GBK 编码 + CRLF 换行**，别加 chcp 65001。
rem  理由同 启动程序.bat：UTF-8 的中文在 cmd 里会被按 GBK 解读，中文路径被截断。
rem  改这个文件请用 Python 以 encoding="gbk" 写回，别用普通编辑器存成 UTF-8。
rem
rem  真正干活的还是 tools\pack_release.py，这里只是把常用组合摆成菜单。
rem  也可以带序号直接跑、跳过菜单：   打包整合包.bat 2
rem ============================================================================
cd /d "%~dp0"

set "PY=%~dp0runtime\python\python.exe"
set "PACK=%~dp0tools\pack_release.py"

rem ---- 跟 启动程序.bat 保持一致的环境 ----
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PY%" (
    echo [错误] 没找到便携 Python：
    echo         %PY%
    echo         这个脚本要放在项目根目录下运行。
    pause
    exit /b 1
)
if not exist "%PACK%" (
    echo [错误] 没找到打包脚本：%PACK%
    pause
    exit /b 1
)

rem 带参数就直接跳菜单（方便脚本调用，也方便自测）
if not "%~1"=="" goto :from_arg

:menu
cls
echo ============================================================
echo   Qwen3-TTS Studio  -  整合包打包
echo ============================================================
echo.
"%PY%" -c "import shutil; u=shutil.disk_usage('.'); print('  当前盘可用：%.1f G' % (u.free/1024**3))"
echo.
echo   要打的东西合计约 26 G（其中权重 17.5 G、便携运行时 7.8 G）。
echo   voices / outputs 这些**个人数据不会进包**，脚本里已经挡住了。
echo.
echo   [1] 完整包，目录形态                约 26 G    拷移动硬盘用这个
echo   [2] 完整包 + 压成单个 zip           峰值 44 G  传网盘用这个
echo   [3] 完整包 + 分卷（每卷 3800 MB）   峰值 44 G  微信/QQ 转发用这个
echo   [4] 轻量包（不含权重）              约 8.4 G   对方自己有权重时才用
echo   [5] 只自测：硬链接建副本（秒级，不占空间）      打包后先验一遍
echo   [9] 只看清单，不写盘
echo   [0] 退出
echo.
set /p CH=  输入序号再回车：
goto :dispatch

:from_arg
set "CH=%~1"

:dispatch
if "%CH%"=="0" exit /b 0
set "ARGS="
if "%CH%"=="1" set "ARGS=--yes"
if "%CH%"=="2" set "ARGS=--yes --zip"
if "%CH%"=="3" set "ARGS=--yes --zip --split 3800"
if "%CH%"=="4" set "ARGS=--yes --no-models"
if "%CH%"=="5" set "ARGS=--yes --hardlink --name _selftest"
if "%CH%"=="9" set "ARGS=--list"
if defined ARGS goto :do

echo.
echo   没有这个序号：%CH%
pause
exit /b 1

:do
echo.
echo   执行： tools\pack_release.py %ARGS%
echo   （文件多，会慢慢走；中途想停就关掉这个窗口）
echo.
"%PY%" "%PACK%" %ARGS%
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" goto :failed

echo ============================================================
echo   打包完成，产物在： %~dp0dist\
echo ============================================================
if "%CH%"=="5" echo   注意：5 是**硬链接**副本，只能自测，不能发给别人。
if "%CH%"=="2" echo   要传网盘就把 dist 里那个 zip 传上去。
if "%CH%"=="3" echo   把 dist 里的分卷 + 合并并解压.bat 一起发过去。
goto :bye

:failed
echo ============================================================
echo   打包失败，退出码 %CODE% ，看上一段报错。
echo   最常见的原因：dist 里的同名目录已存在（换个名字或删掉它）。
echo ============================================================

:bye
echo.
if "%~1"=="" pause
exit /b %CODE%
