@echo off
rem ============================================================================
rem  Qwen3-TTS Studio  --  启动程序（局域网版）
rem
rem  和「启动程序.bat」的区别只有一处：这个文件让服务监听 0.0.0.0，
rem  所以**同一个 Wi-Fi 下的朋友和手机能直接打开**。它还会：
rem    1) 打印本机的内网地址 —— 把那一行发给朋友就行
rem    2) 放行防火墙端口（Windows 入站默认是拦截的，没有规则还是连不上）
rem
rem  安全提醒（重要）：
rem    这个服务**没有密码**。同一个网络里任何设备都能用你的显卡生成语音、
rem    读你的音色库、删你的合成记录。只在你自己信的 Wi-Fi 下开，
rem    别在咖啡厅 / 公司网 / 公共热点上开。关掉这个黑窗口就等于关掉服务。
rem
rem  本文件必须是 **GBK 编码 + CRLF 行尾**，绝对不要加 chcp 65001。
rem ============================================================================
cd /d "%~dp0"

rem ---- 取内网 IP 用的小工具（Win10/11 自带；取不到也不影响启动）----
set "PSEXE=powershell"

rem ---- 端口 ----
set "QPORT=7867"
if defined QWENSTUDIO_PORT set "QPORT=%QWENSTUDIO_PORT%"

rem ---- 关键：监听所有网卡，而不是只监听 127.0.0.1 ----
set "QWENSTUDIO_HOST=0.0.0.0"

rem ---- Python：优先用整合包自带的 runtime ----
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
set "ENTRY=%~dp0web\server.py"

rem ---- 清干净外部 PYTHONPATH，固定 UTF-8 ----
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---- 全离线：权重都在本地目录，不碰网络 ----
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "MODELSCOPE_OFFLINE=1"

if not exist "%PY%" (
    echo [错误] 没找到能用的 Python。
    echo         %PY%
    pause
    exit /b 1
)
if not exist "%ENTRY%" (
    echo [错误] 找不到入口文件 %ENTRY%
    pause
    exit /b 1
)

rem ---- 取本机内网 IPv4：走**默认路由所在的那块网卡**。
rem      一开始是按地址段挑的，结果总是挑中 WSL / 虚拟网卡的 172.26.x.x ——
rem      那些地址在局域网里没意义，朋友拿它肯定连不上。----
set "LANIP="
for /f "usebackq delims=" %%i in (`%PSEXE% -NoProfile -Command "$if=(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1).InterfaceAlias; (Get-NetIPAddress -InterfaceAlias $if -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress"`) do set "LANIP=%%i"

rem ---- 防火墙：入站默认拦截，放行这一个端口 ----
set "FWRULE=Qwen3-TTS Studio (%QPORT%)"
net session >nul 2>&1
if errorlevel 1 (
    echo [注意] 当前不是管理员权限，防火墙规则没加。
    echo        服务照样启动，但别人可能连不上。
    echo        解决办法：右键本文件，选择"以管理员身份运行"，再执行一次；
    echo        或者在"Windows 防火墙 - 允许应用通过防火墙"里放行 Python。
    echo.
) else (
    netsh advfirewall firewall show rule name="%FWRULE%" >nul 2>&1
    if errorlevel 1 (
        netsh advfirewall firewall add rule name="%FWRULE%" dir=in action=allow protocol=TCP localport=%QPORT% >nul
        echo   防火墙已放行 TCP %QPORT%
    ) else (
        echo   防火墙规则已存在：%FWRULE%
    )
)

echo ============================================================
echo   Qwen3-TTS Studio  --  局域网访问已开启
echo.
echo   本机      http://127.0.0.1:%QPORT%
if defined LANIP (
    echo   局域网    http://%LANIP%:%QPORT%
    echo.
    echo   把上面「局域网」那行地址发给朋友：他们连同一个 Wi-Fi、同一个路由器，
    echo   在浏览器里输这个地址就能用（手机也行）。
) else (
    echo   没取到内网 IP。手工查：运行  ipconfig  ，找"IPv4 地址"那一行。
)
echo   没有密码：同网络的人都能用。只在信的 Wi-Fi 下开。
echo   不想被访问了：关掉这个黑窗口就行，服务一停就连不上了。
echo ============================================================
echo.

rem ---- 自动打开浏览器（探测端口起来后再开，不抢启动时间）----
if "%QWENSTUDIO_NO_BROWSER%"=="1" goto :skip_browser
start "" /b %PSEXE% -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "for($i=0;$i -lt 240;$i++){try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',%QPORT%);$c.Close();$ok=$true;break}catch{Start-Sleep -Milliseconds 500}};if($ok){Start-Process ('http://127.0.0.1:%QPORT%/')}" >nul 2>nul
:skip_browser

"%PY%" -s "%ENTRY%"
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" echo [异常退出] 退出码 %CODE%，看上面的提示找原因。
echo 服务已停止。
pause