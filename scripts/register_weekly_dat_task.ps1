param(
    [string]$TaskName = "NSW Weekly DAT Property Sales Upload",
    [string]$ProjectDir = "D:\projects\six_nsw_property_download",
    [string]$DbConnectorSrc = "D:\projects\db_connector\src",
    [string]$RunTime = "06:00",
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Tuesday"
)

$ErrorActionPreference = "Stop"

$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

$LogDir = Join-Path $ProjectDir "logs"
$WorkDir = Join-Path $ProjectDir "data\valuation_weekly"
$WeeklyScript = Join-Path $ProjectDir "scripts\weekly_dat_upload.py"
$TaskOutputLog = Join-Path $LogDir "weekly_dat_upload_task_output.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

if (-not (Test-Path $WeeklyScript)) {
    throw "Weekly DAT upload script not found: $WeeklyScript"
}
if (-not (Test-Path $DbConnectorSrc)) {
    throw "db_connector src directory not found: $DbConnectorSrc"
}

$TaskArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$ProjectDir'; & '$PythonExe' '$WeeklyScript' --latest --work-dir '$WorkDir' --log-file '$LogDir\weekly_dat_upload.log' --log-level INFO --db-connector-src '$DbConnectorSrc' *>&1 | Tee-Object -FilePath '$TaskOutputLog' -Append; exit `$LASTEXITCODE"
)

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($TaskArgs -join " ")
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $RunTime
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Download the latest NSW Valuer General weekly DAT property sales ZIP and upload it to PostgreSQL." -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' for every $DayOfWeek at $RunTime."
Write-Host "Command uses: $PythonExe"
Write-Host "db_connector src: $DbConnectorSrc"
Write-Host "Log file: $LogDir\weekly_dat_upload.log"
Write-Host "Task output log: $TaskOutputLog"
