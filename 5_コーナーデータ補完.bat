@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Corner] Backfilling c1-c4 corner data into races.csv
echo  Run multiple times to fill gradually
echo  Example: 5_コーナーデータ補完.bat --limit 500
echo  Press Ctrl+C to stop safely
echo.
python update_corner_data.py %*
echo.
pause