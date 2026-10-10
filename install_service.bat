@echo off
REM ============================================================================
REM  install_service.bat - register collector.py as a Windows service via NSSM
REM
REM  Run this from an ADMINISTRATOR PowerShell/cmd prompt:
REM      cd C:\Users\jamie\Solpy
REM      .\install_service.bat
REM
REM  Prereq: nssm.exe on PATH, or sitting in this folder.
REM      winget install NSSM
REM  or download from https://nssm.cc/download and drop nssm.exe here.
REM
REM  Everything below uses ABSOLUTE paths. A service does not inherit your
REM  working directory, so relative paths like "data" would land somewhere
REM  like C:\Windows\System32 and you would not find them for weeks.
REM ============================================================================

setlocal

REM ---- EDIT THESE THREE IF YOUR LAYOUT DIFFERS -------------------------------
set PROJDIR=C:\Users\jamie\Solpy
set PYTHON=%PROJDIR%\venv\Scripts\python.exe
set SERVICE=SolCollector
REM ---------------------------------------------------------------------------

set SCRIPT=%PROJDIR%\collector.py
set DATADIR=%PROJDIR%\data
set LOGDIR=%PROJDIR%\logs

echo.
echo === Installing %SERVICE% ===
echo   python : %PYTHON%
echo   script : %SCRIPT%
echo   data   : %DATADIR%
echo.

if not exist "%PYTHON%" (
    echo ERROR: python not found at %PYTHON%
    echo Fix the PYTHON variable at the top of this file.
    exit /b 1
)
if not exist "%SCRIPT%" (
    echo ERROR: collector.py not found at %SCRIPT%
    exit /b 1
)

if not exist "%DATADIR%" mkdir "%DATADIR%"
if not exist "%LOGDIR%"  mkdir "%LOGDIR%"

REM Remove any previous install so re-running this is safe
nssm stop %SERVICE% >nul 2>&1
nssm remove %SERVICE% confirm >nul 2>&1

nssm install %SERVICE% "%PYTHON%"
if errorlevel 1 (
    echo ERROR: nssm not found. Install it or put nssm.exe in this folder.
    exit /b 1
)

REM -- what to run -------------------------------------------------------------
nssm set %SERVICE% AppDirectory "%PROJDIR%"
nssm set %SERVICE% AppParameters "\"%SCRIPT%\" --symbols SOLUSDT BTCUSDT ETHUSDT --interval 300 --outdir \"%DATADIR%\""

REM -- identity ---------------------------------------------------------------
nssm set %SERVICE% DisplayName "SOL Market Data Collector"
nssm set %SERVICE% Description "Logs order book, open interest, liquidations and positioning data that Binance does not serve historically."
nssm set %SERVICE% Start SERVICE_AUTO_START

REM -- start after the network is actually up ----------------------------------
REM  Without this the service races Windows' network stack on boot and spends
REM  its first minutes logging empty records.
nssm set %SERVICE% DependOnService Tcpip Dnscache

REM -- restart forever --------------------------------------------------------
nssm set %SERVICE% AppExit Default Restart
nssm set %SERVICE% AppRestartDelay 15000
nssm set %SERVICE% AppThrottle 10000

REM -- stop cleanly: Ctrl+C first so the SIGINT handler flushes files ----------
nssm set %SERVICE% AppStopMethodSkip 0
nssm set %SERVICE% AppStopMethodConsole 15000
nssm set %SERVICE% AppStopMethodWindow 5000
nssm set %SERVICE% AppStopMethodThreads 5000

REM -- console output to rotating log files ------------------------------------
nssm set %SERVICE% AppStdout "%LOGDIR%\collector.out.log"
nssm set %SERVICE% AppStderr "%LOGDIR%\collector.err.log"
nssm set %SERVICE% AppRotateFiles 1
nssm set %SERVICE% AppRotateOnline 1
nssm set %SERVICE% AppRotateBytes 10485760

REM -- unbuffered, so the log updates live rather than in 8KB lurches ----------
nssm set %SERVICE% AppEnvironmentExtra PYTHONUNBUFFERED=1

echo.
echo Starting...
nssm start %SERVICE%
timeout /t 6 /nobreak >nul
nssm status %SERVICE%

echo.
echo === Done ===
echo.
echo   status  : nssm status %SERVICE%
echo   stop    : nssm stop %SERVICE%
echo   start   : nssm start %SERVICE%
echo   edit    : nssm edit %SERVICE%
echo   remove  : nssm stop %SERVICE% ^&^& nssm remove %SERVICE% confirm
echo.
echo   live log: powershell Get-Content "%LOGDIR%\collector.out.log" -Wait -Tail 20
echo   check   : "%PYTHON%" "%PROJDIR%\collector.py" --inspect "%DATADIR%"
echo.
echo Verify in 15 minutes that records are accumulating. If the data folder is
echo empty, read collector.err.log - it is almost always a path or venv issue.
echo.

endlocal