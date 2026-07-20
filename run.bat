@echo off
rem Ultimate Sacrifice launcher (cmd.exe): sets up a venv, installs deps, then runs.
rem Any arguments are passed through, e.g.  run.bat --root "C:\Users\me" --provider ollama
setlocal
cd /d "%~dp0"

set "VENV=.venv"
set "PYEXE=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.us-installed"

if not exist "%PYEXE%" (
    echo [setup] Creating virtual environment in %VENV% ...
    py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%"
    if errorlevel 1 goto :venvfail
)

if not exist "%STAMP%" (
    echo [setup] Installing dependencies ^(first run only^) ...
    "%PYEXE%" -m pip install --upgrade pip
    "%PYEXE%" -m pip install -e .
    if errorlevel 1 goto :installfail
    echo installed> "%STAMP%"
)

"%PYEXE%" -m ultimate_sacrifice %*
exit /b %errorlevel%

:venvfail
echo [error] Could not create a virtual environment.
echo         Install Python 3.11+ and make sure "py" or "python" is on your PATH.
pause
exit /b 1

:installfail
echo [error] Dependency installation failed. See the output above.
pause
exit /b 1
