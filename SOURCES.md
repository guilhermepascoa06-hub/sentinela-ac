# Fontes monitoradas

Inventário das fontes. A configuração viva é **`sources.yaml`**; este documento
explica o que cada uma cobre e, principalmente, onde cada uma falha.

**33 fontes** — 32 oficiais e 1 de descoberta. Na última coleta real: 26 saudáveis, 7 degradadas.

## Hierarquia de confiança

| Nível | O que é | Pode gerar alerta primário? |
|---|---|---|
| 1 | Edital oficial, diário oficial, site do órgão | sim |
| 2 | Banca organizadora oficial do certame | sim |
| 3 | Portal especializado em concursos | **não** — só descoberta |
| 4 | Notícia ou agregador | **não** — só descoberta |

Um certame conhecido apenas por nível 3 ou 4 fica registrado, mas o Sentinela procura
o documento oficial antes de avisar o usuário.

## Resumo

| ID | Instituição | Nível | Estado | Última coleta OK | Docs |
|---|---|---|---|---|---|
| `camara-rio-branco` | Câmara Municipal de Rio Branco | 1 | HEALTHY | 14/09/2026 00:48 | 7 |
| `prefeitura-rio-branco` | Prefeitura Municipal de Rio Branco | 1 | HEALTHY | 14/09/2026 00:48 | 8 |
| `banco-amazonia` | Banco da Amazônia | 1 | HEALTHY | 14/09/2026 00:48 | 1 |
| `banco-brasil` | Banco do Brasil | 1 | DEGRADED | nunca | — |
| `caixa` | Caixa Econômica Federal | 1 | HEALTHY | 14/09/2026 00:48 | 6 |
| `doe-acre` | Governo do Estado do Acre | 1 | HEALTHY | 14/09/2026 00:48 | 0 |
| `mpac` | Ministério Público do Estado do Acre | 1 | HEALTHY | 14/09/2026 00:49 | 7 |
| `rb-simplificado` | Prefeitura Municipal de Rio Branco | 1 | HEALTHY | 14/09/2026 00:49 | 0 |
| `sead-acre` | Governo do Estado do Acre - SEAD | 1 | DEGRADED | nunca | — |
| `tjac` | Tribunal de Justiça do Estado do Acre | 1 | DEGRADED | nunca | — |
| `aleac` | Assembleia Legislativa do Estado do Acre | 1 | HEALTHY | 14/09/2026 00:51 | 1 |
| `conab` | Companhia Nacional de Abastecimento | 1 | HEALTHY | 14/09/2026 00:51 | 6 |
| `crea-ac` | Conselho Regional de Engenharia e Agronomi | 1 | HEALTHY | 14/09/2026 00:51 | 6 |
| `crm-ac` | Conselho Regional de Medicina do Acre | 1 | HEALTHY | 14/09/2026 00:51 | 3 |
| `dou` | Imprensa Nacional | 1 | HEALTHY | 14/09/2026 00:51 | 0 |
| `dpe-ac` | Defensoria Pública do Estado do Acre | 1 | HEALTHY | 14/09/2026 00:51 | 0 |
| `estado-acre` | Governo do Estado do Acre | 1 | DEGRADED | nunca | — |
| `ibge` | Instituto Brasileiro de Geografia e Estatí | 1 | DEGRADED | nunca | — |
| `ifac` | Instituto Federal do Acre | 1 | HEALTHY | 14/09/2026 00:53 | 7 |
| `tce-ac` | Tribunal de Contas do Estado do Acre | 1 | HEALTHY | 14/09/2026 00:53 | 0 |
| `tre-ac` | Tribunal Regional Eleitoral do Acre | 1 | HEALTHY | 14/09/2026 00:53 | 6 |
| `ufac` | Universidade Federal do Acre | 1 | HEALTHY | 14/09/2026 00:53 | 8 |
| `cnu` | Ministério da Gestão e da Inovação em Serv | 1 | HEALTHY | 14/09/2026 00:54 | 6 |
| `correios` | Empresa Brasileira de Correios e Telégrafo | 1 | HEALTHY | 14/09/2026 00:54 | 6 |
| `funai` | Fundação Nacional dos Povos Indígenas | 1 | DEGRADED | nunca | — |
| `ibama` | Instituto Brasileiro do Meio Ambiente e do | 1 | HEALTHY | 14/09/2026 00:54 | 6 |
| `inss` | Instituto Nacional do Seguro Social | 1 | HEALTHY | 14/09/2026 00:54 | 6 |
| `transparencia-acre` | Governo do Estado do Acre - SEAD | 1 | HEALTHY | 14/09/2026 00:54 | 1 |
| `trf1` | Tribunal Regional Federal da 1ª Região | 1 | HEALTHY | 14/09/2026 00:55 | 6 |
| `trt14` | Tribunal Regional do Trabalho da 14ª Regiã | 1 | HEALTHY | 14/09/2026 00:55 | 6 |
| `idib` | Instituto de Desenvolvimento Institucional | 2 | DEGRADED | nunca | — |
| `cebraspe-tce-ac` | Tribunal de Contas do Estado do Acre | 2 | HEALTHY | 14/09/2026 00:55 | 1 |
| `pci` | PCI Concursos | 3 | HEALTHY | 14/09/2026 00:55 | 6 |

