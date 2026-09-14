# Avaliação de evolução — 14/09/2026

Inspeção do código local, sem consulta ou alteração do banco de produção e sem enviar
mensagens. AGENTS.md lido integralmente. Os números de produção deste documento não foram
recontados: esta avaliação trata do produto e das ligações presentes no código.

1. **Consulta por cargo com evidência.** O bot possui apenas quatro consultas prontas
   (`bot.py:198-203`). Texto livre depende de IA (`bot.py:263-267`) e recebe somente os dez
   primeiros cargos elegíveis (`bot.py:212`). Requisitos, motivos do filtro e evidência já
   existem (`models.py:263-288`), mas não chegam à resposta. Prioridade: busca por nome/órgão,
   ficha individual, distinção entre compatibilidade e inscrição aberta, histórico e
   localização do trecho oficial; responder consultas comuns sem depender do modelo.

2. **Acompanhamento pessoal separado dos dados oficiais.** O banco representa editais,
   cargos e prazos (`models.py:204-327`), mas não escolhas do candidato. Marcar interesse,
   inscrição feita ou pagamento concluído permitiria próximos passos relevantes, mantendo
   esses registros claramente como anotações do usuário. Os alertas atuais são por certame
   (`pipeline.py:809-857`); a evolução precisa ser ligada a eles e ao bot, com migration e
   testes, sem confundir conclusão pessoal com confirmação oficial da inscrição.

3. **Agenda e comparação acessíveis.** O relatório é somente Markdown (`report.py:290-301`)
   e repete cargos entre categorias. Um painel navegável pode expor filtros, comparação,
   ficha e agenda exportável a partir dos mesmos fatos. As consultas de prazo do bot e do
   relatório não filtram certames suspensos/cancelados (`bot.py:111-121`,
   `report.py:128-137`), ao contrário dos alertas (`pipeline.py:825-830`). Corrigir a
   consistência de estado antes de ampliar a apresentação.

Limites observados: a consulta `/vagas` chama de compatíveis agora os cargos de um certame
com inscrições vencidas porque só exclui CANCELLED e EXPIRED (`bot.py:65-75`); listas
longas são cortadas por caractere, sem paginação (`bot.py:304`, `bot.py:349`).

Implementação autorizada nesta frente: consultas determinísticas do bot e testes locais.
As referências de linha acima apontam para o estado anterior às mudanças desta data.
