@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>&1
    if errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 exit /b 1
)
call ".venv\Scripts\activate.bat"
if errorlevel 1 exit /b 1

python -B -c "import fastapi, uvicorn, psutil, httpx, python_multipart, tzdata; from PIL import Image" >nul 2>&1
if errorlevel 1 (
    python -m pip install -r backend\requirements.txt
    if errorlevel 1 exit /b 1
)

if not exist "config.json" (
    copy /y "config.example.json" "config.json" >nul
    if errorlevel 1 exit /b 1
)

if not exist "backend\static\index.html" (
    where npm.cmd >nul 2>&1
    if errorlevel 1 (
        echo Node.js with npm is required to build the frontend.
        exit /b 1
    )
    pushd "frontend"
    if errorlevel 1 exit /b 1
    call npm.cmd ci
    if errorlevel 1 (
        popd
        exit /b 1
    )
    call npm.cmd run build
    if errorlevel 1 (
        popd
        exit /b 1
    )
    popd
)

python -B -m backend.main
exit /b %ERRORLEVEL%
