@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Update] Adding latest race results to races.csv
python update_data.py
echo.
pause