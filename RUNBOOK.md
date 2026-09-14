# Runbook

O que fazer quando algo dá errado. Cada seção começa pelo sintoma que você vai ver.

Primeiro comando, sempre:

```bash
sentinela doctor
```

---

## 1. "Faz dias que não chega alerta nenhum"

Silêncio pode ser normal — o sistema **não** manda mensagem quando não há novidade. Mas
silêncio também é o sintoma de um monitor morto. Diferencie assim:

```bash
sentinela watchdog
```

- **OK** → não havia nada para avisar. Confirme em `reports/latest.md`.
- **CRITICAL** → o monitoramento parou. Siga abaixo.

```sql
SELECT started_at, status, sources_successful, sources_attempted, errors
FROM monitor_runs ORDER BY started_at DESC LIMIT 10;
```

| O que você vê | Causa provável | Ação |
|---|---|---|
| Nenhuma linha recente | Workflow desabilitado ou repositório inativo | GitHub → Actions → habilitar; rodar manualmente |
| `status = FAILED` | Banco inacessível ou segredo expirado | Seção 2 |
| `status = RUNNING` parado há horas | Execução morta sem liberar o registro | Seção 6 |
| Execuções OK, sem alerta | Nada relevante publicado | Nada a fazer |

> O GitHub desabilita `schedule` em repositórios sem commits por **60 dias**. Um commit
> qualquer reativa. O watchdog avisa antes de você perceber.

---

## 2. Banco de dados inacessível

**Sintoma:** `sentinela doctor` mostra `Banco de dados: FALHOU`, ou o workflow para no
passo de migrations.

```bash
sentinela doctor
```

1. **Segredo ausente ou errado** → GitHub → Settings → Secrets → `DATABASE_URL`.
2. **Projeto Supabase pausado** (7 dias sem atividade) → despause no painel e rode
   `sentinela run` manualmente.
3. **Porta 6543** → o sistema recusa de propósito: o transaction pooler descarta locks
   consultivos, que são o que impede duas execuções de duplicar alertas. Use a conexão
   direta ou o **session pooler na 5432**.
4. **Senha rotacionada** → gere nova em Project Settings → Database e atualize o segredo.
5. **Só IPv6** → o runner do GitHub Actions é IPv4. Use o session pooler
   (`aws-0-sa-east-1.pooler.supabase.com`).

Nenhum dado se perde nesse cenário: sem banco, a execução falha limpo e não grava nada
pela metade.

---

## 3. Uma fonte quebrou

**Sintoma:** issue automática `[Fonte degradada] <nome>`, ou `sentinela sources --degraded`
listando alguém.

```bash
sentinela sources --degraded
sentinela source-check <id>
```

```sql
SELECT created_at, kind, message FROM system_events
WHERE kind IN ('SOURCE_DRIFT','SOURCE_HEALTH','SOURCE_URL_CHANGED','SOURCE_MIGRATED')
ORDER BY created_at DESC LIMIT 20;
```

| Estado | Significado | Ação |
|---|---|---|
| `DEGRADED` | Respondeu, mas não trouxe os documentos esperados | Layout provavelmente mudou → seção 4 |
| `OPEN` | Falhou seguidas vezes; circuito aberto | Espere o resfriamento (6h); o sistema testa sozinho |
| `RECOVERING` | Testando de novo após o resfriamento | Nada a fazer |
| `DISABLED` | Desligada manualmente | Reative em `sources.yaml` |

**Importante:** uma fonte degradada **não** significa "nenhum concurso". Significa "esta
fonte parou de responder direito". As outras 32 continuam rodando normalmente.

---

## 4. Parser quebrado / site mudou de layout

**Sintoma:** `SOURCE_DRIFT` em `system_events`, ou a fonte traz 0 documentos tendo
histórico de trazer vários.

```bash
sentinela source-check <id>     # reproduzir
```

Abra a página no navegador e compare:

1. **A URL mudou?** Procure `SOURCE_URL_CHANGED` em `system_events` e atualize `base_url`
   e `validation_url` em `sources.yaml`.
2. **Os links mudaram de formato?** Ajuste `link_pattern`.
3. **O conteúdo foi para um subdomínio?** Acrescente o host em `allowed_hosts` — sem isso
   os links são descartados de propósito.
4. **Há uma página intermediária nova?** Acrescente-a em `seed_urls`.
5. **A página passou a exigir JavaScript?** Registre em `limitations`, procure alternativa
   (feed, sitemap, endpoint oficial). **Não** contorne bloqueio.

Depois:

```bash
sentinela source-check <id>
pytest
```

Acrescente um teste de regressão antes de considerar resolvido — é a política do projeto.
Feche a issue automática; ela não é reaberta enquanto estiver aberta.

---

## 5. Alerta errado ou duplicado

**Duplicado:** não deveria acontecer — a chave de idempotência é coluna única.

```sql
SELECT idempotency_key, channel, category, status, created_at
FROM notifications ORDER BY created_at DESC LIMIT 20;
```

Se houver duas linhas com a mesma chave, o índice único sumiu: rode `alembic upgrade head`.

**Alerta com informação errada:** todo campo é auditável.

```sql
SELECT name, evidence->'salary', evidence->'weekly_workload'
FROM positions WHERE opportunity_id = '<id>';

SELECT version, changes, source_url, detected_at
FROM opportunity_versions WHERE opportunity_id = '<id>' ORDER BY version;
```

`raw_evidence` traz o trecho literal do documento. Compare com o PDF oficial. Se o parser
leu errado, corrija-o e **escreva o teste de regressão** com o valor real.

