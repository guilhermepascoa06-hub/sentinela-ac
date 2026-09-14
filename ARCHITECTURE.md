# Arquitetura

O Sentinela AC é um pequeno serviço de produção com uma responsabilidade: garantir que o
usuário não perca um concurso adequado em Rio Branco. Tudo aqui existe para servir a essa
frase, na ordem **confiabilidade → precisão → integridade → observabilidade → recuperação
automática → cobertura → manutenibilidade → desempenho**.

## Princípio central

**Software determinístico para tudo que código normal resolve. IA só onde é preciso
interpretar semântica — e mesmo aí, jamais para decidir elegibilidade.**

Não existe enxame de agentes. Existe um pipeline com estágios nomeados, cada um
idempotente, observável, testável, retentável e isolado em falha.

## Estágios

| # | Estágio | Módulo | O que garante |
|---|---|---|---|
| 1 | Registry | `registry.py` | Toda URL num só arquivo (`sources.yaml`), espelhado no banco sem apagar histórico |
| 2 | Descoberta | `collector.py`, `audit.py` | Acha documentos novos; descobre portais novos como `CANDIDATE` |
| 3 | Coleta | `fetch.py` | Timeout, retry com backoff e jitter, ETag/Last-Modified, rate limit por host, pool, limite de tamanho |
| 4 | Detecção de documento | `extract.py` | Tipo real vem do conteúdo, não do header nem da extensão |
| 5 | Validação de origem | `fetch.py` | `allowed_hosts` por fonte; `tjac.jus.br.evil.com` não passa |
| 6 | Extração | `extract.py` | HTML → PDF nativo → parser alternativo → OCR → IA → fila de revisão |
| 7 | Parsing estruturado | `tables.py`, `parse.py` | Tabelas de cargos e de cronograma; datas, moeda, carga horária, escolaridade |
| 8 | Fallback semântico | `llm.py` | Opcional, desligado, só preenche lacunas, nunca inventa |
| 9 | Normalização | `structure.py` | Monta o `OpportunityDraft` com evidência por campo |
| 10 | Deduplicação | `dedup.py` | Chave explícita primeiro; similaridade só como último recurso |
| 11 | Elegibilidade | `eligibility.py` | Filtros duros e determinísticos |
| 12 | Pontuação | `eligibility.py` | 0–100 + confiança separada |
| 13 | Mudanças | `diff.py` | Diff campo a campo, legível, só alerta o que importa |
| 14 | Persistência | `pipeline.py`, `models.py` | Versão nova a cada mudança; nada é sobrescrito em silêncio |
| 15 | Notificação | `alerts.py`, `notifications.py` | Outbox com chave única; canal plugável |
| 16 | Saúde | `health.py` | Estado por fonte, circuit breaker, detecção de mudança de layout |
| 17 | Auditoria | `audit.py`, `watchdog.py` | Revalidação semanal, watchdog, issues automáticas |

## Decisões e o porquê

### PostgreSQL é a única fonte de verdade

O sistema de arquivos do GitHub Actions é efêmero. Todo estado — o que já foi visto, o que
já foi alertado, a saúde de cada fonte — vive no banco. Isso é o que permite duas execuções
por dia sem duplicar alerta e o que torna o watchdog possível.

### Idempotência é estrutural, não disciplinar

Cada notificação deriva uma chave de fatos estáveis (`opportunity_id`, categoria, valor
anunciado). A chave é **coluna única** no banco. Reexecutar o dia inteiro não duplica nada
— não porque o código lembra, mas porque o banco recusa.

### Uma execução por vez, garantida pelo banco

`pg_try_advisory_lock` com escopo de sessão. Se o processo morrer, o servidor solta o lock:
uma execução travada nunca emperra a agenda. O `concurrency` do GitHub Actions é a segunda
camada, não a única.

### Hash de conteúdo evita trabalho e custo

SHA-256 do documento baixado. Igual ao anterior → pula extração, parsing e IA. Só
reprocessa quando é novo, mudou, os metadados mudaram, a extração ficou incompleta, ou a
versão do parser/prompt mudou.

### Zero documentos nunca significa zero concursos

`health.detect_drift` compara a contagem atual com o histórico da fonte e com a impressão
digital estrutural da página (quantidade de links, tabelas, listas, títulos — nunca o
conteúdo, que muda todo dia). Uma fonte que costuma trazer 12–15 documentos e hoje trouxe 0
vira `SOURCE_DEGRADED`, não "nada publicado".

### Circuit breaker

`HEALTHY → DEGRADED → OPEN → (resfriamento) → RECOVERING → HEALTHY`. Sites de governo caem;
martelar um site caído é falta de educação e não ajuda. Depois do resfriamento a fonte é
testada de novo automaticamente.

