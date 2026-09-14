# Para quem for continuar este projeto

Leia este arquivo inteiro antes de editar qualquer coisa. Ele existe porque quase todo
defeito sério encontrado aqui foi de um tipo só — **funcionalidade escrita e nunca
ligada** — e a seção sobre isso é o que mais economiza tempo.

O sistema está **no ar e rodando sozinho**. Não é um protótipo.

---

## 1. O que é, em uma frase

Um serviço pequeno de produção com uma responsabilidade: garantir que **Guilherme não
perca um concurso adequado em Rio Branco/AC** — ensino médio, lotação na cidade, carga
horária compatível com estudo.

### O princípio que governa toda decisão

**Evidência antes de alerta.**

Todo dado publicado vem de documento oficial coletado, versionado e citável. O que não foi
lido fica `null` e sai como "não informado". O sistema **nunca** preenche uma lacuna com
palpite. Se você se pegar escrevendo um valor padrão plausível para um campo que não foi
lido, pare: isso é o antipadrão central deste projeto.

Ordem de prioridade, quando houver conflito entre objetivos:

```
CONFIABILIDADE → PRECISÃO → INTEGRIDADE → OBSERVABILIDADE
→ RECUPERAÇÃO AUTOMÁTICA → COBERTURA → MANUTENIBILIDADE → DESEMPENHO
```

---

## 2. Estado atual (14/09/2026)

| | |
|---|---|
| Repositório | `guilhermepascoa06-hub/sentinela-ac` — **público**, branch `main` |
| Banco | Supabase, **session pooler** `aws-0-sa-east-1.pooler.supabase.com:5432` |
| Testes | 337, verdes no SQLite; o PostgreSQL é exercitado pelo CI a cada push |
| Qualidade | ruff, ruff format, mypy e bandit limpos |
| Fontes | 33 confiáveis + 1 candidata · 28 saudáveis, 1 degradada, 5 circuito aberto |
| Dados | 124 documentos · 18 oportunidades · 34 cargos · 181 versões |
| Custo | **R$ 0/mês**, sem teto (repo público = Actions ilimitado) |
| Consulta | painel local (`abrir-painel.bat`) + consultas determinísticas no bot |

**Resultado real:** 2 cargos compatíveis — Agente Legislativo e Tradutor Intérprete, Câmara
Municipal de Rio Branco, 30h, R$ 4.656,75, inscrições até 13/10/2026. Conferidos campo a
campo contra o PDF oficial de 56 páginas.

### Automação

| Workflow | Quando | O quê |
|---|---|---|
| `daily-monitor.yml` | 07:17 e 08:17 (Rio Branco) | coleta; a segunda só age se a primeira faltou |
| `telegram-bot.yml` | de hora em hora | responde mensagens no Telegram |
| `database-backup.yml` | 03:07 | dump + verificação + teste de restauração |
| `weekly-deep-audit.yml` | domingo 06:43 | revalida fontes, reprocessa pendências, descobre fontes |
| `ci.yml` | push e PR | lint, format, mypy, bandit, migrations up/down/check, testes em PG |

Os quatro primeiros **já foram executados de verdade na nuvem** e passaram. Não presuma que
um workflow funciona porque o YAML é válido — veja a seção 6.

---

## 3. Mapa dos módulos

