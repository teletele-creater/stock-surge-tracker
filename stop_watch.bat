@echo off
taskkill /F /FI "WINDOWTITLE eq stock_watch" >nul 2>&1
taskkill /F /IM "python.exe" /FI "WINDOWTITLE eq stock_watch" >nul 2>&1
:: main.pyのプロセスを確実に停止
for /f "tokens=2" %%i in ('wmic process where "commandline like '%%main.py%%--watch%%'" get processid /value 2^>nul ^| find "="') do (
    taskkill /F /PID %%i >nul 2>&1
)
echo Stock watch stopped.
