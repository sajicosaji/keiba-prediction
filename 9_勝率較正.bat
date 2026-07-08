@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Calibrate] Fitting win-probability temperature (10-20 min)
echo  Run after 4_ƒ‚ƒfƒ‹ÄŒP—û.bat (model retrain)
echo  Result: data\calibration.json (used by predict.py for EV)
echo.
python calibrate_temperature.py %*
echo.
pause
