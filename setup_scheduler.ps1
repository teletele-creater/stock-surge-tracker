$taskDir = "C:\Users\moris\Documents\test\stock_memo"

$startAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c title stock_watch & cd /d $taskDir & py -u main.py --watch 5 --auto-post" -WorkingDirectory $taskDir

$startTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00AM

$startSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "StockWatch_Start" -Action $startAction -Trigger $startTrigger -Settings $startSettings -Description "Stock watch start weekdays 9AM" -Force

Write-Host "OK: StockWatch_Start registered (Weekdays 9:00)" -ForegroundColor Green

$stopAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $taskDir\stop_watch.bat" -WorkingDirectory $taskDir

$stopTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 3:30PM

$stopSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "StockWatch_Stop" -Action $stopAction -Trigger $stopTrigger -Settings $stopSettings -Description "Stock watch stop weekdays 3:30PM" -Force

Write-Host "OK: StockWatch_Stop registered (Weekdays 15:30)" -ForegroundColor Green
Write-Host ""
Write-Host "Done! Auto watch: Weekdays 9:00-15:30" -ForegroundColor Cyan
