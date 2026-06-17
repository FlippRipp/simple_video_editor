@echo off
title Install Context Menu
echo ========================================================
echo        Install "Open in Visual Video Editor"
echo ========================================================
echo.
echo This will add an option to right-click media files in
echo Windows and open them directly in your video editor.
echo.
pause

set "LAUNCHER_PATH=%~dp0windows_launcher.bat"

:: Add registry keys for the current user
reg add "HKCU\Software\Classes\*\shell\VisualVideoEditor" /ve /d "Open in Visual Video Editor" /f
reg add "HKCU\Software\Classes\*\shell\VisualVideoEditor\command" /ve /d "\"%LAUNCHER_PATH%\" \"%%1\"" /f

echo.
echo [SUCCESS] Context menu option installed!
echo Try right-clicking a video file to test it out.
echo.
pause