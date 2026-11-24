# Registers the daily task "ExtendElkanaLoans" (run as Administrator — one time).
# It runs C:\Users\Elisha\Desktop\CrazyBunny\Code Projects\Booking\run_extend.bat daily at 09:00.

$scriptDir = Split-Path -Parent $PSCommandPath
$batPath = Join-Path $scriptDir "run_extend.bat"
$taskName = "ExtendElkanaLoans"

Write-Host "Registering scheduled task '$taskName' (daily, 09:00)..."

$action = New-ScheduledTaskAction -Execute $batPath
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force

Write-Host "--- Verification:"
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, @{n='Triggers'; e={($_.Triggers | Select-Object -First 1).ToString()}}, State, @{n='RunLevel'; e={$_.Principal.RunLevel}} | Format-List

Write-Host "To test now: Start-ScheduledTask -TaskName $taskName"
