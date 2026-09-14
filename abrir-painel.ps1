$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$panelUrl = 'http://127.0.0.1:8765'
$panelExe = Join-Path $projectRoot '.venv\Scripts\sentinela.exe'
$panelLogs = Join-Path $projectRoot 'artifacts'

function Test-SentinelaPanel {
    try {
        $response = Invoke-RestMethod -Uri "$panelUrl/api/health" -TimeoutSec 2
        return $response.service -eq 'sentinela-dashboard'
    } catch { return $false }
}

try {
    if (-not (Test-SentinelaPanel)) {
        if (-not (Test-Path -LiteralPath $panelExe)) {
            throw 'Ambiente Python do Sentinela não encontrado.'
        }
        New-Item -ItemType Directory -Path $panelLogs -Force | Out-Null
        Start-Process -FilePath $panelExe -ArgumentList @('dashboard', '--port', '8765') `
            -WorkingDirectory $projectRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $panelLogs 'dashboard.log') `
            -RedirectStandardError (Join-Path $panelLogs 'dashboard-error.log') | Out-Null
        $panelReady = $false
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if (Test-SentinelaPanel) { $panelReady = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $panelReady) { throw 'O painel não iniciou. Consulte artifacts\dashboard-error.log.' }
    }
    Start-Process $panelUrl
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Sentinela AC') | Out-Null
    exit 1
}
