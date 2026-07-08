@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Check] Betting recommendation performance
echo  Requires 3_データ更新.bat to have run after the races
echo.
python check_results.py %*
echo.
pause
