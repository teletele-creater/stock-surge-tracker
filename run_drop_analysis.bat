@echo off
chcp 65001 > nul
title drop_analysis
set PYTHONIOENCODING=utf-8
cd /d C:\Users\moris\Documents\test\stock_memo

set LOGDIR=analysis_out\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%
set LOG=%LOGDIR%\drop_%STAMP%.log

echo ============================================ >> "%LOG%"
echo [START] %date% %time% >> "%LOG%"

rem Detect today's drops after the close, then analyze, save and notify.
rem sync_prices() runs on this path, so the price cache is refreshed here.
rem (A stale cache is why the intraday scan was removed from the watch loop.)
py -u drop_detector.py --notion --slack >> "%LOG%" 2>&1

echo [DONE] %date% %time% >> "%LOG%"
