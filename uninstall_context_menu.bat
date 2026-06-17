@echo off
title Uninstall Context Menu
echo ========================================================
echo        Remove "Open in Visual Video Editor"
echo ========================================================
echo.
echo This will remove the option to right-click media files in
echo Windows and open them directly in your video editor.
echo.
pause

:: Remove registry keys for the current user
reg delete "HKCU\Software\Classes\*\shell\VisualVideoEditor" /f

echo.
echo [SUCCESS] Context menu option removed!
echo.
pause