# Install X Agent (Nagi) as a Windows Scheduled Task
# Run as Administrator on RemotePC
#
# Creates a task that:
# - Starts at system boot (logon)
# - Restarts on failure
# - Runs the agent in the background

param(
    [string]$Config = "configs/nagi.yaml",
    [string]$TaskName = "XAgent-Nagi",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$AgentDir = "C:\Users\USER\x-agent"
$Python = "C:\Users\USER\AppData\Local\Microsoft\WindowsApps\python.exe"
$LogFile = "$AgentDir\data\logs\service.log"

if ($Uninstall) {
    Write-Host "Removing scheduled task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Done."
    exit 0
}

# Create a wrapper script that the task will execute
$WrapperScript = @"
@echo off
cd /d $AgentDir
echo [%date% %time%] Starting X Agent >> $LogFile
$Python run.py --config $Config >> $LogFile 2>&1
echo [%date% %time%] X Agent exited with code %errorlevel% >> $LogFile
"@

$WrapperPath = "$AgentDir\scripts\run_service.bat"
Set-Content -Path $WrapperPath -Value $WrapperScript -Encoding ASCII

# Create the scheduled task
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$WrapperPath`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "USER"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 3 `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Register new task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User "USER" `
    -Password "Password123!" `
    -RunLevel Highest `
    -Description "X Agent (Nagi) - Autonomous coffee persona bot"

Write-Host "Scheduled task '$TaskName' created successfully."
Write-Host "  Trigger: At logon (USER)"
Write-Host "  Restart: Every 5 min on failure, max 3 retries"
Write-Host "  Log: $LogFile"
Write-Host ""
Write-Host "To start now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To check:     Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "To remove:    .\install_service.ps1 -Uninstall"
