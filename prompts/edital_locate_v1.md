# edital_locate_v1

Você recebe o texto de um documento oficial brasileiro de concurso ou processo seletivo.

Sua tarefa é **apontar onde** certas informações estão escritas. Você **não** decide nada:
outro programa vai reler cada trecho que você citar e extrair o valor por conta própria. Se
o trecho que você citar não existir literalmente no documento, ou não sustentar o que você
afirmou, sua resposta inteira para aquele campo é descartada.

Por isso, a única coisa que importa é: **cite o trecho exato**.

## Regras

1. `raw_evidence` deve ser **copiado literalmente** do documento, com no mínimo 25
   caracteres. Não parafraseie, não resuma, não corrija, não traduza.
2. Se a informação não estiver no documento, **omita o campo**. Nunca invente.
3. Um trecho deve conter o valor que você afirma. Citar "o cargo terá carga horária
   definida em portaria" para afirmar 30 horas é errado: o trecho não diz 30.
4. Um documento pode ter vários cargos. Devolva um item por cargo.
5. **Local de prova nunca é local de lotação.** Não use uma frase sobre onde a prova é
   aplicada para afirmar onde a pessoa vai trabalhar.
6. Responda **apenas** com JSON válido.

## Formato

```json
{
  "positions": [
    {
      "name": "nome do cargo exatamente como aparece",
      "fields": {
        "weekly_workload": {"value": 30, "raw_evidence": "trecho literal que diz 30 horas semanais"},
        "salary":          {"value": 4656.75, "raw_evidence": "trecho literal com o valor"},
        "education":       {"value": "high_school", "raw_evidence": "trecho literal sobre escolaridade"},
        "vacancies":       {"value": 1, "raw_evidence": "trecho literal com o numero de vagas"}
      }
    }
  ]
}
```

`education` aceita: `high_school`, `higher_education`, `primary`.
`weekly_workload` em horas por semana, número. `salary` em reais, número decimal sem `R$`.

Se o documento não descreve nenhum cargo, devolva `{"positions": []}`.
