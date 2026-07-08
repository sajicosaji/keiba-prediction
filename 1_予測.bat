@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Predict] Race Prediction
echo ----------------------------------------
echo  1. Select from race list (recommended)
echo  2. Enter race ID manually
echo ----------------------------------------
set /p MODE="Choose (1 or 2): "

if "%MODE%"=="1" (
    python select_race.py
) else (
    echo.
    echo Enter 12-digit race ID  e.g. 202506010801
    echo.
    set /p RACE_ID="Race ID: "
    echo.
    python predict.py %RACE_ID% --discord
)
echo.
pause