## Detalhe por fonte

### `camara-rio-branco` — Câmara de Rio Branco

- **Instituição:** Câmara Municipal de Rio Branco
- **URL:** https://www.riobranco.ac.leg.br/institucional/noticias
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.riobranco.ac.leg.br`, `riobranco.ac.leg.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:48
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Página transparência/concurso/Concursos está desatualizada em relação ao edital 01/2026; monitorar notícias e documento oficial.

### `prefeitura-rio-branco` — Prefeitura de Rio Branco - Editais

- **Instituição:** Prefeitura Municipal de Rio Branco
- **URL:** https://www.riobranco.ac.gov.br/editais-processos-seletivos/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.riobranco.ac.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:48
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Mistura editais de contratação, esporte e PSS; paginação precisa de auditoria.

### `banco-amazonia` — Banco da Amazônia - Concursos

- **Instituição:** Banco da Amazônia
- **URL:** https://www.bancoamazonia.com.br/acesso-informacao/concursos-e-empregados
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.bancoamazonia.com.br`, `www.bancodamazonia.com.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:48
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Site em migração, conteúdo parcialmente renderizado. Polo de inscrição não garante unidade sem edital.

### `banco-brasil` — Banco do Brasil - Concurso

- **Instituição:** Banco do Brasil
- **URL:** https://www.bb.com.br/site/concurso-bb/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.bb.com.br`
- **Saúde:** DEGRADED — responde, mas não entrega os documentos esperados (falhas consecutivas: 1)
- **Última coleta bem-sucedida:** nunca
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Página depende de JavaScript; não confundir notícias especulativas com edital publicado.

### `caixa` — CAIXA - Trabalhe na Caixa

- **Instituição:** Caixa Econômica Federal
- **URL:** https://www.caixa.gov.br/sobre-a-caixa/trabalhe-na-caixa/paginas/default.aspx
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.caixa.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:48
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Portal pode bloquear automação. Confirmar polo e unidade; nível superior também aparece.

### `doe-acre` — Diário Oficial do Acre

- **Instituição:** Governo do Estado do Acre
- **URL:** https://www.diario.ac.gov.br/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.diario.ac.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:48
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Diários extensos; limite de documentos e extração genérica não garantem leitura exaustiva de todas as seções.

### `mpac` — MPAC - Servidores

- **Instituição:** Ministério Público do Estado do Acre
- **URL:** https://www.mpac.mp.br/concursos/servidores/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.mpac.mp.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:49
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Página inclui concursos anteriores; acompanhar anexos e retificações.

### `rb-simplificado` — RB Simplificado

- **Instituição:** Prefeitura Municipal de Rio Branco
- **URL:** https://rbsimplificado.riobranco.ac.gov.br/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `rbsimplificado.riobranco.ac.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:49
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Portal mantém certames antigos; calendário deve prevalecer sobre data de descoberta.

### `sead-acre` — SEAD Acre - Concursos

- **Instituição:** Governo do Estado do Acre - SEAD
- **URL:** https://sead.ac.gov.br/gestao-governamental/editais-e-concursos/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `sead.ac.gov.br`
- **Saúde:** DEGRADED — responde, mas não entrega os documentos esperados (falhas consecutivas: 1)
- **Última coleta bem-sucedida:** nunca
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Portal em migração; recrutamento-e-selecao contém histórico complementar.

### `tjac` — TJAC - Concursos e Processos Seletivos

- **Instituição:** Tribunal de Justiça do Estado do Acre
- **URL:** https://www.tjac.jus.br/adm/processos-seletivos/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.tjac.jus.br`
- **Saúde:** DEGRADED — responde, mas não entrega os documentos esperados (falhas consecutivas: 1)
- **Última coleta bem-sucedida:** nunca
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Separar concurso externo, remoção interna, estágio e convocação de aprovados.

### `aleac` — ALEAC - Portal da Transparência

