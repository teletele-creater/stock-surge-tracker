$taskDir = "C:\Users\moris\Documents\test\stock_memo"

# 大引け15:00の後、yfinanceの当日足が確定するのを待って16:10に回す。
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c run_drop_analysis.bat" -WorkingDirectory $taskDir

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 4:10PM

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName "StockDrop_Daily" -Action $action -Trigger $trigger -Settings $settings -Description "Detect daily stock drops after the close (weekdays 16:10)" -Force

Write-Host "OK: StockDrop_Daily registered (Weekdays 16:10)" -ForegroundColor Green
