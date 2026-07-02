@echo off
REM ============================================================================
REM AUREON self-restart launcher (Fix 4 / E-12, Level 3 "never blind").
REM
REM The bot exits with code 42 ONLY on a controlled feed-death self-restart
REM (feed went blind -> re-subscribe failed -> full MT5 reinit failed -> state
REM persisted -> sys.exit(42)). This loop RELAUNCHES on 42 and STOPS on any
REM other exit code (a clean /stop, an operator Ctrl-C, or an unhandled crash
REM you want to see rather than silently respawn).
REM
REM Exit-code contract:
REM   42          -> controlled feed self-restart: relaunch.
REM   0 / other   -> stop the loop, surface the exit code.
REM
REM Open positions stay protected by their broker-side SL across the restart,
REM and run/state.json lets the relaunched process recover same-day (Fix 5).
REM Never self-restarts when the market is closed (weekend), so this loop only
REM ever spins on a genuine open-market feed death.
REM ============================================================================
setlocal enabledelayedexpansion

REM --- edit these two lines for your install ---------------------------------
set "AUREON_DIR=C:\A02-PR"
set "AUREON_LOT=0.35"
REM ---------------------------------------------------------------------------

cd /d "%AUREON_DIR%"

:loop
echo [run_aureon] starting AUREON at %date% %time%
python bot.py live --lot %AUREON_LOT% --i-understand-the-risks
set "EXITCODE=%ERRORLEVEL%"
echo [run_aureon] AUREON exited with code %EXITCODE% at %date% %time%

if "%EXITCODE%"=="42" (
    echo [run_aureon] exit 42 = controlled feed self-restart -- relaunching in 5s...
    timeout /t 5 /nobreak >nul
    goto loop
)

echo [run_aureon] exit %EXITCODE% is not 42 -- stopping the launcher loop.
endlocal
exit /b %EXITCODE%