```
domain.py      modelos Pydantic, normalize(), normalize_indexed(), PARSER_VERSION
models.py      16 tabelas SQLAlchemy (o esquema é a fonte da verdade)
db.py          engine, sessão, lock consultivo, tamanho do banco
config.py      config.yaml + segredos (.env / GitHub Secrets)
registry.py    sources.yaml ↔ banco; fontes CANDIDATE
fetch.py       HTTP: retry, backoff, jitter, ETag, rate limit, allow-list
browser.py     Playwright — último degrau, só quando HTTP não enxerga
collector.py   por fonte: descobre, baixa, extrai. NUNCA levanta exceção
extract.py     HTML → PDF nativo → parser alternativo → OCR → revisão
tables.py      tabelas do PDF (find_tables) e classificação de célula por conteúdo
parse.py       parsers determinísticos: data, moeda, carga horária, escolaridade, lotação
structure.py   monta OpportunityDraft com evidência por campo
verify.py      IA localiza, parser determinístico confirma. Nada passa sem os dois
llm.py         provedor OpenAI-compatível (Gemini), cache, retry, fallback de modelo
dedup.py       identidade da oportunidade; similaridade só como último recurso
eligibility.py filtros duros e pontuação. Nenhum modelo decide isso
diff.py        mudanças campo a campo; só o que um humano agiria vira alerta
health.py      saúde da fonte, circuit breaker, detecção de mudança de layout
pipeline.py    orquestra tudo. É o arquivo mais denso — leia antes de mexer
alerts.py      texto das mensagens + chave de idempotência
notifications.py  canais (Telegram, e-mail, markdown, console)
bot.py         Telegram de duas vias: roteia comandos e perguntas
bot_queries.py busca, ficha e histórico determinísticos — respondem sem IA
dashboard.py   painel local somente-leitura em 127.0.0.1 + web/ (HTML, CSS, JS)
watchdog.py    monitora o próprio monitoramento
audit.py       auditoria semanal + issues automáticas no GitHub
report.py      reports/latest.md
backup.py      dump, verificação, restauração
cli.py         15 comandos
```

---

## 4. Invariantes — quebrar qualquer um destes é regressão

Cada linha aqui custou um bug real em produção. Há teste travando todas.

### Domínio

1. **Local de prova ≠ local de lotação.** `location_context` classifica pela *oração*, não
   por janela de caracteres. Complemento: `institution_implies_city` — órgão **municipal**
   de Rio Branco lota em Rio Branco; estadual e federal exigem evidência no texto.
2. **Chamamento de OSC, licitação, seleção de aluno e remoção interna usam o mesmo
   vocabulário e o mesmo portal que concurso.** `is_job_selection` barra. Sem isso, um
   "Edital 001/2026 — Convocação das OSCs" colide com o concurso 01/2026 do mesmo órgão.
3. **Zero documentos nunca significa zero concursos** → `SOURCE_DEGRADED`.
4. **Página de notícia não pode sobrescrever edital.** `best_authority` = confiança × 10,
   com bônus para quem trouxe tabela de cargos. Sem isso o status anda para trás.
5. **Score da oportunidade é o máximo sobre os cargos armazenados**, não sobre os do
   documento atual.
6. **Conflito entre fontes é registrado, nunca resolvido em silêncio** — mas só em campos
   onde o certame tem um único valor verdadeiro (`CONFLICT_FIELDS`). URL e sigla de banca
   ficam de fora: eram ruído que enterrava o conflito real.

### IA

7. **O modelo localiza, o código decide.** Ele devolve um trecho; `verify.py` confere que o
   trecho existe literalmente e que o parser determinístico rederiva o mesmo valor. Só
   então entra, como `llm_located+parser_verified`, confiança 0.85.
8. **O sistema roda inteiro sem IA.** Se ela sumir, nada essencial se perde.
9. **Texto recebido é dado, nunca instrução** — vale para edital e para mensagem do
   Telegram. Vai no turno do usuário, rotulado; as regras ficam no turno de sistema.
10. **Falha transitória do provedor nunca entra no cache** — senão o documento fica
    "já perguntado" para sempre por causa de um minuto ruim.

### Notificação

11. **Idempotência é estrutural**: chave derivada de fatos estáveis, coluna única no banco.
    Não é disciplina de código.
12. **Credencial ausente ≠ canal recusou.** `UNCONFIGURED` é reprocessável; `BLOCKED` é
    terminal. Confundir os dois deixou dois alertas presos para sempre.
13. **Entrega incerta não é reenviada às cegas.** Telegram não tem chave de idempotência;
    duplicar um alerta é pior que atrasá-lo.
14. **Só o chat configurado é respondido.** Um token de bot é público na prática.

### Operação

15. **Uma execução por vez**, garantida por `pg_try_advisory_lock` — não pelo agendador.
16. **Porta 6543 é proibida** (transaction pooler do Supabase): descarta locks consultivos.
    O código recusa explicitamente.