**Alerta que não deveria existir** (concurso antigo, lotação fora de Rio Branco): verifique
`publication_date`, `registration_deadline` e `assignment_confirmed`. As defesas contra isso
vivem em `eligibility.evaluate` e `parse.location_context`.

---

## 6. Execução presa em RUNNING

```sql
SELECT id, started_at, status FROM monitor_runs
WHERE status = 'RUNNING' AND started_at < now() - interval '2 hours';
```

O lock consultivo é de sessão: quando o processo morre, o PostgreSQL o libera sozinho, então
**a agenda não fica travada**. A linha `RUNNING` é só registro órfão.

```sql
UPDATE monitor_runs SET status = 'FAILED', finished_at = now()
WHERE status = 'RUNNING' AND started_at < now() - interval '2 hours';
```

Para confirmar que nenhum lock ficou pendurado:

```sql
SELECT * FROM pg_locks WHERE locktype = 'advisory';
```

---

## 7. Telegram parou de entregar

```bash
sentinela test-notification
```

| Resultado | Causa | Ação |
|---|---|---|
| `BLOCKED` | Token/chat ausente, inválido, ou bot bloqueado | Confira segredos; desbloqueie o bot no app |
| `RETRY` | Rate limit ou rede | O outbox reenvia sozinho; `sentinela retry` força |
| `UNCERTAIN` | Resposta ambígua após o envio | **Não reenvie às cegas** — veja abaixo |

`UNCERTAIN` é deliberado: Telegram não tem chave de idempotência, então um reenvio cego pode
duplicar uma mensagem que chegou. Confira no app e resolva:

```sql
-- chegou:
UPDATE notifications SET status='SENT', sent_at=now() WHERE status='UNCERTAIN' AND id='<id>';
-- não chegou:
UPDATE notifications SET status='PENDING', not_before=NULL WHERE status='UNCERTAIN' AND id='<id>';
```

Depois: `sentinela retry`.

Nenhum alerta se perde nesse meio-tempo: eles ficam no outbox até serem resolvidos.

---

## 8. Fila de revisão crescendo

**Sintoma:** `sentinela doctor` mostra muitos documentos em `REVIEW`.

```sql
SELECT review_reason, count(*) FROM documents
WHERE processing_state = 'REVIEW' GROUP BY 1 ORDER BY 2 DESC;
```

| Motivo | Significado | Ação |
|---|---|---|
| `Página sem edital, datas ou quadro de cargos` | Página de navegação — **normal e esperado** | Nada |
| `Nenhuma tabela de cargos reconhecida` | Notícia sobre concurso, sem o edital | Nada; o PDF chega depois |
| `Extracao NO_TEXT_LAYER` | PDF escaneado | Instale Tesseract, ou leia manualmente |
| `Extracao PARSER_ERROR` | PDF corrompido | Verifique a URL |
| `Texto truncado` | Documento acima do limite | Suba `max_document_bytes` em `config.yaml` |

```bash
sentinela retry
```

A auditoria semanal reprocessa essa fila sozinha. Ela existe justamente para nada ser
descartado em silêncio.

---

## 9. Restaurar o banco

Backups diários ficam nos artefatos do workflow `database-backup` (retenção 30 dias). Cada
execução **verifica e testa a restauração** — um backup que ninguém consegue restaurar não
é backup.

**Backup JSON** (`sentinela-*.json.gz`):

```bash
python - <<'PY'
from sentinela.backup import restore_json, verify
from sentinela.db import create_engine
from sentinela.models import Base

engine = create_engine("postgresql+psycopg://.../banco_restaurado")
Base.metadata.create_all(engine)
print(verify("backups/sentinela-....json.gz"))
print(restore_json(engine, "backups/sentinela-....json.gz"))
PY
```

`restore_json` **recusa** rodar sobre um banco que já tem oportunidades — restaure sempre em
um banco limpo para não sobrescrever evidência.

**Backup pg_dump** (`sentinela-*.dump.gz`):

```bash
gunzip -c backups/sentinela-....dump.gz | psql "$DATABASE_URL_RESTAURADO"
```

Depois, valide:

```bash
DATABASE_URL=<restaurado> sentinela doctor
DATABASE_URL=<restaurado> sentinela opportunities
```

**Frequência:** diária, 03:07 (Rio Branco) · **Retenção:** 30 dias · **Verificação:** toda
execução · **Teste de restauração:** toda execução.

---

## 10. Migrar de banco

```bash
alembic upgrade head                    # no banco novo
sentinela backup                        # no antigo
# restaure (seção 9)
DATABASE_URL=<novo> sentinela doctor
```

Atualize o segredo `DATABASE_URL` no GitHub. O histórico de idempotência viaja junto, então
nenhum alerta antigo é reenviado.

---

## 11. Conferir se o sistema está realmente vigiando

Uma vez por mês, dois minutos:

```bash
sentinela doctor              # tudo OK, watchdog OK
sentinela sources             # quantas degradadas
sentinela report --show       # tem conteúdo plausível
sentinela test-notification   # o canal entrega
```

E olhe as execuções recentes em GitHub → Actions.

---

## Escalada

| Situação | Gravidade | Prazo |
|---|---|---|
| Watchdog `CRITICAL` | Alta — nada está sendo vigiado | mesmo dia |
| Banco inacessível | Alta | mesmo dia |
| Telegram `BLOCKED` | Alta — alertas não chegam | mesmo dia |
| 5+ fontes degradadas | Média | mesma semana |
| 1 fonte degradada | Baixa | auditoria semanal resolve ou avisa |
| Fila de revisão crescendo | Baixa | auditoria semanal |
