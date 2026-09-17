@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "VENV_PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo Creating Python virtual environment in .venv...
    where py >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python launcher "py" was not found.
        echo Install Python 3 for Windows, then run this file again.
        exit /b 1
    )

    py -3 -m venv "%PROJECT_DIR%.venv"
    if errorlevel 1 (
        echo ERROR: Could not create the virtual environment.
        exit /b 1
    )
)

"%VENV_PYTHON%" -c "import matplotlib, numpy, requests, scipy" >nul 2>&1
if errorlevel 1 (
    echo Installing MLX90393 visualizer dependencies...
    "%VENV_PYTHON%" -m pip install -r "%PROJECT_DIR%requirements-mlx90393-client.txt"
    if errorlevel 1 (
        echo ERROR: Dependency installation failed.
        exit /b 1
    )
)

"%VENV_PYTHON%" "%PROJECT_DIR%mlx90393_visualizer.py" %*
exit /b %errorlevel%