17. **Execução com tudo ignorado é `SKIPPED`, não `FAILED`** — senão o watchdog grita sobre
    um sistema saudável.
18. **Silêncio nunca é boa notícia.** O watchdog existe para que ninguém acredite que está
    sendo vigiado quando o monitoramento parou.
19. **`WARNING` do watchdog é chaveado pelo conteúdo do problema**, não pela data — senão
    vira mensagem diária eterna sobre as mesmas fontes quebradas. `CRITICAL` repete diário.

### Segurança

20. Nunca contornar CAPTCHA, login ou bloqueio. Página com muro fica degradada.
21. Nunca desabilitar TLS. Certificado quebrado é fonte degradada.
22. Documento baixado é dado: nunca executado, nunca vira caminho de arquivo, nunca sai da
    allow-list da própria fonte.
23. **Repositório é público.** Nunca commitar `.env`, chat_id real, e-mail pessoal ou a ref
    do projeto Supabase. O histórico foi reescrito uma vez para remover chat_id e e-mail.

### Apresentação

24. **Nenhum código sai para quem lê.** `high_school`, `GOOD`, `REGISTRATION_OPEN` e
    `HIGH SCHOOL ONLY` são chaves internas. Painel, card do bot, alerta e diff traduzem
    pelos mapas de `alerts.py` (`STATUS_LABEL`, `EMPLOYMENT_LABEL`, `EDUCATION_LABEL`,
    `WORKLOAD_LABEL`, `QUALIFICATION_LABEL`). Código desconhecido aparece como está —
    traduzir por adivinhação seria inventar. `raw_old`/`raw_new` do diff continuam com o
    código, porque é o que o escritor compara.
25. **Releitura sem mudança não é histórico.** Versões com `changes` vazio provam que o
    monitor está vivo, mas se entram na lista enterram a alteração que importa: são
    **contadas e declaradas**, nunca listadas — no painel e no bot.
26. **Texto cortado é declarado.** O card do Telegram reserva evidência e link no fim (uma
    URL cortada é um link errado, não um link menor) e marca `[mensagem truncada]` quando
    qualquer campo foi encurtado. O painel corta campos gigantes na lista e diz que o texto
    completo está na ficha.

---

## 5. Armadilhas técnicas conhecidas

- `normalize()` remove `$` — moeda tem de ser detectada no texto **cru**.
- `normalize_indexed()` existe porque estimar posição por proporção de tamanho cai no
  parágrafo errado em documento longo. Use-a sempre que localizar marcador.
- `find_tables()` do PyMuPDF lê o quadro de vagas real muito bem. Classifique a célula
  **pelo conteúdo**, não pela coluna: cabeçalhos quebram em duas linhas e deslocam.
- Mandar os primeiros N caracteres de um edital para o modelo é inútil: no edital da Câmara
  a tabela de cargos começa por volta do caractere 146.000. Use `verify.focus_excerpt`.
- `gemini-2.5-flash` foi descontinuado para contas novas. Está em `gemini-3.5-flash`, com
  queda automática para os *lite* quando a cota estoura (429 não se resolve esperando).
- O GitHub cobra **minuto arredondado para cima** por execução.
- `bot_queries.py` não pode importar `bot.py` (o caminho é o contrário). `MAX_REPLY`
  mora em `bot_queries` e `bot` o reexporta: há uma definição só.
- O painel é servido de `src/sentinela/web/`. A fonte vem junto no repositório porque a
  CSP dele é `'self'` para tudo: nada carrega de CDN, e um arquivo declarado e não
  enviado vira 404 em toda visita (há teste travando isso).
- A CLI importa `pipeline` de forma **tardia**. Não mova para o topo: isso arrastaria
  bs4/pymupdf/playwright para o job do bot, que só lê o banco. Há teste bloqueando.

---

## 6. Como procurar bugs aqui (o método que achou 17)