- **Instituição:** Assembleia Legislativa do Estado do Acre
- **URL:** https://app.al.ac.leg.br/servicos?page=0
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `app.al.ac.leg.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:51
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Índice de serviços aponta Diário Eletrônico Legislativo; nenhuma lista recente de concursos validada.

### `conab` — Conab - Concursos em Aberto

- **Instituição:** Companhia Nacional de Abastecimento
- **URL:** https://www.gov.br/conab/pt-br/acesso-a-informacao/empregados-publicos/concursos-publicos/concursos-em-aberto/concursos-em-aberto-2
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:51
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Título em aberto pode incluir homologação; conferir inscrições e regional do Acre.

### `crea-ac` — CREA-AC - Transparência

- **Instituição:** Conselho Regional de Engenharia e Agronomia do Acre
- **URL:** https://www.creaac.org.br/portal/?categoria=36&ir=documento
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.creaac.org.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:51
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Comissão para concurso criada pela Portaria 060/2025; comissão não é edital de inscrições.

### `crm-ac` — CRM-AC - Concursos

- **Instituição:** Conselho Regional de Medicina do Acre
- **URL:** https://transparencia.crmac.org.br/concursos/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `transparencia.crmac.org.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:51
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Upload em 2025 contém edital de 2021; data de upload não representa concurso novo.

### `dou` — Diário Oficial da União

- **Instituição:** Imprensa Nacional
- **URL:** https://www.in.gov.br
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.in.gov.br`, `in.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:51
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Portal nacional de grande volume e busca dinâmica; cobertura genérica não é leitura integral do DOU.

### `dpe-ac` — DPE-AC

- **Instituição:** Defensoria Pública do Estado do Acre
- **URL:** https://defensoria.ac.def.br/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `defensoria.ac.def.br`, `portalcandidato.ac.def.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:51
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Homepage genérica; Portal Candidato pode depender de JavaScript e frequentemente contém estágios.

### `estado-acre` — Governo Acre - Chamadas Públicas e Editais

- **Instituição:** Governo do Estado do Acre
- **URL:** https://estado.ac.gov.br/acre/chamadas-publicas-e-editais/
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `estado.ac.gov.br`
- **Saúde:** DEGRADED — responde, mas não entrega os documentos esperados (falhas consecutivas: 1)
- **Última coleta bem-sucedida:** nunca
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Inclui Fundhacre e outros órgãos/fundações, mas também bolsas, residência e chamamentos sem emprego.

### `ibge` — IBGE - Trabalhe Conosco

- **Instituição:** Instituto Brasileiro de Geografia e Estatística
- **URL:** https://www.ibge.gov.br/acesso-informacao/institucional/trabalhe-conosco.html
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.ibge.gov.br`
- **Saúde:** DEGRADED — responde, mas não entrega os documentos esperados (falhas consecutivas: 1)
- **Última coleta bem-sucedida:** nunca
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Separar efetivo, temporário e estágio; arquivos podem estar em FTP/hosts adicionais não autorizados.

### `ifac` — IFAC - Concursos

- **Instituição:** Instituto Federal do Acre
- **URL:** https://www.ifac.edu.br/o-ifac/gestao-de-pessoas/concursos
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.ifac.edu.br`, `selecoes.ifac.edu.br`, `sigrh.ifac.edu.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:53
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Concursos e seleções distribuídos por portal institucional, SIGRH e selecoes.ifac.edu.br; separar seleção de alunos.

### `tce-ac` — TCE-AC

- **Instituição:** Tribunal de Contas do Estado do Acre
- **URL:** https://www.tceac.tc.br
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.tceac.tc.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:53
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Abertura pelo navegador de pesquisa falhou; domínio institucional confirmado por sistemas.tceac.tc.br. Cobertura alternativa Cebraspe, sem contornar bloqueios.

### `tre-ac` — TRE-AC - Concurso público

- **Instituição:** Tribunal Regional Eleitoral do Acre
- **URL:** https://www.tre-ac.jus.br/institucional/concurso-publico
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www.tre-ac.jus.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:53
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Técnico Judiciário não implica ensino médio: validar escolaridade no edital vigente.

### `ufac` — UFAC - Portal de Editais

- **Instituição:** Universidade Federal do Acre
- **URL:** https://www3.ufac.br/recentes
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `www3.ufac.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:53
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Mistura seleção acadêmica e pessoal; requer confirmação do cargo e campus de lotação.

### `cnu` — Concurso Público Nacional Unificado

- **Instituição:** Ministério da Gestão e da Inovação em Serviços Públicos
- **URL:** https://www.gov.br/gestao/pt-br/concursonacional/editais
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:54
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Editais nacionais extensos; cidade de prova não é lotação. Banco de aprovados não abre novas inscrições.

### `correios` — Correios - PROSEL

