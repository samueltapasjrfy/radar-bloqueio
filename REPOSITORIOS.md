# Mapa dos repositórios — Informador Trabalhista

> **Cópia idêntica nos 5 repositórios** (`mono-repo-trabalhista`, `datajur-trabalhista`,
> `lety-trabalhista`, `sentence-trabalhista`, `radar-bloqueio`). Mudou algo aqui? Atualize nos cinco e faça commit + push nos cinco.

## O produto em uma frase

O advogado cadastra o número de um processo trabalhista (24 TRTs + TST). O sistema busca
a **capa** e a **petição inicial** no PJe, e a **Lety** (IA) lê a inicial e organiza o caso.
Com isso o sistema monta o **e-mail de pedido de subsídios** ao cliente. O **radar de audiências**
verifica, às 10h, 14h e 17h, andamentos, pauta e publicações dos processos marcados e mantém a
agenda de audiências (uma por processo e dia, com status atualizado). Para clientes com o
recurso (hoje a SABESP), a captura também traz a **defesa** do cliente e a Lety escreve as
**orientações para audiência** no modelo do escritório (DOCX/PDF). O **radar de sentença** baixa
cada sentença nova do PJe e o **agente de sentenças** lê a procedência, como o cliente saiu e cada
pedido: daí saem os **insights por cliente** (percentual de improcedência, onde perde e por quê).
O **radar de bloqueio** acompanha processos de **TJs e TRFs** (não só trabalhistas): uma vez por dia
lê os autos e diz o risco de bloqueio judicial (SISBAJUD) contra o cliente réu, por quê e o que fazer,
e avisa quando o risco muda.

```
 navegador ──► mono-repo-trabalhista (web React + API NestJS + Postgres)
                    │  fila "capturas" (Postgres)          │  fila "analises" (Postgres)
                    ▼                                      ▼
   worker de captura (apps/worker)          worker da Lety (apps/worker, comando .lety)
     usa o pacote ──► datajur-trabalhista     usa o pacote ──► lety-trabalhista
     (PJe-JT: login Loy, capa, inicial, OCR,   (OpenAI: análise estruturada da inicial)
      radar: andamentos, pauta, sentenças)     e o pacote ──► sentence-trabalhista
                                               (OpenAI: procedência e pedidos da sentença; fila "sentencas")
                    │  fila "bloqueios" (Postgres)
                    ▼
   worker do radar de bloqueio (apps/worker, comando .bloqueio)
     usa o pacote ──► radar-bloqueio (TJs e TRFs: PJe, eproc, e-SAJ, Projudi, TJRJ, TJSE; login Loy;
                      fatos por regra + leitura da IA + risco por regra)
```

## Os 5 repositórios

