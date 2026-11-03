$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
Get-Command docker -ErrorAction Stop | Out-Null
$directory = Join-Path $root '.deployment'
New-Item -ItemType Directory -Force $directory | Out-Null
$envFile = Join-Path $directory 'local.env'
if (-not (Test-Path $envFile)) {
    $values = @(
        "LOCAL_ADMIN_PASSWORD=$([guid]::NewGuid().ToString('N'))",
        "LOCAL_RUNTIME_PASSWORD=$([guid]::NewGuid().ToString('N'))"
    )
    [System.IO.File]::WriteAllLines($envFile, $values)
}
$local = @{}
Get-Content $envFile | ForEach-Object {
    $key, $value = $_ -split '=', 2
    $local[$key] = $value
}
& docker compose --env-file $envFile up -d --wait postgres
if ($LASTEXITCODE -ne 0) { throw 'Local PostgreSQL startup failed.' }
& docker compose --env-file $envFile run --build --rm bootstrap
if ($LASTEXITCODE -ne 0) { throw 'Local database bootstrap failed.' }
$env:APP_MODE = 'local'
$env:PUBLIC_URL = 'http://127.0.0.1:8000'
$env:PGHOST = 'localhost'
$env:PGPORT = '5433'
$env:PGDATABASE = 'mcpdemo'
$env:PGUSER = 'mcp_local'
$env:PGPASSWORD = $local.LOCAL_RUNTIME_PASSWORD
try {
    & "$root\.venv\Scripts\python.exe" -m app.server
    if ($LASTEXITCODE -ne 0) { throw 'Local MCP server failed.' }
}
finally {
    foreach ($key in @('APP_MODE', 'PUBLIC_URL', 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD')) {
        Remove-Item "Env:\$key" -ErrorAction SilentlyContinue
    }
}
