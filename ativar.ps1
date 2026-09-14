<#
    Ativacao do Sentinela AC.

    Roda uma vez. Pede as duas credenciais que so voce tem acesso, e faz todo o
    resto sozinho: grava o .env local, aplica as migrations no banco de producao,
    cadastra os segredos no GitHub, testa o canal do Telegram e dispara a primeira
    execucao na nuvem.

    As credenciais sao lidas de forma oculta, nunca aparecem na tela, nunca vao
    para o Git e nunca sao escritas em log.

        powershell -ExecutionPolicy Bypass -File .\ativar.ps1
#>

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Passo($n, $texto) { Write-Host "`n[$n] $texto" -ForegroundColor Cyan }
function Ok($texto)        { Write-Host "    OK  $texto" -ForegroundColor Green }
function Aviso($texto)     { Write-Host "    !   $texto" -ForegroundColor Yellow }
function Erro($texto)      { Write-Host "    X   $texto" -ForegroundColor Red }

function LerSegredo($rotulo) {
    $seguro = Read-Host -Prompt $rotulo -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro)
    try   { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host @'

  SENTINELA AC - ativacao
  =======================

  Voce vai precisar de duas coisas:

  1. A string de conexao do banco (Supabase > Project Settings > Database).
     Use a conexao direta ou o Session pooler na porta 5432.
     NUNCA a porta 6543: o transaction pooler descarta os locks que impedem
     duas execucoes de duplicarem alerta.

  2. O token do bot do Telegram (@BotFather > /newbot) e o seu chat id.
     Se ainda nao tiver bot, este script mostra como pegar o chat id.

'@ -ForegroundColor White

# ---------------------------------------------------------------- pre-requisitos

Passo 1 'Conferindo ferramentas'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Aviso 'Ambiente virtual ausente. Criando...'
    & py -3.12 -m venv .venv
    if (-not (Test-Path $python)) { & py -m venv .venv }
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet -e .
}
Ok 'Python pronto'

$temGh = $null -ne (Get-Command gh -ErrorAction SilentlyContinue)
if ($temGh) {
    gh auth status *> $null
    if ($LASTEXITCODE -ne 0) { $temGh = $false }
}
if ($temGh) { Ok 'GitHub CLI autenticado' }
else { Aviso 'GitHub CLI ausente ou deslogado: os segredos precisarao ser cadastrados a mao' }

# ---------------------------------------------------------------- banco

Passo 2 'Banco de dados de producao'
$databaseUrl = LerSegredo '    Cole a string de conexao (a digitacao fica oculta)'
if ([string]::IsNullOrWhiteSpace($databaseUrl)) { Erro 'Nada informado.'; exit 1 }

$databaseUrl = $databaseUrl.Trim()
# O projeto usa o driver psycopg 3; aceita a URL no formato que o painel entrega.
if ($databaseUrl.StartsWith('postgresql://'))  { $databaseUrl = $databaseUrl.Replace('postgresql://',  'postgresql+psycopg://') }
elseif ($databaseUrl.StartsWith('postgres://')) { $databaseUrl = $databaseUrl.Replace('postgres://',    'postgresql+psycopg://') }
if ($databaseUrl -match ':6543/') {
    Erro 'Porta 6543 e o transaction pooler do Supabase.'
    Write-Host '        Locks consultivos nao sobrevivem nele, entao duas execucoes' -ForegroundColor Red
    Write-Host '        simultaneas duplicariam alertas. Use a porta 5432.' -ForegroundColor Red
    exit 1
}
if ($databaseUrl -notmatch 'sslmode=') {
    $databaseUrl += $(if ($databaseUrl.Contains('?')) { '&sslmode=require' } else { '?sslmode=require' })
}
Ok 'String de conexao normalizada'

# ---------------------------------------------------------------- telegram

