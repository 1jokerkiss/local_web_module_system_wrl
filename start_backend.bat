@echo off
cd /d "%~dp0backend"

set PY312=D:\develop\python312\python.exe

if not exist .venv (
  "%PY312%" -m venv .venv
)

call .venv\Scripts\activate
python -m pip install -r requirements.txt

if not exist "%~dp0frontend\dist" (
  cd /d "%~dp0frontend"
  call npm install
  call npm run build
  cd /d "%~dp0backend"
)

start http://127.0.0.1:8000
python -m uvicorn app.main:app --port 8000
pause