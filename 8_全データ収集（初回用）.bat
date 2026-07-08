@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Collect] Full race data collection (first time setup)
echo ----------------------------------------
echo  1. 2022-2026  full collection
echo  2. 2025-2026  partial update
echo  3. 2026 only  quick update
echo ----------------------------------------
set /p CHOICE="Choose (1/2/3): "

if "%CHOICE%"=="1" (
    python collect_data.py --years 2022 2023 2024 2025 2026
) else if "%CHOICE%"=="2" (
    python collect_data.py --years 2025 2026
) else (
    python collect_data.py --years 2026
)
echo.
echo Done! Next: run 4_ÉÇÉfÉãçƒåPó˚.bat
pause