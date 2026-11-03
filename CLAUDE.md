# CLAUDE.md — radar-bloqueio (motor do radar de bloqueio)

> **Antes de tudo, leia [`REPOSITORIOS.md`](./REPOSITORIOS.md):** este é 1 de 5 repositórios. O
> produto (API, tela `/bloqueios`, fila `bloqueios`, worker, deploy) está em
> `~/Documents/mono-repo-trabalhista`.

## O que este repositório faz

Dado um número CNJ de TJ ou TRF e o perfil do cliente réu, abre os autos no sistema certo (PJe,
eproc, e-SAJ, Projudi, TJRJ, TJSE), lê os documentos que pesam no risco e devolve o **risco de
bloqueio judicial** (SEM RISCO, BAIXO, MÉDIO, ALTO, INDETERMINADO; bloqueio já realizado à parte),
a etapa, a categoria do porquê, os motivos e a ação sugerida. Biblioteca do worker
`python -m captura_worker.bloqueio` do monorepo.

## Mapa do código (`src/radar_bloqueio/`)

| Arquivo | Responsabilidade |
|---|---|
| `motor.py` | **Entrada**: `Motor.analisar(cnj, perfil, anterior, hoje)`. Roteamento por tribunal (`sistemas_de`), migração e-SAJ/PJe -> eproc, recurso eproc 2º grau + origem, apenso citado. Trava por tribunal (threads) |
| `perfil.py` | Quem é o cliente (nomes, CNPJs) e os advogados do escritório (nome, OAB). ContextVar da análise em curso |
| `analise.py` | Uma análise: abre os autos, junta o grupo (apensos, migrados), resolve polos, escolhe os documentos (`selecionar`), dossiê, LLM, risco |
| `fatos.py` | Fatos por regra sobre os movimentos (TPU) e sinais no texto dos documentos |
| `llm.py` | Uma chamada (gpt-4.1-mini, JSON Schema estrito) que devolve o checklist de fatos |
| `risco.py` | Decisão determinística: grau, fase, motivos, ação; `etapa` e `categoria` (códigos para o produto) |
| `sessao.py` + `loytrust.py` + `browser.py` | Login pela Loy (Chromium só no login, um por vez) e cookies por sistema |
| `pje_http.py`, `eproc.py`, `esaj.py`, `projudi*.py`, `tjrj.py`, `tjse.py` | Clientes HTTP e leitores de cada sistema |
| `cache.py` | Texto dos documentos em disco, por (sistema, id) |

## Decisões que não podem ser desfeitas sem motivo

- **O LLM lê fatos, o código decide o risco.** Nunca peça grau ao modelo.
- **Nunca classificar sem timeline** (`timeline vazia` levanta erro).
- **O cliente é o polo PASSIVO.** Parsers, regras e prompt assumem isso.
- **Mudou regra em `risco.py`/`fatos.py`?** Rode `scripts/regressao_piloto.py` (precisa do Postgres do
  piloto) e explique cada diferença; os textos dos motivos são lidos por `categoria()`.
- **Navegador só para logar**; o resto é HTTP com ritmo de 0,7–1 s por portal. Não aumente o ritmo.
- Encerrar o Chromium com o fluxo normal (fecha o contexto); nunca SIGKILL no perfil.

## Ao terminar uma mudança aqui

1. `uv run --group dev pytest -q` (e a regressão, se mexeu em regra).
2. Commit + push.
3. No monorepo: `cd apps/worker && uv lock --upgrade-package radar-bloqueio && uv sync`, commit + push.
4. `deploy/deploy.sh bloqueio` no monorepo.

Se o dicionário devolvido mudou, atualize `vigenteDoMotor` (monorepo, `modules/bloqueios`) no mesmo dia.
