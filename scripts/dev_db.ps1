# Starts the project's own Postgres (no Docker, no admin rights) on port 5433.
# First run creates the cluster in .pgdata/. Uses the PostgreSQL 18 binaries already installed.
$ErrorActionPreference = "Stop"
$bin = "C:\Program Files\PostgreSQL\18\bin"
$root = Split-Path $PSScriptRoot -Parent
$data = Join-Path $root ".pgdata"
if (-not (Test-Path $data)) {
    $pw = New-TemporaryFile
    Set-Content -Path $pw -Value "pokeprice" -NoNewline
    & "$bin\initdb.exe" -D $data -U pokeprice --pwfile=$pw -A scram-sha-256 -E UTF8 --locale=C | Out-Null
    Remove-Item $pw
    & "$bin\pg_ctl.exe" -D $data -o "-p 5433" -l "$data\server.log" start -w
    $env:PGPASSWORD = "pokeprice"
    & "$bin\psql.exe" -h localhost -p 5433 -U pokeprice -d postgres -c "create database pokeprice;"
} else {
    & "$bin\pg_ctl.exe" -D $data status | Out-Null
    if ($LASTEXITCODE -ne 0) { & "$bin\pg_ctl.exe" -D $data -o "-p 5433" -l "$data\server.log" start -w }
    else { Write-Host "Postgres already running on 5433" }
}
