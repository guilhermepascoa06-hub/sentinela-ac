# edital_validation_v1

Você recebe (a) um trecho de documento oficial e (b) um conjunto de campos que um parser
determinístico extraiu dele. Sua tarefa é **conferir**, não extrair.

Para cada campo responda apenas:

- `CONFIRMED` — o texto sustenta exatamente esse valor;
- `CONTRADICTED` — o texto diz outra coisa (informe o que ele diz em `raw_evidence`);
- `UNSUPPORTED` — o texto não permite afirmar nada sobre esse campo.

Nunca proponha um valor novo para um campo marcado `UNSUPPORTED`.

```json
{ "checks": { "<campo>": { "verdict": "...", "raw_evidence": "..." } } }
```
