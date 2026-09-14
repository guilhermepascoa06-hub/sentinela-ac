# Sentinela AC

Monitor autônomo de concursos e processos seletivos públicos para **Rio Branco, Acre**.

Roda todo dia na nuvem, lê os documentos oficiais, compara com o filtro do usuário
(ensino médio, lotação em Rio Branco, carga horária compatível com estudo) e avisa no
Telegram **só quando existe algo para fazer**. Nunca manda "nada encontrado hoje".

O princípio que governa cada decisão do código: **evidência antes de alerta**. Todo dado
publicado vem de um documento oficial coletado, versionado e citável. O que não foi lido
fica `null` e aparece como "não informado" — o sistema nunca completa uma lacuna com um
palpite.

---

## Índice

- [O que ele faz](#o-que-ele-faz)
- [Arquitetura em uma tela](#arquitetura-em-uma-tela)
- [Instalação local](#instalação-local)
- [Configuração de produção](#configuração-de-produção)
  - [Supabase (banco)](#supabase-banco)
  - [Telegram](#telegram)
  - [GitHub](#github)
  - [Segredos necessários](#segredos-necessários)
- [Rodando manualmente](#rodando-manualmente)
- [Agendamento automático](#agendamento-automático)
- [Testes](#testes)
- [Tarefas do dia a dia](#tarefas-do-dia-a-dia)
  - [Adicionar uma fonte](#adicionar-uma-fonte)
  - [Consertar um parser quebrado](#consertar-um-parser-quebrado)
  - [Mudar o filtro do usuário](#mudar-o-filtro-do-usuário)
  - [Inspecionar oportunidades](#inspecionar-oportunidades)
  - [Investigar erros](#investigar-erros)
- [Custos](#custos)
- [Limitações conhecidas](#limitações-conhecidas)

---

## O que ele faz

**Monitora** 33 fontes oficiais (Prefeitura e Câmara de Rio Branco, Governo do Acre, Diário
Oficial do Estado e da União, TJAC, TRE-AC, MPAC, DPE-AC, TCE-AC, ALEAC, IFAC, UFAC, TRF1,
TRT14, Banco da Amazônia, Banco do Brasil, CAIXA, Correios, IBGE, INSS, Conab, Ibama,
Funai, CNU, conselhos profissionais) mais uma fonte agregadora usada **apenas para
descoberta**.

**Classifica** cada cargo em categorias que não se misturam:

| Categoria | Exemplo |
|---|---|
| `HIGH SCHOOL ONLY` | Agente Legislativo — só pede certificado de ensino médio |
| `HIGH SCHOOL + TECHNICAL QUALIFICATION` | Técnico em Enfermagem |
| `HIGH SCHOOL + PROFESSIONAL REGISTRATION` | cargo que exige registro em conselho |
| `HIGH SCHOOL + DRIVER/LICENSE REQUIREMENT` | Motorista com CNH D |
| `OTHER ADDITIONAL REQUIREMENT` | exige Prolibras, experiência, curso de formação |

**Classifica a carga horária**, que é o filtro mais importante:

| Classe | Faixa |
|---|---|
| `PERFECT` | até 20h/semana |
| `GOOD` | 21h a 30h/semana |
| `UNKNOWN` | ainda não confirmada — **continua investigando, nunca descarta** |
| `OUTSIDE TARGET` | acima de 30h — fica guardado, mas não gera alerta primário |

**Nunca confunde local de prova com local de lotação.** Um concurso federal cuja prova é
aplicada em Rio Branco, mas com lotação em Brasília, não é compatível — e o sistema testa
isso explicitamente (`tests/unit/test_parse.py`).

**Avisa** quando: aparece um concurso novo compatível, abrem inscrições, muda um prazo ou
a data de prova, sai uma retificação relevante, a carga horária finalmente é confirmada, um
prazo está chegando (7/3/1 dias para inscrição; 14/7/3/1 para prova), ou a própria
infraestrutura de monitoramento tem um problema sério.

---

## Arquitetura em uma tela

```
sources.yaml  ──►  Registry  ──►  Coletor  ──►  Fetcher (retry/backoff/ETag/circuit breaker)
                                                   │
                                                   ▼
                                    Extração  HTML ─► PDF nativo ─► parser alternativo
                                              ─► OCR ─► IA semântica ─► fila de revisão
                                                   │
                                                   ▼
                              Parsing determinístico (tabelas, regex, datas, moeda)
                                                   │
                                                   ▼
                     Normalização ─► Deduplicação ─► Elegibilidade ─► Pontuação
                                                   │
                                                   ▼
                Detecção de mudanças ─► Persistência versionada ─► Outbox de notificações
                                                   │
                                                   ▼
                          Saúde das fontes ─► Watchdog ─► Auditoria semanal
```

Detalhes de cada estágio em [ARCHITECTURE.md](ARCHITECTURE.md).

**IA é opcional e está desligada.** Toda a extração que o sistema faz hoje é determinística.
O LLM só entra como último recurso para preencher campos que o parser não achou, jamais para
decidir elegibilidade, e o sistema funciona por completo sem ele.

---

## Instalação local

Requer **Python 3.12+** e um PostgreSQL acessível.

```bash
git clone https://github.com/<usuário>/sentinela-ac.git
cd sentinela-ac

python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -e ".[dev]"

cp .env.example .env            # preencha DATABASE_URL
alembic upgrade head
sentinela doctor
```

`sentinela doctor` diz exatamente o que falta:

```
Sentinela AC Doctor

Banco de dados     OK           PostgreSQL 17.6
Migrations         OK           88f2c95541a9
Telegram           BLOCKED      Telegram credentials not configured
LLM                DESATIVADO   extração 100% determinística
Registry           OK           33 fontes declaradas
Fontes             33 saudáveis 0 degradadas · 0 circuito aberto
Watchdog           CRITICAL     Nenhuma execução bem-sucedida registrada
Agenda (UTC)       17 12 * * *  backup 17 13 * * *
```

---

## Configuração de produção

### Supabase (banco)

1. Crie um projeto no [Supabase](https://supabase.com) (plano gratuito basta).
2. Em **Project Settings → Database**, copie a **Connection string**.
3. Use a **conexão direta** ou o **Session pooler (porta 5432)**.
   **Nunca use o Transaction pooler (porta 6543)** — ele descarta locks consultivos, que
   são o que impede duas execuções simultâneas de duplicar alertas. O código recusa a
   porta 6543 explicitamente e explica o motivo.
4. Converta para o driver usado aqui:

```
postgresql+psycopg://postgres.<ref>:<SENHA>@aws-0-sa-east-1.pooler.supabase.com:5432/postgres?sslmode=require
```

O runner do GitHub Actions só tem IPv4. Se a conexão direta (`db.<ref>.supabase.co`) for
apenas IPv6 no seu projeto, use o **Session pooler**, que é IPv4.

### Telegram

1. No Telegram, fale com **@BotFather** → `/newbot` → escolha nome e usuário.
   Ele devolve o **token** (`TELEGRAM_BOT_TOKEN`).
2. Envie qualquer mensagem para o seu bot novo.
3. Descubra o seu chat id:

```bash
curl -s "https://api.telegram.org/bot<SEU_TOKEN>/getUpdates" | grep -o '"id":[0-9-]*' | head -1
```

O número é o `TELEGRAM_CHAT_ID`.

4. Teste ponta a ponta: `sentinela test-notification`

### GitHub

1. Crie o repositório e faça push.
2. **Settings → Secrets and variables → Actions → New repository secret**: cadastre os
   segredos da tabela abaixo.
3. **Settings → Actions → General**: deixe *Allow all actions* e **Read and write
   permissions** (o workflow abre issue quando uma fonte quebra).
4. Rode `Actions → Monitoramento diário → Run workflow` uma vez para validar.

> Repositório privado tem 2.000 minutos/mês de Actions no plano gratuito; este projeto usa
> cerca de 300. Repositório público é ilimitado — mas o `.env` **nunca** vai para o Git.

### Ativação em um comando

O repositório traz **`ativar.ps1`**, que faz todo o resto sozinho:

```powershell
powershell -ExecutionPolicy Bypass -File .tivar.ps1
```

Ele pede (com digitação oculta) a string de conexão e o token do Telegram, e então:
normaliza a URL, recusa a porta 6543, grava o `.env`, aplica as migrations no banco de
produção, descobre o seu `chat_id` sozinho, roda o diagnóstico, envia uma mensagem de
teste, cadastra os três segredos no GitHub e dispara a primeira execução na nuvem.

As credenciais nunca aparecem na tela, nunca vão para o Git e nunca entram em log.

### Segredos necessários

| Segredo | Obrigatório | Para quê |
|---|---|---|
| `DATABASE_URL` | **sim** | PostgreSQL: a única fonte de verdade |
| `TELEGRAM_BOT_TOKEN` | para alertas | canal principal |
| `TELEGRAM_CHAT_ID` | para alertas | destino das mensagens |
| `LLM_BASE_URL`, `LLM_API_KEY` | não | extração semântica opcional (desligada) |
| `SMTP_*`, `EMAIL_*` | não | canal secundário de e-mail |
| `WATCHDOG_PING_URL` | não | dead-man switch externo (healthchecks.io etc.) |
| `GITHUB_TOKEN` | automático | abre issue de fonte degradada |

Sem `DATABASE_URL` o workflow falha logo no começo, com mensagem explícita — nunca finge
que rodou.

---

## Rodando manualmente

```bash
sentinela run                      # ciclo completo
sentinela run --only tjac --only ifac
sentinela run --dry-run            # coleta sem gravar nem notificar
sentinela report --show            # relatório completo
sentinela opportunities            # o que combina com o filtro
sentinela opportunities --all      # tudo que foi encontrado
sentinela opportunities --unknown-workload
sentinela deadlines --days 45
sentinela sources                  # saúde de cada fonte
sentinela sources --degraded
sentinela source-check camara-rio-branco
sentinela doctor
sentinela deep-audit
sentinela retry                    # reprocessa a fila de revisão
sentinela test-notification
sentinela watchdog
sentinela backup
sentinela schedule                 # horários convertidos para UTC
```

---

## Agendamento automático

| Workflow | Quando | O quê |
|---|---|---|
| `daily-monitor.yml` | 07:17 e 08:17 (Rio Branco) | coleta; a segunda só assume se a primeira faltou ou falhou |
| `weekly-deep-audit.yml` | domingo 06:43 | revalida fontes, reprocessa pendências, descobre fontes novas |
| `database-backup.yml` | 03:07 | backup + verificação + teste de restauração |
| `ci.yml` | push e PR | lint, tipos, migrations, testes, checagem de segredos |

Os horários não caem na hora cheia de propósito: o agendador do GitHub atrasa e congestiona
no minuto 0.

**Como a execução redundante decide:** a de 08:17 consulta o banco. Se a de 07:17 concluiu
com `SUCCESS` hoje, ela sai em segundos. Se estiver `MISSING`, `FAILED`, `PARTIAL` ou
`STALE`, ela assume o ciclo inteiro. Duas execuções simultâneas são impossíveis: há
`concurrency` no GitHub Actions **e** um lock consultivo no PostgreSQL.

---

## Testes

```bash
pytest                                  # suíte completa
pytest tests/golden -v                  # contra um edital real publicado
TEST_DATABASE_URL=postgresql+psycopg://... pytest   # integração no PostgreSQL de verdade
```

Os testes golden validam a extração contra o **Edital nº 01/2026 da Câmara Municipal de Rio
Branco**, conferido campo a campo contra o PDF oficial. Se um parser regredir, o CI fica
vermelho.

**Política de regressão:** todo bug real encontrado ganha um teste antes de ser considerado
resolvido. Vários testes da suíte trazem o comentário `# Regression:` explicando o defeito
que eles travam.

---

## Tarefas do dia a dia

### Adicionar uma fonte

Todas as URLs vivem em **`sources.yaml`**, em lugar nenhum mais.

```yaml
  - id: "nova-fonte"
    name: "Nome legível"
    institution: "Órgão responsável"
    base_url: "https://orgao.ac.gov.br/concursos"
    official: true
    trust_level: 1          # 1 oficial · 2 banca · 3 portal · 4 notícia
    priority: 2
    adapter: "generic"
    discovery_method: "html"
    enabled: true
    trust_status: "TRUSTED" # CANDIDATE até você validar
    allowed_hosts: ["orgao.ac.gov.br"]   # obrigatório
    seed_urls: []
    link_pattern: "(?i)concurso|editais?|seletiv|retifica|\\.pdf(?:$|\\?)"
    max_documents: 8
    max_depth: 2
    expected_min_links: 0
    validation_url: "https://orgao.ac.gov.br/concursos"
    limitations: "o que esta fonte não cobre"
```

Depois: `sentinela source-check nova-fonte`.

`allowed_hosts` é obrigatório — sem ele a coleta seria irrestrita, e o registry recusa
carregar. Fontes descobertas automaticamente entram como `CANDIDATE` e **nunca** geram
alerta primário até você promovê-las.

### Consertar um parser quebrado

Quando uma fonte muda de layout, o sistema **não** conclui que não há concursos: ele marca
`SOURCE_DEGRADED` e abre uma issue. Para consertar:

```bash
sentinela sources --degraded          # quem quebrou
sentinela source-check <id>           # reproduzir
```

Ajuste `link_pattern`, `seed_urls` ou `base_url` em `sources.yaml`, acrescente um teste e
rode `sentinela source-check <id>` de novo. Passo a passo em [RUNBOOK.md](RUNBOOK.md).

### Mudar o filtro do usuário

Tudo em **`config.yaml`**:

```yaml
workload:
  ideal_max_weekly_hours: 20      # vira PERFECT
  acceptable_max_weekly_hours: 30 # vira GOOD
monitoring:
  registration_reminders: [7, 3, 1]
  exam_reminders: [14, 7, 3, 1]
notifications:
  telegram: true
  email: false
```

Nenhum desses valores aparece espalhado pelo código.

### Inspecionar oportunidades

```bash
sentinela opportunities
sentinela report --show
```

Cada campo crítico guarda `value`, `source_url`, `page_number`, `extraction_method`,
`confidence` e `raw_evidence`. Para auditar a remuneração de um cargo:

```sql
SELECT name, evidence->'salary' FROM positions WHERE eligible;
```

### Investigar erros

```bash
sentinela doctor
```

```sql
SELECT created_at, stage, kind, source_id, message
FROM errors ORDER BY created_at DESC LIMIT 20;

SELECT created_at, kind, severity, message
FROM system_events ORDER BY created_at DESC LIMIT 20;

SELECT url, review_reason FROM documents WHERE processing_state = 'REVIEW';
```

Os logs são estruturados e carregam `run_id`, `source_id` e `document_id`. Credenciais são
redigidas antes de qualquer linha ser escrita.

---

## Custos

| Item | Custo |
|---|---|
| Supabase Free (500 MB) | **R$ 0** |
| GitHub Actions | **R$ 0** (~300 de 2.000 min/mês) |
| Telegram Bot API | **R$ 0** |
| LLM | **R$ 0** — desligado por padrão |
| **Total** | **R$ 0/mês** |

Projeto Supabase gratuito **pausa após 7 dias sem atividade**. As execuções diárias contam
como atividade, então na prática ele não pausa. Se ficar semanas parado, é só despausar no
painel.

---

## Limitações conhecidas

- **Portais que exigem JavaScript** são resolvidos por um navegador real (Playwright,
  Chromium headless), acionado só quando o HTTP simples é barrado de um jeito que um
  navegador pode legitimamente abrir, ou quando a página responde mas não produz nada —
  a assinatura de uma lista montada por JavaScript. Orçamento de 12 páginas por execução.
- **O navegador nunca contorna proteção.** CAPTCHA, desafio anti-robô ou tela de login
  fazem a fonte permanecer degradada. A informação não é pública para um cliente
  automatizado, e fingir o contrário seria desonesto além de frágil.
- **Bloqueio por IP de nuvem.** IBGE, Banco do Brasil e TRE-AC respondem de um IP
  residencial mas devolvem 403 ao runner do GitHub, e nem o navegador muda isso: o filtro
  é de rede, não de renderização. FUNAI devolve 401 (acesso restrito), IDIB 525 e SEAD 502
  — erros da própria origem. Todos ficam degradados, com issue aberta automaticamente.
- **Diários oficiais** (DOU, Diário do Acre) são enormes e têm busca dinâmica: a cobertura
  genérica não é leitura integral.
- **OCR** só roda se o Tesseract estiver instalado; sem ele, PDF escaneado vai para a fila
  de revisão em vez de virar alerta.
- **Cargos sem tabela no edital** entram como candidatos incompletos e são reprocessados na
  auditoria semanal, nunca descartados.

Documentos relacionados: [ARCHITECTURE.md](ARCHITECTURE.md) · [SOURCES.md](SOURCES.md) ·
[RUNBOOK.md](RUNBOOK.md)
