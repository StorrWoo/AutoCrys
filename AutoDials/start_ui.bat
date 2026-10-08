@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

if defined AUTODIALS_PYTHON (
    set "PYTHON=%AUTODIALS_PYTHON%"
) else (
    set "PYTHON=C:\dials\python.exe"
)

if not exist "%PYTHON%" (
    echo AutoDials UI Python not found: %PYTHON%
    echo Install DIALS at C:\dials or set AUTODIALS_PYTHON to a Python with tkinter.
    pause
    exit /b 1
)

"%PYTHON%" "%SCRIPT_DIR%mini_ui.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
