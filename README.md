# radar-bloqueio

Motor do **radar de bloqueio**: diz o risco de bloqueio judicial de valores (SISBAJUD/penhora
on-line) contra o cliente réu num processo de qualquer **TJ ou TRF**, e por quê, com os IDs dos
documentos que provam. Nasceu do piloto `bmg-block-service` (BANCO BMG, validado com a equipe
jurídica) e foi generalizado para qualquer cliente. É usado como **biblioteca** pelo worker
`python -m captura_worker.bloqueio` do monorepo (`mono-repo-trabalhista`), que grava o resultado
pela API. Leia [`REPOSITORIOS.md`](./REPOSITORIOS.md): este é 1 de 5 repositórios.

## Como o risco nasce (regra geral)

| Situação | Justiça comum (CPC) | Juizado (Lei 9.099) |
|---|---|---|
| Sem sentença | sem título, sem risco | idem |
| Sentença sem condenação em dinheiro (improcedente/extinção) | sem risco | idem |
| Recurso pendente | apelação tem efeito suspensivo (art. 1.012): **baixo** | recurso inominado em regra sem efeito suspensivo (art. 43): **baixo**, ou **alto** se o autor pedir execução |
| Transitou, não pagou, autor **não** pediu CS | **baixo**: CS depende de requerimento + intimação (arts. 513 §1º, 523) | **alto**: os 15 dias correm do trânsito (Enunciado 105 FONAJE) |
| CS iniciado, prazo de 15 dias correndo | **médio** | — |
| CS iniciado, prazo vencido ou autor pediu penhora | **alto** | **alto** |
| Pagou, mas só a petição **ou** só o comprovante | **alto**: o juízo não "vê" o pagamento | idem |
| Pagou o incontroverso + impugnação pendente **com apólice** do saldo | **baixo** (seguro garantia = dinheiro, arts. 835 §2º e 848) | idem |
| Pagou o incontroverso **sem apólice** | **alto** | **alto** |
| Autor concorda, alvará expedido, execução extinta | sem risco | idem |
| Advogado do escritório fora do polo passivo na fase de CS | sobe um grau (intimações podem não chegar) | idem |

As regras estão em `risco.py` e cada uma que dispara grava um motivo legível. O LLM não classifica
risco: lê o dossiê e devolve FATOS (checklist em JSON estrito); a decisão é código.

## Pagamento nos autos (sinal, não muda o grau)

`resultado["pagamento_nos_autos"]` = o comprovante mais recente da ré depois da última sentença
(`{data, doc_id, descricao, quantidade}` ou `None`): petição com anexo de comprovante/guia de depósito,
comprovante achado no texto da peça ou pagamento com comprovante lido pela IA. Custas e taxas não contam.
Com **corréu** no polo passivo, só vale o que se prova do cliente: assinatura de advogado do escritório,
a peça aberta pelo cliente ("BANCO X ..., vem") ou o movimento que cita o cliente (`fatos.pagamento_nos_autos`).

## Uso

```python
from radar_bloqueio import Advogado, Motor, Perfil, load_config

motor = Motor(load_config())          # BLOQUEIO_HOME, LOY_*, OPENAI_API_KEY no ambiente
cliente = Perfil(nome="BMG", nomes=("BANCO BMG S.A.",), documentos=("61186680000174",),
                 advogados=(Advogado("RICARDO LOPES GODOY", "77167/MG"),))
r = motor.analisar("5004214-45.2025.8.13.0515", cliente, anterior=None)
r["grau_risco"], r["etapa"], r["categoria"], r["motivos"], r["acao_sugerida"]
```

`anterior = {"assinatura", "checklist"}` da última análise: timeline igual reaproveita a leitura
da IA (custo zero) e só recalcula o risco (os prazos dependem da data).

```sh
uv sync
uv run playwright install chromium
uv run python -m radar_bloqueio.setup_extension          # extensão Loy em $BLOQUEIO_HOME/extensions
uv run --extra cli python -m radar_bloqueio <cnj> --cliente BMG --nome "BANCO BMG" \
    --advogado "RICARDO LOPES GODOY:77167/MG" [--json saida.json]
uv run --group dev pytest -q
```

## Validação

- **Regressão contra o piloto** (`scripts/regressao_piloto.py`): reclassifica as 3.112 análises
  gravadas no Postgres do piloto com as regras do piloto e as deste pacote, sobre o mesmo checklist:
  **3.112/3.112 iguais** (grau, situação, fase e nº de motivos) em 03/11/2026.
- Piloto (equipe jurídica): MG 12/16 iguais, SP 15/17; as divergências são o robô mais conservador.
- Custo: ~US$ 0,004 por processo (gpt-4.1-mini, ~9 mil tokens de entrada); ~20–30 s por processo.

## Limitações

- Cliente sempre no **polo passivo** (réu/executado). Cliente autor exige outro conjunto de regras.
- Justiça do Trabalho ainda não (a execução trabalhista segue a CLT, art. 880 e seguintes).
- Projudi BA só abre processos das listagens do advogado do certificado; 2º grau do TJRJ e TJMS
  e-SAJ têm captcha.
- PDF escaneado sem texto não tem OCR (o LLM vê o tipo e o nome do arquivo).
