@echo off
title Visual Video Editor

:: Switch working directory to where this batch file is located to prevent VENV bleeding
cd /d "%~dp0"

echo ========================================================
echo               Visual Video Editor Launcher
echo ========================================================
echo.

:: 1. Check if Python is installed and accessible
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found on your system!
    echo.
    echo Please download and install Python from https://www.python.org/downloads/
    echo IMPORTANT: Make sure to check the box that says "Add Python.exe to PATH" at the bottom of the installer!
    echo.
    pause
    exit /b
)

:: 2. Check for the virtual environment and create it if missing
if not exist venv\Scripts\python.exe (
    echo [INFO] First time setup detected. Creating an isolated Python environment...
    echo [INFO] This might take a minute...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b
    )
)

:: 3. Install required packages (runs quietly unless there is an error)
echo [INFO] Checking and installing required packages (moviepy, Pillow, tkinterdnd2, tkinter, ffmpeg-python, imageio-ffmpeg, customtkinter, sounddevice, numpy)...
venv\Scripts\pip install moviepy Pillow tkinterdnd2 tkinter ffmpeg-python imageio-ffmpeg customtkinter sounddevice numpy --disable-pip-version-check --quiet

:: 4. Run the Python application and pass any command line arguments
echo [INFO] Starting the Video Editor...
echo.
venv\Scripts\python visual_video_editor.py %*

:: 5. Keep the window open ONLY if the app crashes so the user can read the error
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The application crashed or was closed with an error.
    pause
)