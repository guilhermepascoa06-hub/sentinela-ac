param(
    [Parameter(Mandatory = $true)]
    [string]$PostgresBin,
    [switch]$PrepareOnly,
    [switch]$Stop,
    [switch]$SkipQuality
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$clusterRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'sentinela-ac-pg-test-55439'
$dataRoot = Join-Path $clusterRoot 'data'
$statePath = Join-Path $clusterRoot 'state.json'
$markerPath = Join-Path $clusterRoot 'sentinela-disposable-test-cluster.txt'
$pgPort = 55439
$pgUser = 'sentinela_test'
$pgDatabase = 'sentinela_test'
$pgCtl = Join-Path $PostgresBin 'pg_ctl.exe'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

foreach ($binary in @('postgres.exe', 'initdb.exe', 'pg_ctl.exe', 'psql.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PostgresBin $binary) -PathType Leaf)) {
        throw "PostgreSQL binary missing: $binary"
    }
}

function Invoke-HiddenPostgres([string]$Executable, [string[]]$Arguments) {
    # Start-Process does not quote individual arguments on Windows.
    $quoted = @($Arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' })
    $process = Start-Process -FilePath $Executable -ArgumentList $quoted -WindowStyle Hidden `
        -PassThru -RedirectStandardOutput (Join-Path $clusterRoot 'command.out.log') `
        -RedirectStandardError (Join-Path $clusterRoot 'command.err.log')
    # -Wait waits for the full process tree, including the server pg_ctl starts.
    # WaitForExit waits only for pg_ctl/initdb so a running server cannot hang setup.
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        $detail = Get-Content -LiteralPath (Join-Path $clusterRoot 'command.err.log') -Raw
        throw "PostgreSQL command failed ($($process.ExitCode)): $detail"
    }
}

if ($Stop) {
    if (-not (Test-Path -LiteralPath $markerPath)) { throw 'Disposable cluster marker missing' }
    if (Test-Path -LiteralPath (Join-Path $dataRoot 'postmaster.pid')) {
        Invoke-HiddenPostgres $pgCtl @('-D', $dataRoot, '-m', 'fast', '-w', 'stop')
    }
    Write-Output "Disposable PostgreSQL stopped; files preserved in $clusterRoot"
    return
}

if (Test-Path -LiteralPath $clusterRoot) {
    if (-not (Test-Path -LiteralPath $markerPath)) { throw 'Refusing an unrecognized cluster directory' }
} else {
    New-Item -ItemType Directory -Path $clusterRoot | Out-Null
    Set-Content -LiteralPath $markerPath -Value 'Sentinela AC disposable local test cluster only' -Encoding utf8
}

if (-not (Test-Path -LiteralPath $statePath)) {
    if (Test-Path -LiteralPath $dataRoot) { throw 'Cluster data exists without its test state; refusing to reuse' }
    $testPassword = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    $passwordPath = Join-Path $clusterRoot 'init-password.txt'
    try {
        Set-Content -LiteralPath $passwordPath -Value $testPassword -Encoding ascii
        Invoke-HiddenPostgres (Join-Path $PostgresBin 'initdb.exe') @(
            '-D', $dataRoot, '-U', $pgUser, '--encoding=UTF8', '--locale=C',
            '--auth=scram-sha-256', '--pwfile', $passwordPath
        )
    } finally {
        if (Test-Path -LiteralPath $passwordPath) { Remove-Item -LiteralPath $passwordPath }
    }
    @{
        password = $testPassword
        port = $pgPort
        user = $pgUser
        database = $pgDatabase
    } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    Add-Content -LiteralPath (Join-Path $dataRoot 'postgresql.conf') -Value @"
listen_addresses = '127.0.0.1'
port = $pgPort
max_connections = 20
shared_buffers = '32MB'
"@ -Encoding ascii
}

$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if ($state.port -ne $pgPort -or $state.user -ne $pgUser -or $state.database -ne $pgDatabase) {
    throw 'Unexpected disposable cluster state'
}
& $pgCtl -D $dataRoot status *> $null
if ($LASTEXITCODE -ne 0) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $pgPort)
    try { $listener.Start() } finally { $listener.Stop() }
    Invoke-HiddenPostgres $pgCtl @('-D', $dataRoot, '-l', (Join-Path $clusterRoot 'server.log'), '-w', 'start')
}

$previousPassword = $env:PGPASSWORD
try {
    $env:PGPASSWORD = $state.password
    $exists = & (Join-Path $PostgresBin 'psql.exe') -h 127.0.0.1 -p $pgPort -U $pgUser `
        -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'sentinela_test'"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot connect to the disposable PostgreSQL cluster' }
    if ($exists -ne '1') {
        & (Join-Path $PostgresBin 'psql.exe') -h 127.0.0.1 -p $pgPort -U $pgUser `
            -d postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE sentinela_test'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create the disposable test database' }
    }
} finally {
    $env:PGPASSWORD = $previousPassword
}

Write-Output "Disposable PostgreSQL ready: 127.0.0.1:$pgPort / $pgDatabase"
Write-Output "Generated test-only credentials are in $statePath (outside the repository)."
if ($PrepareOnly) { return }
if (-not (Test-Path -LiteralPath $python)) { throw 'Project .venv Python not found' }

function Invoke-Check([string[]]$Arguments) {
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Check failed: python $($Arguments -join ' ')" }
}

$isolatedKeys = @(
    'DATABASE_URL', 'TEST_DATABASE_URL', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
    'SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'EMAIL_FROM', 'EMAIL_TO',
    'LLM_BASE_URL', 'LLM_API_KEY', 'GITHUB_TOKEN', 'GITHUB_REPOSITORY',
    'BACKUP_ENCRYPTION_KEY', 'WATCHDOG_PING_URL', 'PGHOST', 'PGHOSTADDR', 'PGPORT',
    'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSERVICE', 'PGSERVICEFILE'
)
$savedEnvironment = @{}
foreach ($key in $isolatedKeys) {
    $savedEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    [Environment]::SetEnvironmentVariable($key, '', 'Process')
}
Push-Location $projectRoot
try {
    if (-not $SkipQuality) {
        Invoke-Check @('-m', 'ruff', 'check', 'src', 'tests')
        Invoke-Check @('-m', 'ruff', 'format', '--check', 'src', 'tests')
        Invoke-Check @('-m', 'mypy')
        Invoke-Check @('-m', 'bandit', '-q', '-c', 'pyproject.toml', '-r', 'src')
    }
    $env:DATABASE_URL = 'sqlite+pysqlite:///:memory:'
    $env:TEST_DATABASE_URL = 'sqlite+pysqlite:///:memory:'
    Invoke-Check @('-m', 'pytest', '-q')

    # Assemble the generated test URL at runtime; never copy the production .env URL.
    $testUrl = 'postgresql+psycopg://' + $pgUser + ':' + $state.password +
        '@127.0.0.1:' + $pgPort + '/' + $pgDatabase
    $env:DATABASE_URL = $testUrl
    $env:TEST_DATABASE_URL = $testUrl
    Invoke-Check @('-m', 'alembic', 'upgrade', 'head')
    Invoke-Check @('-m', 'alembic', 'downgrade', 'base')
    Invoke-Check @('-m', 'alembic', 'upgrade', 'head')
    Invoke-Check @('-m', 'alembic', 'check')
    Invoke-Check @('-m', 'pytest', '-q')
} finally {
    Pop-Location
    foreach ($key in $isolatedKeys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
}
