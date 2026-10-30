$taskDir = "C:\Users\moris\Documents\test\stock_memo"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c cd /d $taskDir & py -u sync_ohlc.py --recent 30 >> kyutou_logs\sync_ohlc.log 2>&1" -WorkingDirectory $taskDir

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 4:00PM

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "StockOHLC_Sync" -Action $action -Trigger $trigger -Settings $settings -Description "Sync OHLC to Supabase weekdays 16:00" -Force

Write-Host "OK: StockOHLC_Sync registered (Weekdays 16:00)" -ForegroundColor Green