### Local de prova ≠ local de lotação

Esse é o erro mais fácil de cometer e o mais caro. `parse.location_context` classifica cada
menção à cidade pela **oração** em que ela aparece, não por uma janela fixa de caracteres —
justamente porque "As provas serão aplicadas em Rio Branco. A lotação será em Cruzeiro do
Sul" colocava um marcador de lotação perto demais da cidade.

Complemento determinístico: `institution_implies_city` reconhece que um órgão **municipal**
de Rio Branco só lota em Rio Branco. Órgãos estaduais e federais ficam de fora dessa regra
de propósito — o TJAC tem comarcas no estado inteiro.

### Antes de virar oportunidade, precisa ser um emprego

Editais de chamamento público para OSCs, licitações, seleções de alunos, residências e
remoções internas usam exatamente o mesmo vocabulário e vivem no mesmo portal.
`parse.is_job_selection` barra isso. Sem essa porta, um "Edital nº 001/2026 — Convocação das
Organizações da Sociedade Civil" colidia com o concurso 01/2026 do mesmo órgão.

### Autoridade documental no merge

Nem todo documento da mesma fonte vale o mesmo. Um edital de abertura com quadro de cargos
tem mais autoridade que a página de notícias que fala dele. `best_authority` (nível de
confiança × 10, com bônus para quem trouxe tabela de cargos) impede que a página fraca
sobrescreva o que o edital estabeleceu — e impede que o status ande para trás.

### Falso positivo e falso negativo têm defesas diferentes

**Falso positivo:** nunca alertar só por resultado de busca; validar data de publicação, ano
do certame, janela de inscrição, status atual e origem oficial. Uma página de 2022
descoberta em 2026 não vira alerta de 2026.

**Falso negativo:** nunca descartar por falta de dado. Carga horária ausente, salário
ausente, PDF ilegível, fonte fora do ar — tudo vai para `REVIEW` e volta na auditoria
semanal.

### Confiança é separada de compatibilidade

`Compatibilidade: 90/100` diz o quanto a vaga serve. `Confiança: MEDIUM` diz o quanto temos
certeza da informação. Misturar os dois esconde o que o usuário mais precisa saber.

### IA opcional, cacheada, limitada

- desligada por padrão, sem chave necessária;
- cache por `(document_hash, prompt_version, model, model_version)`;
- prompts versionados em `prompts/`;
- só preenche campos que o parser deixou vazios;
- **proibida** de tocar em escolaridade, carga horária e lotação;
- confiança limitada a 0,9 — nunca supera uma leitura de tabela;
- afirmação sem trecho citado é descartada;
- provedor fora do ar degrada, não derruba a execução.

### Entrega incerta não é reenviada às cegas

Telegram e SMTP não têm chave de idempotência. Se a resposta ficar ambígua (timeout depois
do envio, erro 5xx), o registro fica `UNCERTAIN` para revisão em vez de ser reenviado —
duplicar um alerta é pior do que atrasá-lo.

### Tudo que é público é dado, nunca comando

Documentos baixados nunca são executados, nunca viram caminho de arquivo, nunca saem da
allow-list da própria fonte. TLS nunca é desabilitado: certificado quebrado é fonte
degradada, não motivo para baixar a guarda.

## Esquema

16 tabelas em três blocos:

**Operação** — `sources`, `source_health`, `monitor_runs`, `source_runs`
**Evidência** — `documents`, `document_versions`, `raw_snapshots`, `extraction_results`
**Domínio** — `opportunities`, `opportunity_versions`, `positions`, `deadlines`,
`notifications`, `classification_history`, `errors`, `system_events`

Timestamps em UTC no banco; apresentação sempre em `America/Rio_Branco`.

O histórico completo de uma oportunidade é reconstruível: cada `opportunity_versions` traz o
snapshot inteiro **e** o diff que o produziu, com `source_id`, `source_url`, `document_id` e
`document_version_id`.

## Observabilidade

Logs estruturados com `run_id`, `source_id`, `document_id` e `stage`. Redação de segredos
acontece no formatter, não no ponto de chamada — nenhuma linha pode vazar credencial mesmo
que alguém passe uma por engano.

Cada execução grava um heartbeat em `monitor_runs`. O watchdog lê só essa tabela. Silêncio
nunca é interpretado como boa notícia.

## Extensão futura

O domínio não conhece a camada de apresentação, então outra interface lê o mesmo banco sem
tocar no pipeline. Adicionar canal de notificação é implementar o protocolo `Notifier`.
Adicionar fonte é uma entrada em YAML.
