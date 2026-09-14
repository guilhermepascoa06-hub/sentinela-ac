# Fontes monitoradas

Este arquivo é o inventário das fontes. A configuração viva é **`sources.yaml`** — este
documento explica o que cada uma cobre e onde cada uma falha.

## Hierarquia de confiança

| Nível | O que é | Pode gerar alerta primário? |
|---|---|---|
| 1 | Edital oficial, diário oficial, site do órgão | sim |
| 2 | Banca organizadora oficial do certame | sim |
| 3 | Portal especializado em concursos | **não** — só descoberta |
| 4 | Notícia ou agregador | **não** — só descoberta |

Uma oportunidade vinda apenas de nível 3 ou 4 fica registrada, mas o sistema procura o
documento oficial antes de avisar o usuário.

**0 fontes registradas** — 0 oficiais, 0 de descoberta.

## Resumo

| ID | Instituição | Nível | Estado | Última coleta OK | Docs |
|---|---|---|---|---|---|

## Detalhe por fonte

## Fontes descobertas automaticamente

A auditoria semanal procura portais oficiais novos linkados pelas fontes já confiáveis.
Elas entram como `CANDIDATE`, nunca produzem alerta primário e só viram `TRUSTED` após
validação (`sentinela sources` mostra quem está pendente).
