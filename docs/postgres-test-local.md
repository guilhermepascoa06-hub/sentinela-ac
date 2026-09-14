# PostgreSQL descartável para os testes locais

A suíte recria e apaga as tabelas a cada teste. `TEST_DATABASE_URL` nunca pode receber
a URL de produção. A fixture rejeita PostgreSQL remoto, banco cujo nome não termine em
`_test` e parâmetros de URL que poderiam redirecionar a conexão.

O script `scripts/run-local-checks.ps1` usa exclusivamente `127.0.0.1:55439`, banco e
usuário `sentinela_test`. Cria o cluster em `%TEMP%\sentinela-ac-pg-test-55439`, gera
senha aleatória exclusiva e mantém os dados e a senha fora do repositório. Não instala
serviço do Windows, não modifica `.env` e não acessa o Supabase. O servidor escuta
somente no loopback IPv4.

```powershell
# Informe a pasta que contém postgres.exe, initdb.exe, pg_ctl.exe e psql.exe.
.\scripts\run-local-checks.ps1 -PostgresBin 'C:\caminho\pgsql\bin' -PrepareOnly

# Qualidade + suíte SQLite + ciclo de migrations + suíte PostgreSQL.
.\scripts\run-local-checks.ps1 -PostgresBin 'C:\caminho\pgsql\bin'

# Encerrar somente este cluster; preserva arquivos e logs para diagnóstico.
.\scripts\run-local-checks.ps1 -PostgresBin 'C:\caminho\pgsql\bin' -Stop
```

`-SkipQuality` pula lint, formatação, tipos e bandit quando já foram validados; as duas
suítes e migrations continuam sendo executadas. `-PrepareOnly` prepara e comprova a
conexão sem iniciar a suíte, para não concorrer com alterações de outros agentes.

Durante as verificações, as variáveis de banco apontam explicitamente para as bases de
teste. Credenciais de notificações e provedores são esvaziadas apenas no processo do
script e restauradas no fim. A configuração pessoal em `.env` permanece intacta.
Execute uma suíte PostgreSQL de cada vez, porque os testes compartilham as tabelas.

Binários sem instalador podem ser obtidos pela página oficial do
[PostgreSQL para Windows](https://www.postgresql.org/download/windows/), que aponta
para a [distribuição de binários da EDB](https://www.enterprisedb.com/download-postgresql-binaries).
Não é necessário instalar nem atualizar o banco de produção para usar esse ambiente.

## Validação nesta entrega

Este script **não foi exercitado** em 14/09/2026: a máquina não tem binários do
PostgreSQL instalados e baixá-los não fazia parte da entrega. O que foi executado
localmente: `ruff check`, `ruff format --check`, `mypy`, `bandit` e a suíte completa
no SQLite. A suíte no PostgreSQL roda no CI a cada push (`ci.yml`), que é o ambiente
que o sistema usa de verdade — confira o resultado lá antes de considerar a mudança
validada.
