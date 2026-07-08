@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Backtest] ROI analysis on historical data
echo  Options:
echo    --test-year 2025
echo    --grade g1g2   (G1/G2 only)
echo    --grade all    (all races)
echo.
python backtest.py %*
echo.
pause