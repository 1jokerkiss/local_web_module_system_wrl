@echo off
setlocal EnableExtensions

REM ==================================================
REM Auto-scaling backend starter.
REM This version does not hard-code LOCAL_WEB_MAX_PROCESS_SLOTS.
REM It detects CPU cores and memory, then calculates safe limits.
REM ==================================================

cd /d "%~dp0backend"
if errorlevel 1 (
  echo [ERROR] Cannot enter backend directory. Put this BAT file in the project root.
  pause
  exit /b 1
)

REM --------------------------------------------------
REM Find Python automatically
REM --------------------------------------------------
set "PYBASE="

if exist "D:\develop\python312\python.exe" (
  set "PYBASE=D:\develop\python312\python.exe"
)

if not defined PYBASE (
  for /f "usebackq delims=" %%p in (`py -3.12 -c "import sys; print(sys.executable)" 2^>nul`) do (
    if not defined PYBASE set "PYBASE=%%p"
  )
)

if not defined PYBASE (
  for /f "usebackq delims=" %%p in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
    if not defined PYBASE set "PYBASE=%%p"
  )
)

if not defined PYBASE (
  for /f "usebackq delims=" %%p in (`python -c "import sys; print(sys.executable)" 2^>nul`) do (
    if not defined PYBASE set "PYBASE=%%p"
  )
)

if not defined PYBASE (
  echo [ERROR] No usable Python found.
  echo Please install Python 3.10 or above.
  pause
  exit /b 1
)

echo [INFO] Using Python: %PYBASE%

REM --------------------------------------------------
REM Create and activate backend venv
REM --------------------------------------------------
if not exist ".venv" (
  "%PYBASE%" -m venv .venv
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo [ERROR] Failed to activate backend virtual environment.
  pause
  exit /b 1
)

REM --------------------------------------------------
REM PyInstaller temp directory
REM --------------------------------------------------
set "PYI_TMP=D:\pyi_tmp"
set "TMP=%PYI_TMP%"
set "TEMP=%PYI_TMP%"
set "TMPDIR=%PYI_TMP%"

if not exist "%PYI_TMP%" mkdir "%PYI_TMP%"
for /d %%i in ("%PYI_TMP%\_MEI*") do rmdir /s /q "%%i" 2>nul

REM --------------------------------------------------
REM Runtime input link policy.
REM Important: do NOT copy large input files into backend\runtime.
REM hardlink is preferred. symlink is used when hardlink is not possible.
REM LOCAL_WEB_INPUT_LINK_FALLBACK=error prevents older backend code from falling back to copy2.
REM --------------------------------------------------
set "LOCAL_WEB_INPUT_LINK_ORDER=symlink"
set "LOCAL_WEB_ALLOW_INPUT_SYMLINKS=1"
set "LOCAL_WEB_INPUT_LINK_FALLBACK=error"
set "LOCAL_WEB_ALLOW_INPUT_HARDLINKS=0"


REM --------------------------------------------------
REM Auto calculate scheduler limits from current machine
REM No fixed max slot value is written here.
REM You can override memory estimate by setting:
REM LOCAL_WEB_MEMORY_PER_WORKER_GB before this section.
REM --------------------------------------------------
if not defined LOCAL_WEB_MEMORY_PER_WORKER_GB set "LOCAL_WEB_MEMORY_PER_WORKER_GB=3"

for /f "usebackq delims=" %%L in (`python -c "import os, ctypes, math; class M(ctypes.Structure): _fields_=[('dwLength',ctypes.c_ulong),('dwMemoryLoad',ctypes.c_ulong),('ullTotalPhys',ctypes.c_ulonglong),('ullAvailPhys',ctypes.c_ulonglong),('ullTotalPageFile',ctypes.c_ulonglong),('ullAvailPageFile',ctypes.c_ulonglong),('ullTotalVirtual',ctypes.c_ulonglong),('ullAvailVirtual',ctypes.c_ulonglong),('sullAvailExtendedVirtual',ctypes.c_ulonglong)]; m=M(); m.dwLength=ctypes.sizeof(M); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)); cpu=os.cpu_count() or 1; total_gb=m.ullTotalPhys/(1024**3); reserve_gb=max(4.0,total_gb*0.20); per=float(os.environ.get('LOCAL_WEB_MEMORY_PER_WORKER_GB','3') or 3); by_mem=max(1,int((total_gb-reserve_gb)//per)); by_cpu=max(1,int(cpu*0.75)); max_slots=max(1,min(by_cpu,by_mem)); suggested=max(1,int(max_slots*0.5)); total_threads=max(1,int(cpu*0.75)); max_threads_per_child=max(1,min(4,max(1,total_threads//max(1,suggested)))); print(f'set LOCAL_WEB_DETECTED_CPU_COUNT={cpu}'); print(f'set LOCAL_WEB_DETECTED_MEMORY_GB={total_gb:.1f}'); print(f'set LOCAL_WEB_SUGGESTED_PROCESS_SLOTS={suggested}'); print(f'set LOCAL_WEB_MAX_PROCESS_SLOTS={max_slots}'); print(f'set LOCAL_WEB_TOTAL_COMPUTE_THREADS={total_threads}'); print(f'set LOCAL_WEB_MAX_THREADS_PER_CHILD={max_threads_per_child}')"`) do (
  %%L
)

