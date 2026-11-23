@echo off
setlocal

set "script=%~dp0codex-backup-restore.py"
if not exist "%script%" (
    echo codex-backup-restore.py must be beside this launcher.
    pause
    exit /b 1
)

py -3.14 -c "from compression import zstd; import tkinter" >nul 2>&1
if not errorlevel 1 (
    set "runner=py -3.14"
    goto launch
)
py -3 -c "from compression import zstd; import tkinter" >nul 2>&1
if not errorlevel 1 (
    set "runner=py -3"
    goto launch
)
python -c "from compression import zstd; import tkinter" >nul 2>&1
if not errorlevel 1 (
    set "runner=python"
    goto launch
)
echo Install Python 3.14 or newer with Tcl/Tk to open Codex Backup ^& Restore.
pause
exit /b 1

:launch
%runner% -c "from pathlib import Path; import subprocess, sys; subprocess.Popen([str(Path(sys.executable).with_name('pythonw.exe')), sys.argv[1]])" "%script%"
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%
