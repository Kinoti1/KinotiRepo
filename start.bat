@echo off
setlocal
cd /d "%~dp0"
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "METASYNC_HOME=%LOCALAPPDATA%\Metasync"
set "METASYNC_PY=%METASYNC_HOME%\venv\Scripts\python.exe"
if not exist "%METASYNC_PY%" (
  if not exist "%METASYNC_HOME%" mkdir "%METASYNC_HOME%"
  echo Creating the Metasync Python environment...
  python -m venv "%METASYNC_HOME%\venv"
  if errorlevel 1 goto :error
)
echo Installing or updating Metasync dependencies...
"%METASYNC_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :error
start "" powershell.exe -WindowStyle Hidden -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'"
"%METASYNC_PY%" -m uvicorn api.main:app --host 127.0.0.1 --port 8000
goto :eof
:error
echo Metasync could not start. Check that Python 3.10 or newer is installed.
pause