Três rodadas de auditoria encontraram 17 defeitos do mesmo tipo: **eu escrevia a função, a
coluna e a configuração — e não ligava a ponta final.** Cada peça parecia pronta isolada. Só
apareceu comparando **o que o banco tinha contra o que o código prometia**.

Rode isto periodicamente:

```bash
# 1. colunas sempre vazias em produção
SELECT count(col) FROM tabela WHERE col IS NOT NULL;   -- para cada coluna

# 2. funções definidas sem chamador
grep -rn "^def nome" src/ ; grep -rn "\bnome\b" src/

# 3. chaves de config.yaml que nenhum módulo lê
# 4. valores gravados e nunca COMPARADOS (parser_version, metadata_hash já foram)
# 5. workflows que existem e nunca foram executados na nuvem
```

Exemplos do que isso pegou: conflito entre fontes nunca registrado (os "0 conflitos" eram
falsos); `source_runs` declarada, migrada e nunca escrita; `RECOVERING` inalcançável; quatro
chaves de config sem efeito; `parser_version` nunca comparada, então melhoria de parser não
alcançava o já armazenado.

---

## 7. Processo obrigatório antes de enviar

```bash
ruff check src tests && ruff format --check src tests
mypy
bandit -q -c pyproject.toml -r src
pytest -q                                              # SQLite
TEST_DATABASE_URL=postgresql+psycopg://... pytest -q    # PostgreSQL — o CI usa este
```

**Rode as duas suítes.** O SQLite deixa passar o que o PostgreSQL pega; isso já quebrou o CI.

**Todo bug real ganha teste de regressão antes de ser considerado resolvido.** Vários testes
trazem um comentário explicando o defeito que travam — mantenha esse hábito, é o que impede
o mesmo erro de voltar.

---

## 8. Comandos

```
sentinela run [--only ID] [--force] [--dry-run] [--skip-if-fresh]
sentinela report | opportunities | deadlines | sources | source-check ID
sentinela doctor | watchdog | deep-audit | issues | retry | backup | schedule
sentinela bot [--watch]        # --watch responde em segundos, sem gastar Actions
sentinela dashboard [--port 8765]   # painel local; abrir-painel.bat faz o mesmo
sentinela test-notification
```

`sentinela doctor` é o primeiro comando em qualquer investigação.

---

## 9. O que está realmente aberto

1. **5 fontes com circuito aberto, e nenhuma é bug de código.** `tre-ac`, `ibge` e
   `banco-brasil` devolvem 403 ao runner do GitHub mas respondem de IP residencial — filtro
   de rede, não de renderização, e o navegador não muda isso. `funai` é 401, `idib` 525,
   `sead-acre` 502. Cada uma tem issue aberta. Resolver exigiria proxy ou fonte alternativa;
   **não tente contornar bloqueio.**
2. **122 documentos na fila de revisão.** A maioria é esperada: páginas de navegação sem
   edital, datas ou quadro de cargos. Vale investigar só as que têm motivo diferente disso.
3. **`canonical_url` é redundante** com `url` (a canonicalização acontece antes). Coluna
   inofensiva; remover exigiria migration.
4. **Cobertura de Rio Branco depende de poucos portais.** O caminho de maior valor é
   acrescentar fontes municipais e estaduais em `sources.yaml` — é uma entrada de YAML.
5. **Os `reasons` já gravados ainda trazem `Carga horária: GOOD`.** O texto passou a ser
   traduzido em `eligibility.py`, mas as linhas antigas só se corrigem quando o cargo é
   reavaliado pela próxima coleta. Não vale migration.
6. **`raw_snapshots` e `opportunity_versions` crescem para sempre.** 17 MB de 500 hoje, com
   aviso configurado em 350. Anos de folga, mas não há poda.

---

## 10. Onde está o resto

`README.md` instalação e operação · `ARCHITECTURE.md` decisões e por quês ·
`SOURCES.md` inventário das fontes · `RUNBOOK.md` recuperação de falhas.

O usuário é o Guilherme. Ele quer as coisas **feitas**, não descritas: entregue funcionando
e conte o resultado, em vez de devolver uma lista de passos para ele executar.
