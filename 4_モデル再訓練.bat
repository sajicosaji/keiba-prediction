@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Train] Retraining AI model (20-90 min)
echo  Run after 3_データ更新.bat
echo  Option: --tune  (Optuna search, +30-60 min)
echo.
python train_model.py %*
echo.
pause