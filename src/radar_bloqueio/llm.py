"""Uma chamada de LLM por processo: lê o dossiê (timeline condensada + trechos de
documentos) e devolve os FATOS do checklist em JSON estrito. O risco é decidido em
código (risco.py) a partir desses fatos — o LLM não classifica risco.
"""

from __future__ import annotations

import json
import logging
import time

from openai import OpenAI

from . import perfil

log = logging.getLogger(__name__)

# USD por 1M tokens (entrada, entrada em cache, saída)
PRECOS = {
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
}

SYSTEM = """Você é advogado(a) sênior de contencioso de massa, analisando processos em que {CLIENTE} é RÉU/EXECUTADO (chamado aqui de "a ré"), para prevenir bloqueio judicial de valores (SISBAJUD/penhora on-line).
Responda SOMENTE com base no dossiê. Datas em AAAA-MM-DD. Quando não houver evidência, use null/"nao" conforme o campo e diga isso na justificativa. Cite sempre os IDs (#id) dos documentos que sustentam cada resposta.

Regras de leitura:
- Polo ATIVO = autor/exequente; PASSIVO = a ré ({CLIENTE}); INTERNO = juízo/secretaria; SISTEMA = movimento automático.
- Sentença: identifique o dispositivo. "condenacao_pecuniaria_reu" = a sentença/acórdão VIGENTE manda a ré pagar quantia (indenização, restituição, honorários, multa). Improcedência total ou extinção sem mérito = false. Acórdão que reforma prevalece sobre a sentença.
- Trânsito em julgado: movimento "Transitado em julgado em", certidão de trânsito, ou menção expressa em despacho/petição. Recurso pendente = há recurso interposto (apelação, recurso inominado, agravo, REsp) ainda não julgado ou cujo acórdão ainda não transitou.
- Cumprimento de sentença (CS) iniciado: petição do autor pedindo cumprimento/execução (art. 523 CPC), classe retificada/evoluída para Cumprimento de Sentença, ou intimação da ré para pagar. Em Juizado, o simples trânsito com condenação já torna a obrigação exigível.
- Pagamento pela ré: petição do polo PASSIVO informando pagamento/depósito judicial, com comprovante/guia anexada (nomes como "comprovante", "guia", "Pasta...Valor...IdCusta..." indicam guia de depósito). Classifique a comprovação:
  * "peticao_com_comprovante": petição + comprovante/guia anexos no mesmo protocolo;
  * "so_peticao_sem_comprovante": petição diz que pagou/vai pagar mas não há comprovante anexo;
  * "so_comprovante_sem_peticao": comprovante/guia juntado sem petição que explique o pagamento.
  Pagamento "parcial" = a ré pagou só o valor que entende devido (incontroverso) e discute o resto.
- Apólice (seguro garantia judicial) pode estar anexada na impugnação/embargos/contestação OU junto da petição de pagamento. Informe onde.
- Bloqueio realizado = ordem SISBAJUD/BACENJUD efetivada (valores bloqueados/transferidos) contra a ré. Pedido do autor ou ordem ainda não cumprida NÃO é bloqueio realizado (registre como pedido).
- pedido_bloqueio_autor = true se QUALQUER petição do autor pede penhora on-line/SISBAJUD/BACENJUD/bloqueio de ativos, inclusive como pedido sucessivo ("na hipótese de não pagamento... penhora on-line") dentro da petição de cumprimento de sentença. data_pedido_bloqueio = data dessa petição.
- ordem_bloqueio_deferida = juiz determinou/deferiu bloqueio ou penhora on-line ainda sem resultado nos autos.
- Autor concorda/discorda: considere apenas manifestações do autor POSTERIORES ao pagamento da ré. Se não houve pagamento, use "nao_houve_pagamento". Pedir cumprimento de sentença ou penhora quando nada foi pago NÃO é "discordar".
- cs_iniciado exige evidência expressa: petição do autor que efetivamente REQUER o cumprimento/execução (com valor ou cálculo), classe alterada para Cumprimento de Sentença, ou intimação da ré para pagar nos termos do art. 523. Dizer que "fará os cálculos para posterior cumprimento" NÃO inicia o CS. "Decorrido prazo" genérico (ex.: para requerer o que entender de direito) NÃO é intimação para pagar.
- ordem_pagamento_pendente = existe decisão/despacho mandando a ré pagar condenação, remanescente, acordo ou honorários, ainda não cumprida (NÃO inclua custas processuais — liste-as só em pendencias_reu); prazo_ordem_pagamento = data-limite estimada (15 dias úteis da publicação, se não houver data expressa).
- Linhas "SINAIS NO TEXTO INTEGRAL" e "FATOS JÁ VERIFICADOS" foram extraídas por regra do documento inteiro: trate-as como verdadeiras (ex.: se uma petição da ré tem sinal "comprovante/depósito", houve pagamento). Movimentos "Acolhida/Rejeitada a impugnação" significam impugnação JULGADA.{ADVOGADOS}"""

