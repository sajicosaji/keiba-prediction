@echo off
chcp 932 > nul
cd /d "%~dp0src"
echo [Pedigree] Collecting sire / dam-sire data
echo  Incremental - safe to stop and resume
echo  Example: 6_ŒŒ“ûW.bat --limit 200
echo.
python collect_pedigrees.py %*
echo.
pause