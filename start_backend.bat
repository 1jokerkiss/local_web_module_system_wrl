@echo off
cd /d "%~dp0backend"

if not exist .venv (
  python -m venv .venv
)

call .venv\Scripts\activate
call pip install -r requirements.txt

if not exist "%~dp0frontend\dist" (
  cd /d "%~dp0frontend"
  call npm install
  call npm run build
  cd /d "%~dp0backend"
)

start http://127.0.0.1:8000
uvicorn app.main:app --port 8000
pause