@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Discord] Auto-send predictions for today's races
echo  Sends 30 min before each race automatically
echo  Press Ctrl+C to stop
echo.
python daily_discord.py %*
echo.
pause