| Repositório | Pasta local | Linguagem | O que é | Quem usa |
|---|---|---|---|---|
| [`mono-repo-trabalhista`](https://github.com/samueltapasjrfy/mono-repo-trabalhista) | `~/Documents/mono-repo-trabalhista` | TS (NestJS, React) + Python (workers) | **O produto.** API, site, banco, filas, workers, e-mail de subsídios, deploy | Usuários |
| [`datajur-trabalhista`](https://github.com/samueltapasjrfy/datajur-trabalhista) | `~/Documents/datajur-trabalhista` | Python | **Motor de captura.** Login por certificado na Loy Trust, sessões dos 49 portais, API interna do PJe-JT, capa, partes, audiências e PDF + texto da inicial (OCR) | Worker de captura do monorepo (dependência git) |
| [`lety-trabalhista`](https://github.com/samueltapasjrfy/lety-trabalhista) | `~/Documents/lety-trabalhista` | Python | **A IA (Lety).** Prompt, schema da saída, chamada à OpenAI, custo e a avaliação contra casos reais | Worker da Lety do monorepo (dependência git) |
| [`sentence-trabalhista`](https://github.com/samueltapasjrfy/sentence-trabalhista) | `~/Documents/sentence-trabalhista` | Python | **Agente de sentenças.** Procedência, resultado para o cliente (reclamada) e pedidos julgados, com a avaliação contra o movimento do PJe | Worker da Lety do monorepo, fila "sentencas" (dependência git) |
| [`radar-bloqueio`](https://github.com/samueltapasjrfy/radar-bloqueio) | `~/Documents/radar-bloqueio` | Python | **Motor do radar de bloqueio.** Risco de bloqueio judicial (SISBAJUD) contra o cliente réu em TJs e TRFs: sessões Loy, leitores de cada sistema, fatos por regra, leitura da IA e risco por regra | Worker do radar de bloqueio do monorepo, fila "bloqueios" (dependência git) |

Repositórios **só de referência**, que não fazem parte do produto (não mexer por causa dele):
- `poc-protocolo-jurify`: origem do design system e dos padrões de API e fila.
- `robo-pje-mg`: origem do login Loy; o guia está em `datajur-trabalhista/LOY_LOGIN_GUIA.md`.
- `petitusAgent`: predecessor da Lety, substituído por ela.
- `JurifyDev/agent-procedencia` e `JurifyDev/sentius-pedidos`: predecessores do agente de sentenças (genéricos, não trabalhistas).
- `bmg-block-service` (só local, sem git): piloto do radar de bloqueio para o BMG, com o Postgres `bmg_block` usado
  na regressão do `radar-bloqueio`. O código vivo é o `radar-bloqueio`.

## Onde mexer

| Quero... | Repositório | Onde |
|---|---|---|
| Mudar tela, componente, texto da interface | mono-repo | `apps/web/src/features/<area>/` (primitivos em `components/ui/`; regras em `DESIGN_SYSTEM.md`) |
| Mudar regra de negócio, rota, permissão | mono-repo | `apps/api/src/modules/<contexto>/` (iam, processos, captura, lety, subsidios, arquivos) |
| Mudar banco | mono-repo | `apps/api/prisma/schema.prisma` + `pnpm db:migrate --name <x>` (nunca `db push`) |
| Mudar o **texto do e-mail de subsídios** | mono-repo | `packages/shared-schemas/src/subsidios.ts` (`montarEmailSubsidios`, `PARAGRAFO_DOCUMENTOS_PADRAO`) + teste |
| Validar número CNJ / tribunais da JT | mono-repo | `packages/shared-schemas/src/cnj.ts` |
| Mudar o que a fila reagenda ou abandona | mono-repo | `apps/api/src/modules/captura/domain/classificar-resultado.ts` e `lety.use-cases.ts` |
| Mudar como a capa do motor vira banco | mono-repo | `apps/api/src/modules/processos/domain/mapear-captura.ts` |
| Mudar o laço dos workers (concorrência, heartbeat) | mono-repo | `apps/worker/src/captura_worker/` (`worker.py`, `__main__.py`, `lety.py`) |
| Deploy, Nginx, systemd, servidores | mono-repo | `deploy/` (+ `README.md`, seção Produção) |
| **Captura falhando num TRT**, login Loy, sessão expirando | datajur | `src/datajur/` (`loy.py`, `sessao.py`, `pje_client.py`, `keeper.py`, `tribunais.py`) |
| Texto da inicial saindo errado (ordem, acentos, OCR) | datajur | `src/datajur/informador.py` (`texto_pagina`, `extrair_texto`) |
| Achar a inicial certa nos autos / novo dado do PJe (ex.: audiências) | datajur | `src/datajur/informador.py` (`localizar_inicial`, `normalizar_*`) e `pje_client.py` |
| **Melhorar a análise da IA** (resumo, contrato, pedidos) | lety | `src/lety/prompts/analise_inicial.md` (+ subir `VERSAO_PROMPT` em `analisar.py`) |
| Novo campo na análise da Lety | lety **e** mono-repo | `src/lety/schema.py` + espelho opcional em `packages/shared-schemas/src/lety.ts` + tela `apps/web/src/features/lety/` |
| Trocar o modelo da IA ou o raciocínio | lety | `LETY_MODELO` / `LETY_RACIOCINIO`, depois de rodar `avaliacao/avaliar.py` |
| **Orientações para audiência**: texto/seções fixas do documento, chassi, e-mail de envio | mono-repo | `packages/shared-schemas/src/orientacao.ts` (`blocosOrientacao`, `emailOrientacao`) + `apps/api/assets/orientacao-fc.docx` |
| Orientações: melhorar síntese, tese, contrato, alertas | lety | `src/lety/prompts/orientacao_audiencia.md` (+ `VERSAO_PROMPT_ORIENTACAO`, rodar `avaliacao/orientacao/avaliar.py`) |
| Orientações: achar a defesa do cliente / endereço da audiência / trechos do contrato anexo (administrador, assinaturas) | datajur | `src/datajur/informador.py` (`localizar_defesa`, `extrair_endereco_audiencia`, `trechos_contrato`) |
| **Radar de audiências**: regra de consolidação (um processo + um dia = uma audiência, status, alertas) | mono-repo | `packages/shared-schemas/src/radar.ts` (`consolidarAudiencias`) + teste |
| Radar: horários (10h, 14h, 17h), fila, histórico, agenda | mono-repo | `apps/api/src/modules/radar/` e `apps/web/src/features/radar/` |
| Radar: coleta de andamentos/pauta do PJe e publicações do DJEN, leitura por regra dos movimentos, pré-filtro | datajur | `src/datajur/radar.py` |
| **Radar de arquivamento**: regra (movimento "Arquivados os autos definitivamente" + fase "Arquivados" do PJe, desarquivamento, conferir) | mono-repo | `packages/shared-schemas/src/arquivamento.ts` (`consolidarArquivamento`) + teste |
| Arquivamento: recálculo da base, tela `/arquivamentos`, planilha | mono-repo | `apps/api/src/modules/radar/application/arquivamento.use-cases.ts` e `apps/web/src/features/arquivamentos/` |
| Arquivamento: fase atual de cada instância na coleta do radar | datajur | `src/datajur/radar.py` (`situacao_capa`, campo `capas`) |
| **Prazo da defesa**: defesa no PJe 12 h antes da audiência (agendador, busca no PJe, avisos), filtro e selos | mono-repo | `packages/shared-schemas/src/prazo-defesa.ts`, `apps/api/src/modules/prazo-defesa/`, `apps/web/src/features/prazo-defesa/` |
| Notificações do escritório (sino na barra lateral, aviso no canto) | mono-repo | `apps/api/src/modules/notificacoes/`, `apps/web/src/features/prazo-defesa/components/{AvisoDefesaCanto,CentralNotificacoes}.tsx` |
| **Lety Defesas**: biblioteca do cliente (temas, teses versionadas, trilhas, instruções, OAB, marcos), tela `/lety-defesas` | mono-repo | `apps/api/src/modules/defesas/` e `apps/web/src/features/defesas/pages/LetyDefesasPage.tsx` |
| Lety Defesas no processo: aba Defesa (diagnóstico, peça editável, conferência, DOCX/PDF) | mono-repo | `apps/web/src/features/defesas/components/PainelDefesa.tsx`, `packages/shared-schemas/src/defesa.ts` (`auditarDefesa`, `blocosDefesa`) |
| Lety Defesas: **formatação do modelo .docx** por tese (XML dos parágrafos, notas, prints) e montagem do DOCX; carga das teses antigas | mono-repo | `apps/api/src/modules/defesas/infrastructure/{formato-modelo,docx-defesa}.ts`, `apps/api/src/cli/formatos-teses.ts` |
| Lety Defesas: diagnóstico e redação por tópico (prompts, schema, roteiro, prescrição/marcos) | lety | `src/lety/defesa.py`, `schema_defesa.py`, `prompts/defesa_*.md` |
| Texto de PDF sem marca d'água diagonal/carimbo vertical (direção dominante) | datajur | `src/datajur/informador.py` (`texto_pagina`) |
| **Radar de sentença**: baixar a sentença nova do PJe (tipos, limite por verificação) | datajur | `src/datajur/radar.py` (`sentencas_novas`, `baixar_sentenca`) |
| Radar de sentença: o que a IA lê (procedência, cliente, pedidos, causa da derrota) | sentence | `src/sentenca/prompts/*.md`, `schema.py`, `analisar.py` (+ `avaliacao/avaliar.py`) |
| Radar de sentença: fila, julgamento vigente, tela `/sentencas`, planilha | mono-repo | `apps/api/src/modules/sentencas/`, `packages/shared-schemas/src/sentencas.ts`, `apps/web/src/features/sentencas/` |
| **Insights por cliente** (percentual de improcedência, pedidos, vara, juiz, destaques e sugestões) | mono-repo | `packages/shared-schemas/src/sentencas.ts` (`calcularInsights`, `destaques`, `SUGESTAO_*`) e `apps/web/src/features/sentencas/pages/InsightsPage.tsx` |
| Radar: leitura das publicações pela IA (designada, redesignada, link da sala) | lety | `src/lety/audiencias.py` + `prompts/audiencias_publicacao.md` (+ `avaliacao/audiencias/avaliar.py`) |
| **Radar de bloqueio**: regra do risco (grau, fase, motivos, ação), etapa e categoria | radar-bloqueio | `src/radar_bloqueio/risco.py`, `fatos.py` (+ `scripts/regressao_piloto.py`) |
| Radar de bloqueio: o que a IA lê (checklist de fatos), quais documentos baixa | radar-bloqueio | `src/radar_bloqueio/llm.py`, `analise.py` (`selecionar`, `montar_dossie`) |
| Radar de bloqueio: novo tribunal/sistema, login Loy, leitor de um portal | radar-bloqueio | `src/radar_bloqueio/motor.py` (`sistemas_de`), `sessao.py`, `<sistema>.py` |
| Radar de bloqueio: fila, ciclo diário, avisos, tela `/bloqueios`, planilha, cadastro com "radar de bloqueio" | mono-repo | `apps/api/src/modules/bloqueios/`, `packages/shared-schemas/src/bloqueios.ts`, `apps/web/src/features/bloqueios/`, `apps/worker/src/captura_worker/bloqueio.py` |
| Cliente: nomes nos autos, CNPJs, advogados (OAB) do radar de bloqueio | mono-repo | tela "Clientes" no radar de bloqueio (`POST/PUT /clientes`) |
| Captura: quanto insistir quando o tribunal está fora do ar | mono-repo | `apps/api/src/modules/captura/domain/classificar-resultado.ts` (`ESPERAS_LONGAS_MIN`) |
| Ligar orientações para outro cliente | mono-repo | tabela `clientes` (`orientacaoAudiencia = true`, CNPJs em `documentos`) |
| Por cliente: e-mail de subsídios com/sem a lista de documentos específicos da Lety | mono-repo | tabela `clientes` (`subsidiosDocumentosLety`; SABESP = false) |

## Como uma mudança chega à produção

1. **Mudança só no monorepo:** commit + push, depois `deploy/deploy.sh app` (API, site e Lety) ou `deploy/deploy.sh captura`.
2. **Mudança no datajur, na Lety, no agente de sentenças ou no radar de bloqueio:**
   1. commit + push no repositório do pacote;
   2. no monorepo, `cd apps/worker && uv lock --upgrade-package datajur-trabalhista` (ou `lety-trabalhista`, `sentenca-trabalhista`, `radar-bloqueio`) e `uv sync`;
   3. commit + push do `uv.lock` no monorepo;
   4. `deploy/deploy.sh captura` (datajur), `deploy/deploy.sh app` (Lety e agente de sentenças) ou `deploy/deploy.sh bloqueio` (radar de bloqueio).

   O deploy monta as wheels a partir das **pastas locais** irmãs: o checkout local precisa estar no commit publicado.
3. **Mudança na análise da Lety:** antes do commit, rode `uv run python avaliacao/avaliar.py` no repo lety e compare com a tabela do README de lá.

## Produção (AWS, conta 945478724332)

| Peça | Região | Onde | Serviços |
|---|---|---|---|
| EC2 `trabalhista-prod` t4g.medium | us-east-1 | 44.195.135.249 | Nginx, site, API (`trabalhista-api`), Lety (`trabalhista-lety`), **reserva** de captura/radar (`trabalhista-reserva`) |
| RDS `trabalhista-prod` Postgres 16 | us-east-1 | privado (só a EC2) | banco |
| EC2 `trabalhista-captura-br` t4g.small | sa-east-1 | 56.125.207.71 | captura (`trabalhista-captura`), sessões em `/opt/trabalhista/datajur-home`; radar de bloqueio (`trabalhista-bloqueio`, prioridade menor), sessões e cache em `/opt/trabalhista/bloqueio-home` |

- **A captura fica no Brasil** porque TRT1, TRT5 e TRT12 bloqueiam IP estrangeiro (e o DJEN também).
- **Reserva:** se o worker do Brasil parar, o `trabalhista-reserva` (na máquina da API, com sessões
  próprias do PJe) assume o que estiver atrasado há 15 min, em 19 TRTs + TST, sem DJEN. Em operação
  normal ele não pega nada. `deploy/deploy.sh reserva`; env em `deploy/.env.prod/reserva.env`.
- **Autorrecuperação:** watchdog (timer de 2 min) reinicia worker sem pulso há 3 min; EC2 com
  recuperação automática nativa da AWS (falha de hardware: mesmo IP e disco).
- **Endereço:** https://trabalhista.jurify.com.br (HTTPS desde 01/10/2026, Let's Encrypt com renovação automática pelo `certbot.timer`). HTTP e acesso pelo IP redirecionam para o domínio.
- **O worker de captura fala com a API pelo domínio** (`API_URL=https://trabalhista.jurify.com.br/api/v1` em `deploy/.env.prod/captura.env`). A rota `/api/v1/worker/` só aceita o IP da captura (allowlist no Nginx, bloco 443).
- **SSH:** chave `~/.ssh/trabalhista-prod.pem`, liberada no security group só para o IP de quem administra. Mudou de IP? Atualize a regra da porta 22 nos dois security groups.
- **Segredos (nunca no git):** `deploy/.env.prod/` no monorepo (máquina de quem faz deploy) e `.env` de cada pasta local.

## Regras para quem mexe (pessoas e IA)

1. **Terminou uma tarefa, commit + push em todos os repositórios tocados.** Nenhum repo pode ficar com mudança local esquecida. Se o pacote mudou, o `uv.lock` do monorepo também muda.
2. **Nunca versionar** `.env`, chaves, sessões de tribunal, PDFs ou dados reais de partes (os casos de avaliação ficam fora do git).
3. **Mudou um contrato entre repositórios** (JSON do motor, schema da Lety)? Atualize os dois lados no mesmo dia, com o campo novo opcional.
4. **Este mapa e os `CLAUDE.md` de cada repositório** descrevem a verdade atual: atualize junto com a mudança.