- **Instituição:** Empresa Brasileira de Correios e Telégrafos
- **URL:** https://prosel.correios.com.br/concursos
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `prosel.correios.com.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:54
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Lista nacional; aprendizes não equivalem a emprego público permanente.

### `funai` — Funai - Processos Seletivos

- **Instituição:** Fundação Nacional dos Povos Indígenas
- **URL:** https://www.gov.br/funai/pt-br/acesso-a-informacao/servidores/servidores-funai/processos_seletivos
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.gov.br`
- **Saúde:** DEGRADED — responde, mas não entrega os documentos esperados (falhas consecutivas: 1)
- **Última coleta bem-sucedida:** nunca
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Inclui remoção interna; excluir seleção restrita a servidores e validar requisitos indígenas.

### `ibama` — Ibama - Concursos

- **Instituição:** Instituto Brasileiro do Meio Ambiente e dos Recursos Naturais Renováveis
- **URL:** https://www.gov.br/ibama/pt-br/acesso-a-informacao/concursos
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:54
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Concursos nacionais; exigir lotação Rio Branco e não inferir ensino médio do nome técnico.

### `inss` — INSS - Concursos

- **Instituição:** Instituto Nacional do Seguro Social
- **URL:** https://www.gov.br/inss/pt-br/acesso-a-informacao/servidores/concursos-publicos
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:54
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Validar gerência/agência de lotação e escolaridade por edital.

### `transparencia-acre` — Transparência Acre - Concursos

- **Instituição:** Governo do Estado do Acre - SEAD
- **URL:** https://transparencia.ac.gov.br/conteudo/concursos
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 8 documentos por execução
- **Hosts permitidos:** `transparencia.ac.gov.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:54
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Tabela pode depender de JavaScript; dados indicavam atualização em 26/05/2026.

### `trf1` — TRF1 - Concursos Servidores

- **Instituição:** Tribunal Regional Federal da 1ª Região
- **URL:** https://www.trf1.jus.br/trf1/concursos/concursos-servidores
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.trf1.jus.br`, `trf1.jus.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:55
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Região inclui Acre; prova em Rio Branco não comprova lotação. Exige verificar escolaridade atual.

### `trt14` — TRT14 - Concursos Servidores

- **Instituição:** Tribunal Regional do Trabalho da 14ª Região
- **URL:** https://portal.trt14.jus.br/portal/concursos
- **Classificação:** Nível 1 — oficial · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `portal.trt14.jus.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:55
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Abrange Rondônia e Acre; não presumir lotação na capital acreana.

### `idib` — IDIB - Concursos

- **Instituição:** Instituto de Desenvolvimento Institucional Brasileiro
- **URL:** https://www.idib.org.br
- **Classificação:** Nível 2 — banca organizadora · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.idib.org.br`, `idib.org.br`
- **Saúde:** DEGRADED — responde, mas não entrega os documentos esperados (falhas consecutivas: 1)
- **Última coleta bem-sucedida:** nunca
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Banca confirmada pela Câmara de Rio Branco no edital 01/2026. Site falhou na ferramenta de pesquisa; não contornar bloqueio.

### `cebraspe-tce-ac` — Cebraspe - TCE Acre

- **Instituição:** Tribunal de Contas do Estado do Acre
- **URL:** https://www.cebraspe.org.br/concursos/TCE_AC_26
- **Classificação:** Nível 2 — banca organizadora · oficial · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.cebraspe.org.br`, `cdn.cebraspe.org.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:55
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Banca vinculada a anúncio institucional; seleção 2026 de conselheiro exige superior e experiência.

### `pci` — PCI Concursos - Descoberta

- **Instituição:** PCI Concursos
- **URL:** https://www.pciconcursos.com.br/concursos/
- **Classificação:** Nível 3 — portal especializado · secundária, só descoberta · `TRUSTED`
- **Coleta:** adaptador `generic` sobre HTML, até 6 documentos por execução
- **Hosts permitidos:** `www.pciconcursos.com.br`
- **Saúde:** HEALTHY — coletando normalmente (falhas consecutivas: 0)
- **Última coleta bem-sucedida:** 14/09/2026 00:55
- **Última validação de URL:** 13/09/2026
- **Limitações conhecidas:** Somente descoberta. Toda oportunidade requer confirmação institucional ou banca legitimada.

## Fontes de descoberta

Fontes de nível 3 e 4 existem para o sistema tomar conhecimento de que algo foi
publicado. Elas **nunca** são a fonte final: ao encontrar um certame por elas, o
Sentinela procura o edital oficial antes de gerar alerta primário.

## Fontes descobertas automaticamente

A auditoria semanal procura portais oficiais novos linkados pelas fontes já confiáveis.
Elas entram como `CANDIDATE`, nunca produzem alerta primário, e só viram `TRUSTED` após
validação. `sentinela sources` mostra quais estão pendentes (marcadas com `*`).

## Como acrescentar ou consertar uma fonte

Ver [README.md](README.md#adicionar-uma-fonte) e [RUNBOOK.md](RUNBOOK.md#4-parser-quebrado--site-mudou-de-layout).