Passo 3 'Telegram'
$token  = LerSegredo '    Token do bot (@BotFather). Enter para pular'
$chatId = ''
if (-not [string]::IsNullOrWhiteSpace($token)) {
    $token = $token.Trim()
    $chatId = (LerSegredo '    Seu chat id. Enter para o script descobrir sozinho').Trim()
    if ([string]::IsNullOrWhiteSpace($chatId)) {
        Write-Host '    Abra o Telegram e mande QUALQUER mensagem para o seu bot agora.' -ForegroundColor Yellow
        Read-Host  '    Feito? Tecle Enter'
        try {
            $r = Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/getUpdates" -TimeoutSec 20
            $chatId = ($r.result | Select-Object -Last 1).message.chat.id
            if ($chatId) { Ok "chat id encontrado: $chatId" }
            else { Aviso 'Nenhuma mensagem encontrada. Cadastre TELEGRAM_CHAT_ID depois.' }
        } catch { Aviso "Nao consegui consultar a API do Telegram: $($_.Exception.Message)" }
    }
} else {
    Aviso 'Telegram pulado. O sistema roda e grava tudo, mas nao envia mensagem.'
}

# ---------------------------------------------------------------- .env

Passo 4 'Gravando .env local (fora do Git)'
$linhas = @(
    "DATABASE_URL=$databaseUrl"
    "TELEGRAM_BOT_TOKEN=$token"
    "TELEGRAM_CHAT_ID=$chatId"
    "GITHUB_REPOSITORY=guilhermepascoa06-hub/sentinela-ac"
)
Set-Content -LiteralPath '.env' -Value $linhas -Encoding utf8
Ok '.env gravado (ja esta no .gitignore)'

# ---------------------------------------------------------------- migrations

Passo 5 'Aplicando o schema no banco de producao'
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Erro 'Migrations falharam. Confira a string de conexao.'; exit 1 }
Ok '16 tabelas criadas'

# ---------------------------------------------------------------- diagnostico

Passo 6 'Diagnostico'
& $python -m sentinela.cli doctor

if (-not [string]::IsNullOrWhiteSpace($token) -and -not [string]::IsNullOrWhiteSpace($chatId)) {
    Passo 7 'Teste de notificacao'
    & $python -m sentinela.cli test-notification
    if ($LASTEXITCODE -eq 0) { Ok 'Mensagem de teste enviada. Confira o Telegram.' }
    else { Aviso 'O canal recusou. Confira token e chat id.' }
}

# ---------------------------------------------------------------- segredos

if ($temGh) {
    Passo 8 'Cadastrando segredos no GitHub'
    $repo = 'guilhermepascoa06-hub/sentinela-ac'
    $databaseUrl | gh secret set DATABASE_URL --repo $repo
    if ($LASTEXITCODE -eq 0) { Ok 'DATABASE_URL' }
    if ($token)  { $token  | gh secret set TELEGRAM_BOT_TOKEN --repo $repo; if ($LASTEXITCODE -eq 0) { Ok 'TELEGRAM_BOT_TOKEN' } }
    if ($chatId) { "$chatId" | gh secret set TELEGRAM_CHAT_ID  --repo $repo; if ($LASTEXITCODE -eq 0) { Ok 'TELEGRAM_CHAT_ID' } }

    Passo 9 'Disparando a primeira execucao na nuvem'
    gh workflow run daily-monitor.yml --repo $repo
    if ($LASTEXITCODE -eq 0) {
        Ok 'Execucao disparada'
        Write-Host "    Acompanhe: https://github.com/$repo/actions" -ForegroundColor White
    } else {
        Aviso 'Nao consegui disparar. Rode pelo painel: Actions > Monitoramento diario > Run workflow'
    }
} else {
    Passo 8 'Segredos do GitHub (cadastro manual)'
    Write-Host '    Settings > Secrets and variables > Actions > New repository secret:' -ForegroundColor White
    Write-Host '      DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID' -ForegroundColor White
}

Write-Host @'

  =======================================================
  Pronto. A partir de agora ele roda sozinho:

    07:17  coleta principal        (horario de Rio Branco)
    08:17  coleta redundante       (so age se a principal falhou)
    03:07  backup verificado
    dom 06:43  auditoria semanal

  Voce so recebe mensagem quando existe algo para fazer.
  =======================================================

'@ -ForegroundColor Green
