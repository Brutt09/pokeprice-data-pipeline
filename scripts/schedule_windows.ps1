# Registers a daily Windows scheduled task that starts the DB and runs the pipeline at 09:00.
# Remove with:  Unregister-ScheduledTask -TaskName PokePricePipeline -Confirm:$false
$root = Split-Path $PSScriptRoot -Parent
$cmd = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$root\scripts\dev_db.ps1'; & '$root\.venv\Scripts\python.exe' '$root\pipeline.py' *> '$root\data\pipeline.log'`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $cmd -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 9am
Register-ScheduledTask -TaskName "PokePricePipeline" -Action $action -Trigger $trigger -Description "PokéPrice daily pipeline run" -Force