ENUM = lambda *v: {"type": "string", "enum": list(v)}  # noqa: E731
DATA = {"type": ["string", "null"], "description": "AAAA-MM-DD"}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ha_sentenca": {"type": "boolean"},
        "data_sentenca": DATA,
        "resultado_sentenca": ENUM("procedente", "procedente_em_parte", "improcedente",
                                   "extinto_sem_merito", "homologacao_acordo", "sem_sentenca", "outro"),
        "condenacao_pecuniaria_reu": {"type": "boolean"},
        "transito_em_julgado": ENUM("transitou", "recurso_pendente", "aguarda_transito_sentenca",
                                    "aguarda_transito_acordao", "sem_sentenca"),
        "data_transito": DATA,
        "acordao_resultado": ENUM("nao_houve", "mantida_sentenca", "reformada_favor_reu",
                                  "reformada_contra_reu", "parcialmente_reformada", "pendente"),
        "cs_iniciado": {"type": "boolean"},
        "data_inicio_cs": DATA,
        "intimacao_reu_para_pagar": DATA,
        "valor_executado": {"type": ["number", "null"]},
        "houve_pagamento": ENUM("integral", "parcial", "nao"),
        "data_pagamento": DATA,
        "valor_pago": {"type": ["number", "null"]},
        "comprovacao_pagamento": ENUM("peticao_com_comprovante", "so_peticao_sem_comprovante",
                                      "so_comprovante_sem_peticao", "nao_aplica"),
        "acordo_pos_sentenca_pendente_pagamento": {"type": "boolean"},
        "autor_posicao": ENUM("concorda", "discorda_pede_remanescente_ou_bloqueio",
                              "nao_se_manifestou", "nao_houve_pagamento"),
        "impugnacao_embargos": ENUM("pendente", "julgada_favor_reu", "julgada_contra_reu",
                                    "julgada_parcial", "nao_apresentada"),
        "apolice": ENUM("na_impugnacao_embargos", "no_pagamento", "na_contestacao_ou_outra",
                        "nao_apresentada"),
        "ordem_pagamento_pendente": {"type": "boolean"},
        "prazo_ordem_pagamento": DATA,
        "pedido_bloqueio_autor": {"type": "boolean"},
        "data_pedido_bloqueio": DATA,
        "ordem_bloqueio_deferida": {"type": "boolean"},
        "bloqueio_realizado": {"type": "boolean"},
        "data_bloqueio": DATA,
        "extinto_ou_arquivado": {"type": "boolean"},
        "pendencias_reu": {"type": "array", "items": {"type": "string"},
                           "description": "determinações/intimações à ré ainda não cumpridas, com data e #id"},
        "evidencias": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"campo": {"type": "string"}, "doc_id": {"type": "string"},
                           "data": DATA, "trecho": {"type": "string"}},
            "required": ["campo", "doc_id", "data", "trecho"]}},
        "resumo": {"type": "string", "description": "3-6 frases: o que aconteceu e onde o processo está"},
    },
}
SCHEMA["required"] = list(SCHEMA["properties"])


def sistema() -> str:
    """Prompt com o cliente e os advogados do escritório da análise em curso."""
    p = perfil.atual()
    nomes = " / ".join(dict.fromkeys([p.nome, *p.nomes]))
    adv = (f"\n- {p.nomes_advogados}: advogado(s) do escritório pela ré, que deve(m) estar habilitado(s) na fase de "
           "cumprimento." if p.advogados else "")
    return SYSTEM.replace("{CLIENTE}", nomes).replace("{ADVOGADOS}", adv)


class AnalisadorLLM:
    def __init__(self, api_key: str, modelo: str):
        self.cli = OpenAI(api_key=api_key, timeout=180, max_retries=6)
        self.modelo = modelo

    def analisar(self, dossie: str) -> tuple[dict, dict]:
        t = time.perf_counter()
        r = self.cli.chat.completions.create(
            model=self.modelo,
            temperature=0,
            messages=[{"role": "system", "content": sistema()},
                      {"role": "user", "content": dossie}],
            response_format={"type": "json_schema", "json_schema": {
                "name": "checklist_bloqueio", "strict": True, "schema": SCHEMA}},
        )
        dados = json.loads(r.choices[0].message.content)
        u = r.usage
        cache = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        pin, pcache, pout = PRECOS.get(self.modelo, PRECOS["gpt-4.1-mini"])
        custo = ((u.prompt_tokens - cache) * pin + cache * pcache + u.completion_tokens * pout) / 1e6
        uso = {"modelo": self.modelo, "tokens_in": u.prompt_tokens, "tokens_cache": cache,
               "tokens_out": u.completion_tokens, "custo_usd": round(custo, 5),
               "segundos": round(time.perf_counter() - t, 1)}
        return dados, uso
