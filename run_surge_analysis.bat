@echo off
chcp 65001 > nul
title surge_analysis
set PYTHONIOENCODING=utf-8
cd /d C:\Users\moris\Documents\test\stock_memo

set LOGDIR=analysis_out\logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set STAMP=%date:~0,4%%date:~5,2%%date:~8,2%
set LOG=%LOGDIR%\run_%STAMP%.log

echo ============================================ >> "%LOG%"
echo [START] %date% %time% >> "%LOG%"

echo [1/2] surge_outcome_analysis.py >> "%LOG%"
py -u surge_outcome_analysis.py >> "%LOG%" 2>&1

echo [2/2] dip_rebound_analysis.py >> "%LOG%"
py -u dip_rebound_analysis.py >> "%LOG%" 2>&1

echo [3/3] notify_analysis_slack.py >> "%LOG%"
py -u notify_analysis_slack.py >> "%LOG%" 2>&1

echo [DONE] %date% %time% >> "%LOG%"
