@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -Command "try { $s = Invoke-RestMethod 'http://127.0.0.1:8765/api/health' -TimeoutSec 2; if ($s.status -eq 'ok' -and $s.schema -and $s.version) { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
  echo TeamManager is already running at http://127.0.0.1:8765
  start "" "http://127.0.0.1:8765"
  exit /b 0
)
if not exist .venv\Scripts\python.exe (
  python -m venv .venv
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -m pip install -r requirements.lock
if errorlevel 1 goto fail
.venv\Scripts\python.exe scripts\init_config.py
if errorlevel 1 goto fail
set "PATH=%ProgramFiles%\nodejs;%PATH%"
pushd frontend
call npm.cmd ci
if errorlevel 1 (popd & goto fail)
call npm.cmd run build
if errorlevel 1 (popd & goto fail)
popd
echo Open http://127.0.0.1:8765
echo Initial password: open .env and read TM_ADMIN_PASSWORD.
.venv\Scripts\python.exe -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8765
exit /b %errorlevel%
:fail
echo Startup failed. See the error above.
pause
exit /b 1