echo [INFO] Detected CPU cores: %LOCAL_WEB_DETECTED_CPU_COUNT%
echo [INFO] Detected memory GB: %LOCAL_WEB_DETECTED_MEMORY_GB%
echo [INFO] Suggested process slots: %LOCAL_WEB_SUGGESTED_PROCESS_SLOTS%
echo [INFO] Max process slots: %LOCAL_WEB_MAX_PROCESS_SLOTS%
echo [INFO] Total compute threads: %LOCAL_WEB_TOTAL_COMPUTE_THREADS%
echo [INFO] Max threads per child: %LOCAL_WEB_MAX_THREADS_PER_CHILD%

REM --------------------------------------------------
REM Queue and scheduler policy
REM --------------------------------------------------
set "LOCAL_WEB_CPU_QUEUE_THRESHOLD=99"
set "LOCAL_WEB_UTIL_SCHEDULER=1"

REM Utilization-aware scheduler thresholds
set "LOCAL_WEB_UTIL_CPU_LOW=60"
set "LOCAL_WEB_UTIL_CPU_HIGH=92"
set "LOCAL_WEB_UTIL_MEMORY_SOFT=78"
set "LOCAL_WEB_UTIL_MEMORY_HARD=88"
set "LOCAL_WEB_UTIL_IO_READ_SOFT_MB_S=120"
set "LOCAL_WEB_UTIL_IO_READ_HARD_MB_S=400"
set "LOCAL_WEB_UTIL_IO_WRITE_SOFT_MB_S=60"
set "LOCAL_WEB_UTIL_IO_WRITE_HARD_MB_S=300"
set "LOCAL_WEB_UTIL_SCALE_UP_SAMPLES=2"
set "LOCAL_WEB_UTIL_SCALE_UP_COOLDOWN_SECONDS=8"
set "LOCAL_WEB_UTIL_SCALE_DOWN_COOLDOWN_SECONDS=5"

REM BLAS / OpenMP thread limits.
REM The platform assigns per-child thread budgets through runtime env.
REM These global values prevent accidental thread explosion.
set "OPENBLAS_NUM_THREADS=1"
set "OMP_NUM_THREADS=1"
set "MKL_NUM_THREADS=1"
set "GOTO_NUM_THREADS=1"
set "NUMEXPR_NUM_THREADS=1"

REM I/O stagger scheduling
set "LOCAL_WEB_IO_CPU_LOW_THRESHOLD=55"
set "LOCAL_WEB_IO_MEMORY_THRESHOLD=80"
set "LOCAL_WEB_IO_MIN_AVAILABLE_MEMORY_GB=3"
set "LOCAL_WEB_IO_READ_MB_S_THRESHOLD=120"
set "LOCAL_WEB_IO_WRITE_MB_S_THRESHOLD=60"
set "LOCAL_WEB_IO_DISK_BUSY_THRESHOLD=70"

REM Child task launch protection
set "LOCAL_WEB_CHILD_START_STAGGER_SECONDS=6"
set "LOCAL_WEB_CHILD_START_WAIT_SECONDS=3"
set "LOCAL_WEB_CHILD_START_CPU_THRESHOLD=96"
set "LOCAL_WEB_CHILD_START_MEMORY_THRESHOLD=88"
set "LOCAL_WEB_CHILD_START_MIN_MEMORY_GB=3"
set "LOCAL_WEB_CHILD_START_MIN_DISK_FREE_GB=10"

REM Adaptive child task start
set "LOCAL_WEB_ADAPTIVE_CHILD_START=1"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_MIN_SECONDS=5"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_MAX_SECONDS=60"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_SAMPLE_SECONDS=1.5"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_CPU_DECLINE=10"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_STABLE_SAMPLES=3"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_MAX_PROBE_SECONDS=90"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_MIN_PEAK_CPU=60"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_MEMORY_THRESHOLD=90"
set "LOCAL_WEB_ADAPTIVE_CHILD_START_MIN_MEMORY_GB=1"

REM Python output encoding
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

REM --------------------------------------------------
REM Install dependencies
REM --------------------------------------------------
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install psutil

REM --------------------------------------------------
REM Build frontend if dist does not exist
REM --------------------------------------------------
if not exist "%~dp0frontend\dist" (
  cd /d "%~dp0frontend"
  call npm install
  call npm run build
  cd /d "%~dp0backend"
)

REM --------------------------------------------------
REM Start server
REM --------------------------------------------------
start "" http://127.0.0.1:8000
python -m uvicorn app.main:app --port 8000

pause
