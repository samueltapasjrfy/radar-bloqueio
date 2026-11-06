"""Classificação do risco de bloqueio a partir dos fatos (LLM + regras).

Tudo aqui é determinístico e auditável: cada regra que dispara acrescenta um motivo.
Base legal resumida:
- Justiça comum: apelação tem efeito suspensivo (CPC 1.012). Após o trânsito, o CS
  depende de requerimento do credor (CPC 513 §1º) e intimação para pagar em 15 dias
  (CPC 523); sem pagamento: multa 10% + honorários 10% + penhora on-line (CPC 854).
  Impugnação (CPC 525) não suspende a execução sem garantia + efeito suspensivo (§6º).
  Seguro garantia equivale a dinheiro (CPC 835 §2º e 848 p.ú.).
- Juizado (Lei 9.099): recurso inominado em regra sem efeito suspensivo (art. 43).
  Transitada a condenação, o prazo de 15 dias corre independentemente de nova
  intimação (Enunciado 105 FONAJE) e a penhora on-line pode vir direto. Embargos à
  execução exigem juízo garantido (art. 53 e Enunciado 117 FONAJE).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import perfil
from .fatos import Fatos, norm

ORDEM = ["SEM RISCO", "BAIXO", "MÉDIO", "ALTO", "INDETERMINADO"]


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _brl(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _subir(grau: str) -> str:
    """Sobe um nível, no máximo até ALTO (INDETERMINADO não é grau de risco)."""
    if grau not in ("SEM RISCO", "BAIXO", "MÉDIO"):
        return grau
    return ORDEM[ORDEM.index(grau) + 1]


@dataclass
class Risco:
    grau: str                 # SEM RISCO | BAIXO | MÉDIO | ALTO
    ha_risco: str             # vocabulário da planilha da equipe
    fase: str
    conclusao: str
    motivos: list[str] = field(default_factory=list)
    acao_sugerida: str = ""
    checklist: dict = field(default_factory=dict)  # checklist após normalização por regra


def _normalizar(r: dict, f: Fatos) -> dict:
    """Corrige inconsistências típicas do LLM com base em fatos verificados."""
    r = dict(r)
    if r["houve_pagamento"] == "nao" and f.sinal_pagamento_reu:
        d, doc = max(f.sinal_pagamento_reu)
        r["houve_pagamento"] = "parcial"  # conservador: não presume quitação integral
        r["data_pagamento"] = r.get("data_pagamento") or d.isoformat()
        r.setdefault("_ajustes", []).append(f"pagamento detectado por regra na petição #{doc} ({d:%d/%m/%Y})")
    if r["houve_pagamento"] == "nao":
        r["autor_posicao"] = "nao_houve_pagamento"
        r["comprovacao_pagamento"] = "nao_aplica"
    elif r["autor_posicao"] in ("discorda_pede_remanescente_ou_bloqueio", "concorda"):
        # o depósito costuma ser dias antes da petição que o comprova: tolerância de 7 dias
        dp7 = _d(r.get("data_pagamento"))
        dp7 = dp7 - timedelta(days=7) if dp7 else None
        marco = max([x for x in [dp7] + [d for d, _ in f.impug_julgadas] if x], default=None)
        if marco and not any(p.data >= marco for p in f.peticoes_autor):
            r["autor_posicao"] = "nao_se_manifestou"
    # recurso "pendente" mas os autos já voltaram do tribunal depois da última remessa/recurso
    if r["transito_em_julgado"] in ("recurso_pendente", "aguarda_transito_acordao", "aguarda_transito_sentenca") \
            and f.baixas_recurso:
        ult_rec = max((d for d, _ in f.recursos), default=None)
        ult_baixa = max(d for d, _ in f.baixas_recurso)
        if ult_rec is None or ult_baixa >= ult_rec:
            r["transito_em_julgado"] = "transitou"
            r.setdefault("_ajustes", []).append(
                f"recurso tratado como julgado: autos baixados/devolvidos do tribunal em {ult_baixa:%d/%m/%Y}")
    pend = ("recurso_pendente", "aguarda_transito_acordao", "aguarda_transito_sentenca")
    ult_rec = max((d for d, _ in f.recursos), default=None)
    if r["transito_em_julgado"] in pend and r.get("acordao_resultado") in (
            "mantida_sentenca", "reformada_contra_reu", "parcialmente_reformada", "reformada_favor_reu") \
            and not f.baixas_recurso:
        r["transito_em_julgado"] = "transitou"
        r.setdefault("_ajustes", []).append("recurso tratado como julgado: já há acórdão nos autos")
    dcs = _d(r.get("data_inicio_cs"))
    if r["transito_em_julgado"] in pend and r["cs_iniciado"] and dcs and (ult_rec is None or dcs > ult_rec):
        r["transito_em_julgado"] = "transitou"
        r.setdefault("_ajustes", []).append(
            f"cumprimento iniciado em {dcs:%d/%m/%Y}, depois da remessa do recurso: tratado como execução exigível")
    # cumprimento em curso com intimação para pagar (ou acórdão que reformou contra a ré) prova
    # que há condenação vigente, mesmo que a sentença original tenha sido de improcedência
    if not r["condenacao_pecuniaria_reu"] and (
            (r["cs_iniciado"] and (r.get("ordem_pagamento_pendente") or r.get("intimacao_reu_para_pagar")))
            or r.get("acordao_resultado") in ("reformada_contra_reu", "parcialmente_reformada")):
        r["condenacao_pecuniaria_reu"] = True
        r.setdefault("_ajustes", []).append("condenação considerada vigente: há cumprimento com intimação "
                                            "para pagar ou acórdão que reformou contra a ré")
    # cumprimento em autos próprios (incidente/processo de CS): a sentença está no processo principal
    if not r["ha_sentenca"] and r["cs_iniciado"] and re.search(r"cumprimento|execu", norm(f.classe)) \
            and (r.get("intimacao_reu_para_pagar") or r.get("ordem_pagamento_pendente") or r["pedido_bloqueio_autor"]):
        r["ha_sentenca"] = True
        r["condenacao_pecuniaria_reu"] = True
        if r["transito_em_julgado"] == "sem_sentenca":
            r["transito_em_julgado"] = "transitou"
        r.setdefault("_ajustes", []).append("cumprimento em autos próprios: o título está no processo principal")
    dcs0 = _d(r.get("data_inicio_cs"))
    imp_txt = [p.data for p in f.impugnacoes_reu if dcs0 is None or p.data >= dcs0]
    if r["impugnacao_embargos"] == "nao_apresentada" and imp_txt \
            and not any(d >= max(imp_txt) for d, _ in f.impug_julgadas):
        r["impugnacao_embargos"] = "pendente"
        r.setdefault("_ajustes", []).append(
            f"impugnação/embargos da ré protocolada em {max(imp_txt):%d/%m/%Y} (texto da peça) e sem julgamento")
    if r["impugnacao_embargos"] == "nao_apresentada" and f.impugnacao_pendente_mov and r["cs_iniciado"]:
        r["impugnacao_embargos"] = "pendente"
        r.setdefault("_ajustes", []).append(
            f"impugnação/embargos juntada em {f.impugnacao_pendente_mov:%d/%m/%Y} e sem julgamento nos movimentos")
    # data do CS caída no dia da migração PJe->eproc: é o ajuste de classe da migração, não o início
    dcs = _d(r.get("data_inicio_cs"))
    if dcs and dcs in f.classe_cs_migracao and not any(d == dcs for d, _ in f.cs_inicio):
        reais = [d for d, _ in f.cs_inicio if d < dcs]
        r["data_inicio_cs"] = max(reais).isoformat() if reais else None
        r.setdefault("_ajustes", []).append(
            f"classe 'Cumprimento de Sentença' de {dcs:%d/%m/%Y} veio da migração de sistema; "
            "não conta como início do cumprimento")
    # acordo homologado/extinção da execução DEPOIS da ordem de bloqueio: a ordem ficou superada
    if r["ordem_bloqueio_deferida"] and f.extincoes_execucao and f.ordens_bloqueio \
            and max(d for d, _ in f.extincoes_execucao) >= max(d for d, _ in f.ordens_bloqueio):
        r["ordem_bloqueio_deferida"] = False
        r.setdefault("_ajustes", []).append("ordem de bloqueio superada pela sentença que extinguiu a execução")
    # TJGO: "Certidão Expedida - PENHORA ONLINE - CENTRAL SISBAJUD" é o envio da ordem à central, não o
    # resultado; o LLM lê o nome como bloqueio feito. Sem resultado nos autos, é ordem pendente (segue ALTO).
    db = _d(r.get("data_bloqueio"))
    envios = [d for d, m in f.ordens_bloqueio if "central sisbajud" in m.lower()]
    if r["bloqueio_realizado"] and db and db in envios and not any(d >= db for d, _ in f.bloqueios) \
            and not f.desbloqueios:
        r["bloqueio_realizado"] = False
        r["ordem_bloqueio_deferida"] = True
        r.setdefault("_ajustes", []).append(
            f"{db:%d/%m/%Y}: autos enviados à Central SISBAJUD (ordem em cumprimento), sem resultado do bloqueio")
    if not r["bloqueio_realizado"] and f.desbloqueios:
        d0 = min(d for d, _ in f.desbloqueios)
        r["bloqueio_realizado"] = True
        r["data_bloqueio"] = r.get("data_bloqueio") or d0.isoformat()
        r.setdefault("_ajustes", []).append(f"bloqueio comprovado por pedido/decisão de desbloqueio em {d0:%d/%m/%Y}")
    if r["bloqueio_realizado"]:
        _bloqueio_vigente(r, f)
    # CS só com evidência verificada (regra, sinal no texto ou classe processual)
    sent = f.data_ultima_sentenca
    intim_pos = [d for d in f.sinais_intimacao_pagar if sent is None or d > sent]
    if r["cs_iniciado"] and not f.cs_inicio and not intim_pos \
            and not re.search(r"cumprimento|execu", norm(f.classe)):
        r["cs_iniciado"] = False
    if not r["cs_iniciado"] and (f.cs_inicio and (sent is None or max(d for d, _ in f.cs_inicio) > sent)):
        r["cs_iniciado"] = True
        r["data_inicio_cs"] = r.get("data_inicio_cs") or min(d for d, _ in f.cs_inicio
                                                             if sent is None or d > sent).isoformat()
        r.setdefault("_ajustes", []).append(f"cumprimento iniciado (movimento): {f.cs_inicio[0][1][:90]}")
    # D) pedido de penhora DEPOIS do pagamento = o autor não aceitou o pagamento como quitação
    dpb, dpg = _d(r.get("data_pedido_bloqueio")), _d(r.get("data_pagamento"))
    if r["pedido_bloqueio_autor"] and dpb and dpg and dpb > dpg and r["houve_pagamento"] != "nao" \
            and r["autor_posicao"] != "discorda_pede_remanescente_ou_bloqueio":
        r["autor_posicao"] = "discorda_pede_remanescente_ou_bloqueio"
        r.setdefault("_ajustes", []).append(
            f"autor pediu penhora em {dpb:%d/%m/%Y}, depois do pagamento de {dpg:%d/%m/%Y}: não aceitou a quitação")
    # acórdão que reformou a favor da ré afasta a condenação da sentença (salvo cumprimento em
    # curso com ordem de pagar, que prova condenação remanescente, ex.: sucumbência)
    if r["condenacao_pecuniaria_reu"] and r.get("acordao_resultado") == "reformada_favor_reu" and not (
            r["cs_iniciado"] and (r.get("ordem_pagamento_pendente") or r.get("intimacao_reu_para_pagar"))):
        r["condenacao_pecuniaria_reu"] = False
        r.setdefault("_ajustes", []).append("acórdão reformou a sentença a favor da ré: sem condenação vigente")
    # ordem de pagar já atendida por pagamento posterior
    prazo, dp = _d(r.get("prazo_ordem_pagamento")), _d(r.get("data_pagamento"))
    if r.get("ordem_pagamento_pendente") and prazo and dp and dp >= prazo - timedelta(days=30) \
            and r["autor_posicao"] != "discorda_pede_remanescente_ou_bloqueio":
        r["ordem_pagamento_pendente"] = False
    if f.impug_julgadas and r["impugnacao_embargos"] == "pendente":
        ult_imp = max((p.data for p in f.impugnacoes_reu), default=None)
        if ult_imp is None or max(d for d, _ in f.impug_julgadas) >= ult_imp:
            m = norm(f.impug_julgadas[0][1])
            r["impugnacao_embargos"] = ("julgada_favor_reu" if "acolhid" in m and "parte" not in m
                                        else "julgada_parcial" if "parte" in m else "julgada_contra_reu")
    return r


def _bloqueio_vigente(r: dict, f: Fatos) -> None:
    """O bloqueio já feito ainda vale hoje? Depois do último bloqueio, (a) sentença que extinguiu a execução,
    (b) alvará e arquivamento definitivo ou (c) desbloqueio total com alvará, pagamento ou arquivamento
    encerram o bloqueio (r["_bloqueio_encerrado"] = o porquê). Desbloqueio total sem nada disso: o bloqueio
    deixou de existir e o risco segue as regras gerais (a dívida pode continuar). Ordem de bloqueio, intimação
    para pagar, pedido de penhora ou novo cumprimento depois do encerramento reabrem o risco."""
    liberacoes = sorted(d for d, _ in f.liberacoes)
    db = _d(r.get("data_bloqueio"))
    # a data lida pode ser a do desbloqueio (certidão "comprovante de desbloqueio"): o bloqueio foi antes
    if db and db in liberacoes and not any(d == db for d, _ in f.bloqueios):
        antes = [d for d, _ in f.bloqueios + f.ordens_bloqueio if d < db]
        r["data_bloqueio"] = max(antes).isoformat() if antes else None
        r.setdefault("_ajustes", []).append(
            f"{db:%d/%m/%Y} é a data do desbloqueio, não a do bloqueio"
            + (f" (último ato de bloqueio antes dele: {max(antes):%d/%m/%Y})" if antes else ""))
        # sem ato de bloqueio datado antes: o bloqueio foi em algum dia antes do desbloqueio
        db_lido, db = db, _d(r["data_bloqueio"])
        if db is None:
            db_lido -= timedelta(days=1)
    else:
        db_lido = db
    ult = max([d for d, _ in f.bloqueios] + ([db_lido] if db_lido else []), default=None)
    if ult is None:
        return

    def depois(lista):
        return sorted(d for d, *_ in lista if d >= ult)

    ext, arq, alv, lib = depois(f.extincoes_execucao), depois(f.extincoes), depois(f.alvaras), [d for d in liberacoes if d >= ult]
    pago = depois([(p.data,) for p in f.pagamentos_reu])
    autor_pos_arq = arq and any(p.data > arq[-1] for p in f.peticoes_autor)
    porque = []
    if ext:
        porque.append(f"sentença extinguiu a execução em {ext[-1]:%d/%m/%Y}")
    if alv and arq and not autor_pos_arq and not f.atividade_apos_extincao:
        porque.append(f"alvará em {alv[0]:%d/%m/%Y} e arquivamento em {arq[-1]:%d/%m/%Y}")
    if lib and (alv or pago or (arq and not autor_pos_arq and not f.atividade_apos_extincao)):
        extra = ([f"alvará em {alv[-1]:%d/%m/%Y}"] if alv else []) + ([f"pagamento da ré em {pago[-1]:%d/%m/%Y}"] if pago else []) \
            + ([f"arquivamento em {arq[-1]:%d/%m/%Y}"] if arq and not alv else [])
        porque.append(f"desbloqueio em {lib[-1]:%d/%m/%Y} ({', '.join(extra)})")
    fim = max(ext + alv + arq + lib + pago, default=None)
    reabriu = []
    if fim:
        reabriu = [f"ordem de bloqueio em {d:%d/%m/%Y}" for d, _ in f.ordens_bloqueio if d > fim] + \
                  [f"intimação para pagar em {d:%d/%m/%Y}" for d in f.sinais_intimacao_pagar if d > fim] + \
                  [f"cumprimento iniciado em {d:%d/%m/%Y}" for d, _ in f.cs_inicio if d > fim]
        dpb = _d(r.get("data_pedido_bloqueio"))
        if dpb and dpb > fim:
            reabriu.append(f"pedido de penhora em {dpb:%d/%m/%Y}")
    if porque and not reabriu:
        r["_bloqueio_encerrado"] = (f"Houve bloqueio{f' em {db:%d/%m/%Y}' if db else ''}, mas ele já foi resolvido: "
                                    + "; ".join(porque) + ". Não há bloqueio em vigor nem novo pedido depois.")
    elif lib and not reabriu:
        r["bloqueio_realizado"] = False
        if not any(d > ult for d, _ in f.ordens_bloqueio):
            r["ordem_bloqueio_deferida"] = False  # a ordem já foi cumprida (e desfeita)
        r.setdefault("_ajustes", []).append(
            f"bloqueio{f' de {db:%d/%m/%Y}' if db else ''} desfeito: desbloqueio total em {lib[-1]:%d/%m/%Y}; "
            "o risco segue as regras gerais")
    elif reabriu:
        r.setdefault("_ajustes", []).append("bloqueio anterior resolvido, mas a execução continuou: " + "; ".join(list(dict.fromkeys(reabriu))[:3]))


def classificar(r: dict, f: Fatos, hoje: date) -> Risco:
    risco = _classificar(r, f, hoje)
    risco.checklist = _normalizar(r, f)
    return risco


def _classificar(r: dict, f: Fatos, hoje: date) -> Risco:
    r = _normalizar(r, f)
    motivos: list[str] = [f"(ajuste por regra) {a}" for a in r.get("_ajustes", [])]
    if f.migracoes:
        d, m = f.migracoes[0]
        return Risco("INDETERMINADO", "VERIFICAR NO SISTEMA DE DESTINO", "PROCESSO MIGRADO",
                     "PROCESSO MIGRADO", [f"Tramitação migrada para outro sistema em {d:%d/%m/%Y} ({m}); "
                                          "os atos posteriores não estão no PJe."],
                     "Consultar o processo no sistema de destino (ex.: eproc).")
    juizado = f.juizado
    rito = "JESP" if juizado else "PROC COMUM"
    transito = _d(r.get("data_transito"))
    pag = r["houve_pagamento"]
    data_pag = _d(r.get("data_pagamento"))

    peticao_autor_pos = [p for p in f.peticoes_autor
                         if f.extincoes and p.data > max(d for d, _ in f.extincoes)]

    # ------------------------------------------------------------------ fase
    if not r["ha_sentenca"]:
        fase = "PRÉ-SENTENÇA/CONHECIMENTO"
    elif r["transito_em_julgado"] in ("recurso_pendente", "aguarda_transito_acordao",
                                      "aguarda_transito_sentenca"):
        fase = "FASE RECURSAL"
    elif r["extinto_ou_arquivado"] and (pag == "integral" or r["autor_posicao"] == "concorda") \
            and not peticao_autor_pos and not f.atividade_apos_extincao:
        fase = "SEM PENDENCIA/INDICADO ENCERRAMENTO"
    elif juizado:
        fase = "EXECUÇÃO/JESP"
    elif r["cs_iniciado"] or r["impugnacao_embargos"] != "nao_apresentada" or pag != "nao":
        fase = "EXECUÇÃO/ PROC COMUM"
    else:
        fase = "AG INÍCIO CS/PROC COMUM"

    # ----------------------------------------------- bloqueio já realizado
    if r["bloqueio_realizado"] and r.get("_bloqueio_encerrado"):
        return Risco("SEM RISCO", "NÃO - SEM RISCO DE BLOQUEIO", "SEM PENDENCIA/INDICADO ENCERRAMENTO",
                     "BLOQUEIO ENCERRADO", motivos + [r["_bloqueio_encerrado"]],
                     "Nenhuma providência de bloqueio: conferir o arquivamento/levantamento.")
    if r["bloqueio_realizado"]:
        db = _d(r.get("data_bloqueio"))
        motivos.append(f"Bloqueio/penhora on-line já efetivado{f' em {db:%d/%m/%Y}' if db else ''}.")
        if r["autor_posicao"] == "discorda_pede_remanescente_ou_bloqueio" or r["pendencias_reu"]:
            motivos.append("Há pedido de remanescente/pendência posterior: novo bloqueio possível.")
        return Risco("ALTO", "BLOQUEIO JÁ REALIZADO", fase, "JÁ HOUVE BLOQUEIO", motivos,
                     "Conferir valor bloqueado x devido; impugnar excesso (CPC 854 §3º) ou pedir levantamento.")

    # ------------------------------------------- honorários periciais
    # ordem para a ré depositar honorários do perito, sem depósito depois: a penhora vem mesmo sem sentença
    if f.honorarios_perito_pendentes:
        dh, mh = f.honorarios_perito_pendentes
        vencido = hoje > dh + timedelta(days=21)
        grau_h = "ALTO" if vencido and re.search(r"sob pena|penhora|bloqueio", norm(mh)) else "MÉDIO"
        motivos_h = [f"Ordem para a ré depositar honorários periciais em {dh:%d/%m/%Y} ({mh[:140]}) sem depósito "
                     "visível depois: o não pagamento leva a penhora on-line, mesmo antes da sentença."]
    else:
        grau_h, motivos_h = None, []

    # ------------------------------------------------------------ sem título
    if not r["ha_sentenca"]:
        if grau_h:
            return _fechar(grau_h, fase, rito, motivos + motivos_h, f, r,
                           "Depositar e comprovar os honorários periciais.")
        return Risco("SEM RISCO", "NÃO - SEM RISCO DE BLOQUEIO", fase, "FASE INSTRUTÓRIA",
                     ["Sem sentença proferida: não há título executivo."])
    if not r["condenacao_pecuniaria_reu"] and not r["acordo_pos_sentenca_pendente_pagamento"]:
        dpb = _d(r.get("data_pedido_bloqueio"))
        sent = f.data_ultima_sentenca
        executando = (r["pedido_bloqueio_autor"] and (dpb is None or sent is None or dpb > sent)) or \
            (r["cs_iniciado"] and (_d(r.get("intimacao_reu_para_pagar")) or
                                   any(d > (sent or date.min) for d, _ in f.cs_inicio))) or \
            bool(getattr(f, "cs_apenso_ativo", None))
        if grau_h:
            return _fechar(grau_h, fase, rito, motivos + motivos_h, f, r,
                           "Depositar e comprovar os honorários periciais.")
        if executando and not (f.extincoes_execucao and dpb and max(d for d, _ in f.extincoes_execucao) >= dpb
                               and not getattr(f, "cs_apenso_ativo", None)):
            return _fechar("MÉDIO", fase, rito, motivos + [
                f"A decisão lida não condena a ré em dinheiro ({r['resultado_sentenca']}), mas o autor está "
                "executando (cumprimento/pedido de penhora): pode ser honorários, multa, acórdão que reformou ou "
                "execução da outra parte — CONFERIR."], f, r, "Conferir o que está sendo executado e por quem.")
        return Risco("SEM RISCO", "NÃO - SEM RISCO DE BLOQUEIO",
                     "SEM PENDENCIA/INDICADO ENCERRAMENTO", "SEM PENDENCIA/INDICADO ENCERRAMENTO",
                     [f"Decisão vigente sem condenação em dinheiro contra a ré ({r['resultado_sentenca']})."])

    # ------------------------------------- sentença extinguiu a execução
    # "Extinta a execução" / satisfação da obrigação / acordo homologado no cumprimento: o juízo já
    # declarou a obrigação cumprida, mesmo que o pagamento não esteja à vista na timeline
    if f.extincoes_execucao and not r["acordo_pos_sentenca_pendente_pagamento"] and not grau_h:
        dx, mx = max(f.extincoes_execucao)
        pos = [p for p in f.peticoes_autor if p.data > dx]
        if not pos and not f.atividade_apos_extincao:
            return Risco("SEM RISCO", "NÃO - SEM RISCO DE BLOQUEIO",
                         "SEM PENDENCIA/INDICADO ENCERRAMENTO", "SEM PENDENCIA/INDICADO ENCERRAMENTO",
                         motivos + [f"Sentença extinguiu a execução em {dx:%d/%m/%Y} ({mx[:120]}), "
                                    "sem pedido posterior do autor."])

    # --------------------------------------------------- extinto/arquivado
    quitado = pag == "integral" or r["autor_posicao"] == "concorda"
    pendente_pos = r["autor_posicao"] == "discorda_pede_remanescente_ou_bloqueio" or r.get("ordem_pagamento_pendente")
    if r["extinto_ou_arquivado"] and quitado and not pendente_pos and not peticao_autor_pos \
            and not f.atividade_apos_extincao and not grau_h:
        return Risco("SEM RISCO", "NÃO - SEM RISCO DE BLOQUEIO",
                     "SEM PENDENCIA/INDICADO ENCERRAMENTO", "SEM PENDENCIA/INDICADO ENCERRAMENTO",
                     ["Execução extinta/arquivada após pagamento, sem pedido posterior do autor."])
    # arquivado após pagamento registrado como "parcial" (muitas vezes só porque o valor não aparece): sem
    # saldo comprovado nem pedido posterior, não é sem risco, mas também não é alarme
    vp, ve = r.get("valor_pago"), r.get("valor_executado")
    saldo_provado = isinstance(vp, (int, float)) and isinstance(ve, (int, float)) and ve > 0 and vp < 0.95 * ve
    if r["extinto_ou_arquivado"] and pag == "parcial" and not saldo_provado and not pendente_pos \
            and not peticao_autor_pos and not f.atividade_apos_extincao and not grau_h \
            and r["impugnacao_embargos"] not in ("pendente", "julgada_contra_reu", "julgada_parcial"):
        # no Juizado o bloqueio do saldo vem sem nova intimação: fica no meio-termo
        return _fechar("MÉDIO" if juizado else "BAIXO", "SEM PENDENCIA/INDICADO ENCERRAMENTO", rito, motivos + [
            "Processo extinto/arquivado após pagamento da ré (registrado como parcial, sem saldo comprovado) e sem "
            "pedido posterior do autor: confirmar a quitação antes de encerrar"
            + (" — no Juizado, saldo não pago vira bloqueio sem nova intimação." if juizado else ".")],
            f, r, "Confirmar quitação e encerrar.")
    if r["extinto_ou_arquivado"] and peticao_autor_pos:
        motivos.append(f"Processo arquivado, mas o autor peticionou depois "
                       f"({', '.join(f'#{p.doc_id} em {p.data:%d/%m/%Y}' for p in peticao_autor_pos[:3])}).")

    # ------------------------------------------------------------- acordo
    if r["acordo_pos_sentenca_pendente_pagamento"]:
        motivos.append("Acordo homologado com pagamento pendente: atraso permite ao autor executar o "
                       "acordo e pedir penhora (inclusive multa por descumprimento).")
        prazo = _d(r.get("prazo_ordem_pagamento"))
        vencido = prazo is not None and hoje > prazo
        grau = "ALTO" if vencido or r["pedido_bloqueio_autor"] else "MÉDIO"
        if prazo:
            motivos.append(f"Prazo para pagar o acordo: {prazo:%d/%m/%Y} ({'VENCIDO' if vencido else 'em curso'}).")
        return _fechar(grau, fase, rito, motivos, f, r,
                       "Pagar e comprovar o acordo imediatamente (petição + comprovante).")

    # ------------------------------------------------------------ recursal
    if r["transito_em_julgado"] in ("recurso_pendente", "aguarda_transito_acordao",
                                    "aguarda_transito_sentenca"):
        if r["cs_iniciado"] or r["pedido_bloqueio_autor"]:
            motivos.append("Recurso pendente, mas o autor já pediu cumprimento/execução provisória.")
            grau = "ALTO" if juizado else "MÉDIO"
        elif juizado:
            motivos.append("Juizado: recurso inominado não tem efeito suspensivo (art. 43 Lei 9.099); "
                           "execução provisória é possível, mas não foi pedida.")
            grau = "BAIXO"
        else:
            motivos.append("Justiça comum: apelação com efeito suspensivo (CPC 1.012); sem trânsito, "
                           "não há cumprimento definitivo.")
            grau = "BAIXO"
        return _fechar(grau, fase, rito, motivos, f, r, "Acompanhar julgamento do recurso.")

    # ----------------------------------------------- transitado + condenação
    tdesc = f" em {transito:%d/%m/%Y}" if transito else ""
    if pag == "nao":
        if juizado:
            prazo_int = _d(r.get("prazo_ordem_pagamento"))
            if r.get("ordem_pagamento_pendente") and prazo_int and hoje <= prazo_int:
                motivos.append(f"Juizado: condenação transitada{tdesc}; há intimação para pagar com prazo "
                               f"em curso até {prazo_int:%d/%m/%Y}. Pagar e comprovar até lá evita multa e penhora.")
                grau = "MÉDIO"
            elif transito and hoje <= transito + timedelta(days=15):
                venc = transito + timedelta(days=15)
                motivos.append(f"Juizado: condenação transitou{tdesc}; prazo de 15 dias para pagar "
                               f"corre sem nova intimação (Enunciado 105 FONAJE) e vence ~{venc:%d/%m/%Y}.")
                grau = "MÉDIO"
            else:
                motivos.append(f"Juizado: condenação transitada{tdesc} e NÃO paga; o prazo de 15 dias "
                               "corre do trânsito sem nova intimação (Enunciado 105 FONAJE) — já cabe "
                               "multa de 10% e penhora on-line.")
                grau = "ALTO"
        elif r["cs_iniciado"]:
            dcs = _d(r.get("data_inicio_cs"))
            motivos.append(f"Cumprimento de sentença iniciado{f' em {dcs:%d/%m/%Y}' if dcs else ''} "
                           "e sem pagamento da ré.")
            intim = _d(r.get("intimacao_reu_para_pagar"))
            vencido = intim and hoje > intim + timedelta(days=21)  # 15 dias úteis ≈ 21 corridos
            if vencido or r["pedido_bloqueio_autor"] or f.decorridos_reu and dcs and \
                    max(f.decorridos_reu) >= dcs:
                motivos.append("Prazo do art. 523 já decorrido e/ou autor pediu penhora: bloqueio "
                               "pode ser deferido na próxima conclusão.")
                grau = "ALTO"
            else:
                motivos.append("Aguardando intimação/prazo de 15 dias (CPC 523).")
                grau = "MÉDIO"
        else:
            motivos.append(f"Justiça comum: transitou{tdesc}, mas o autor ainda não iniciou o "
                           "cumprimento de sentença (CPC 513 §1º e 523 exigem requerimento + intimação).")
            grau = "BAIXO"
    else:
        valor = f", R$ {_brl(r['valor_pago'])}" if r.get("valor_pago") else ""
        quando = f" em {data_pag:%d/%m/%Y}" if data_pag else ""
        motivos.append(f"A ré pagou ({pag}){quando}{valor}.")
        grau = "BAIXO"
        cp = r["comprovacao_pagamento"]
        if cp == "so_peticao_sem_comprovante":
            motivos.append("Petição de pagamento protocolada SEM o comprovante anexo: o juízo não "
                           "enxerga o pagamento.")
            grau = "ALTO"
        elif cp == "so_comprovante_sem_peticao":
            motivos.append("Comprovante juntado SEM petição explicando o pagamento: risco de não ser "
                           "considerado.")
            grau = "ALTO"
        if pag == "parcial":
            garantido = r["apolice"] != "nao_apresentada"
            if r["impugnacao_embargos"] == "pendente":
                if garantido:
                    motivos.append(f"Pagou o incontroverso; saldo discutido em impugnação/embargos "
                                   f"pendentes e garantido por apólice ({r['apolice']}).")
                    grau = max(grau, "BAIXO", key=ORDEM.index)
                else:
                    motivos.append("Pagou só o incontroverso; impugnação/embargos pendentes SEM apólice "
                                   "para o saldo: execução segue e o remanescente pode ser bloqueado"
                                   + (" (no Juizado, embargos exigem juízo garantido)." if juizado else "."))
                    grau = "ALTO"
            elif r["impugnacao_embargos"] in ("julgada_contra_reu", "julgada_parcial"):
                motivos.append("Impugnação/embargos julgados contra a ré e saldo remanescente não "
                               "quitado.")
                grau = "ALTO"
            elif not garantido:
                motivos.append("Pagamento parcial sem impugnação nem apólice para o restante.")
                grau = "ALTO"
        if r["autor_posicao"] == "discorda_pede_remanescente_ou_bloqueio":
            if r["apolice"] != "nao_apresentada" and r["impugnacao_embargos"] == "pendente":
                motivos.append("Autor discorda e pede remanescente, mas o saldo está garantido.")
                grau = max(grau, "MÉDIO", key=ORDEM.index)
            else:
                motivos.append("Autor discorda do pagamento e pede remanescente/bloqueio.")
                grau = "ALTO"
        elif r["autor_posicao"] == "concorda" and grau == "BAIXO":
            motivos.append("Autor concordou com o pagamento.")
            grau = "SEM RISCO"

    if r["pedido_bloqueio_autor"] and grau in ("SEM RISCO", "BAIXO") and pag == "nao" \
            and (juizado or _intimado(r, f)):
        dp = _d(r.get("data_pedido_bloqueio"))
        motivos.append(f"Autor pediu penhora on-line{f' em {dp:%d/%m/%Y}' if dp else ''}.")
        grau = "ALTO"
    if r.get("ordem_pagamento_pendente") and grau != "BAIXO":
        prazo = _d(r.get("prazo_ordem_pagamento"))
        if prazo and hoje > prazo:
            motivos.append(f"Ordem judicial para a ré pagar, com prazo VENCIDO em {prazo:%d/%m/%Y}.")
            grau = "ALTO"
        else:
            motivos.append("Ordem judicial para a ré pagar, com prazo em curso"
                           f"{f' até {prazo:%d/%m/%Y}' if prazo else ''}.")
            grau = max(grau, "MÉDIO", key=ORDEM.index)
    grau = _atenuantes(grau, r, f, juizado, pag, motivos)
    if grau_h and ORDEM.index(grau_h) > ORDEM.index(grau):
        motivos.extend(motivos_h)
        grau = grau_h
    if r["ordem_bloqueio_deferida"]:
        motivos.append("Há ordem de bloqueio deferida ainda não cumprida.")
        grau = "ALTO"
    return _fechar(grau, fase, rito, motivos, f, r, _acao(r, juizado))


def _intimado(r: dict, f: Fatos) -> bool:
    """Ré já intimada a pagar na fase de cumprimento (art. 523) ou com ordem de pagar."""
    if _d(r.get("intimacao_reu_para_pagar")) or r.get("ordem_pagamento_pendente") or f.ordens_bloqueio:
        return True
    # o LLM às vezes não enxerga a intimação: sinais objetivos na timeline depois do início do cumprimento
    dcs = _d(r.get("data_inicio_cs")) or min((d for d, _ in f.cs_inicio), default=None)
    if dcs is None:
        return bool(f.sinais_intimacao_pagar)
    dpb = _d(r.get("data_pedido_bloqueio"))
    return (any(d > dcs for d in f.decorridos_reu) or any(d >= dcs for d in f.sinais_intimacao_pagar)
            or bool(r["pedido_bloqueio_autor"] and dpb and dpb > dcs + timedelta(days=10)))


def _atenuantes(grau: str, r: dict, f: Fatos, juizado: bool, pag: str, motivos: list[str]) -> str:
    """Situações em que o bloqueio, na prática, não vem agora. Cada uma é um TETO de grau, calibrado
    no histórico de bloqueios da base (backtest de 177 bloqueios efetivados):
    - suspensão do processo (não por acordo): 1/177 bloqueios ocorreu com processo suspenso;
    - justiça comum sem intimação do art. 523: 174/177 bloqueios vieram após intimação para pagar;
    - impugnação pendente: o bloqueio ainda acontece (~6% dos casos), sobretudo SEM garantia;
    - perícia/contadoria: o bloqueio ainda acontece quando o autor já pediu penhora."""
    if grau not in ("ALTO", "MÉDIO") or r["ordem_bloqueio_deferida"]:
        return grau
    teto = None
    # suspensão só vale se o processo não andou depois dela (o fim da suspensão nem sempre vira movimento)
    ds = f.suspensao[0] if f.suspensao else None
    andou = ds and any(d > ds for d in [x for x, _ in f.cs_inicio] + [x for x, _ in f.ordens_bloqueio]
                       + f.decorridos_reu + f.sinais_intimacao_pagar + [x for x, _, _ in f.sentencas]
                       + [p.data for p in f.peticoes_autor])
    if f.suspensao and not andou:
        d, m = f.suspensao
        motivos.append(f"Processo suspenso desde {d:%d/%m/%Y} ({m[:100]}): atos executivos ficam vedados "
                       "enquanto durar a suspensão (CPC 314).")
        teto = "BAIXO"
    elif not juizado and pag == "nao" and not _intimado(r, f):
        motivos.append("Justiça comum: a ré ainda não foi intimada para pagar (art. 523); a penhora só cabe "
                       "depois da intimação e do prazo de 15 dias. Aguarda intimação.")
        teto = "BAIXO"
    elif r["impugnacao_embargos"] == "pendente":
        # garantia "contestada" = o autor pediu penhora DEPOIS da impugnação garantida (pedir o remanescente
        # na resposta à impugnação é o padrão e não indica recusa da garantia)
        d_imp = max([p.data for p in f.impugnacoes_reu] + ([f.impugnacao_pendente_mov] if f.impugnacao_pendente_mov else []),
                    default=None)
        dpb = _d(r.get("data_pedido_bloqueio"))
        contesta = bool(r["pedido_bloqueio_autor"] and dpb and d_imp and dpb > d_imp)
        if r["apolice"] != "nao_apresentada" and not contesta:
            motivos.append(f"Impugnação/embargos pendentes de julgamento com o juízo garantido ({r['apolice']}).")
            teto = "BAIXO"
        elif r["apolice"] != "nao_apresentada":
            # garantia contestada pelo autor (apólice recusada, insuficiente, sem preparo...) não afasta o
            # bloqueio do saldo: fica no meio-termo
            motivos.append(f"Impugnação pendente com garantia ({r['apolice']}), mas o autor contesta/pede penhora: "
                           "se a garantia for recusada, o saldo pode ser bloqueado.")
            teto = "MÉDIO"
        elif not juizado:
            motivos.append("Impugnação pendente de julgamento: o juízo costuma aguardar o julgamento, mas sem "
                           "garantia a impugnação não suspende a execução (CPC 525 §6º) — risco moderado.")
            teto = "MÉDIO"
    ref_sent = f.data_ultima_sentenca
    dcs = _d(r.get("data_inicio_cs"))
    per = [d for d, _ in f.pericias_calculo if (d >= dcs if dcs else ref_sent is None or d > ref_sent)]
    if per and teto is None:
        dp = max(per)
        falha = r["comprovacao_pagamento"] in ("so_peticao_sem_comprovante", "so_comprovante_sem_peticao") \
            or r["autor_posicao"] == "discorda_pede_remanescente_ou_bloqueio"
        if r["pedido_bloqueio_autor"] or falha or juizado:
            motivos.append(f"Perícia/contadoria determinada em {dp:%d/%m/%Y} para apurar o valor, mas o autor já "
                           "pediu penhora: o incontroverso ainda pode ser bloqueado.")
            teto = "MÉDIO"
        else:
            motivos.append(f"Aguarda perícia/contadoria (determinada em {dp:%d/%m/%Y}) para apurar o valor: "
                           "risco reduzido até a homologação do cálculo.")
            teto = "BAIXO"
    return min(grau, teto, key=ORDEM.index) if teto else grau


def _acao(r: dict, juizado: bool) -> str:
    if r["houve_pagamento"] == "nao":
        return ("Pagar o valor devido (ou o incontroverso + apólice para o saldo) e protocolar "
                "petição COM comprovante; se discordar dos cálculos, impugnar com garantia.")
    if r["comprovacao_pagamento"] in ("so_peticao_sem_comprovante", "so_comprovante_sem_peticao"):
        return "Protocolar petição de pagamento acompanhada do comprovante."
    if r["autor_posicao"] == "discorda_pede_remanescente_ou_bloqueio":
        return "Analisar cálculo do remanescente; pagar ou garantir com apólice e impugnar."
    return "Acompanhar."


def _fechar(grau: str, fase: str, rito: str, motivos: list[str], f: Fatos, r: dict,
            acao: str) -> Risco:
    if f.advogado_habilitado is False and r.get("cs_iniciado"):
        motivos.append(f"Nenhum advogado do escritório ({perfil.atual().nomes_advogados}) consta no polo passivo: "
                       "intimações da fase de cumprimento podem não chegar ao escritório.")
        grau = _subir(grau) if grau != "SEM RISCO" else grau
    for p in r.get("pendencias_reu") or []:
        motivos.append(f"Pendência: {p}")
    ha = "SIM - RISCO DE BLOQUEIO" if grau in ("ALTO", "MÉDIO") else "NÃO - SEM RISCO DE BLOQUEIO"
    if fase.startswith("EXECUÇÃO") and r.get("impugnacao_embargos") == "pendente":
        conclusao = "PENDENTE DE JULGAMENTO DE IMPUGNAÇÃO"
    elif fase.startswith("EXECUÇÃO"):
        conclusao = f"FASE DE EXECUÇÃO {'JESP' if rito == 'JESP' else 'J.COMUM'}"
    else:
        conclusao = fase
    return Risco(grau, ha, fase, conclusao, motivos, acao)


# ---------------------------------------------------------------- leitura para o produto
# Códigos estáveis (o produto filtra e conta por eles); o texto livre fica nos motivos.

def etapa(r: Risco) -> str:
    """Etapa da esteira de cumprimento em que o processo está."""
    if r.ha_risco == "BLOQUEIO JÁ REALIZADO":
        return "BLOQUEIO_EFETIVADO"
    f = r.fase or ""
    if f.startswith("PRÉ-SENTENÇA"):
        return "CONHECIMENTO"
    if f.startswith("FASE RECURSAL"):
        return "RECURSAL"
    if f.startswith("AG INÍCIO CS"):
        return "AGUARDA_CUMPRIMENTO"
    if f.startswith("EXECUÇÃO"):
        return "CUMPRIMENTO"
    if f.startswith("SEM PENDENCIA"):
        return "ENCERRADO"
    return "INDETERMINADO"


# (código, teste sobre os motivos), na ordem de prioridade: vale o primeiro que bate
_CATEGORIAS: list[tuple[str, object]] = [
    ("ORDEM_BLOQUEIO", lambda m: any("ordem de bloqueio deferida" in x for x in m)),
    ("JESP_NAO_PAGO", lambda m: any(x.startswith("Juizado: condenação transitada") for x in m)),
    ("PRAZO_523_VENCIDO", lambda m: any("Prazo do art. 523 já decorrido" in x for x in m)),
    ("PAGAMENTO_SEM_COMPROVACAO", lambda m: any("SEM o comprovante" in x or "SEM petição" in x for x in m)),
    ("SALDO_SEM_GARANTIA", lambda m: any("SEM apólice" in x or "sem impugnação nem apólice" in x for x in m)),
    ("IMPUGNACAO_PERDIDA", lambda m: any("julgados contra a ré" in x for x in m)),
    ("ORDEM_PAGAMENTO_VENCIDA", lambda m: any("prazo VENCIDO" in x for x in m)),
    ("ORDEM_PAGAMENTO_EM_CURSO", lambda m: any("prazo em curso" in x for x in m)),
    ("AUTOR_DISCORDA", lambda m: any(x.startswith("Autor discorda do pagamento") for x in m)),
    ("ACORDO_PENDENTE", lambda m: any(x.startswith("Acordo homologado") for x in m)),
    ("EXECUCAO_PROVISORIA", lambda m: any(x.startswith("Recurso pendente, mas") for x in m)),
    ("PETICAO_APOS_ARQUIVAMENTO", lambda m: any(x.startswith("Processo arquivado, mas") for x in m)),
    ("HONORARIOS_PERICIAIS", lambda m: any("honorários periciais" in x for x in m)),
    ("EXECUCAO_SEM_CONDENACAO", lambda m: any("mas o autor está executando" in x for x in m)),
]


def categoria(r: Risco) -> str | None:
    """Por que há risco (só para ALTO/MÉDIO e bloqueio já realizado)."""
    if r.ha_risco == "BLOQUEIO JÁ REALIZADO":
        return "BLOQUEIO_REALIZADO"
    if r.grau not in ("ALTO", "MÉDIO"):
        return None
    for codigo, teste in _CATEGORIAS:
        if teste(r.motivos):
            return codigo
    return "OUTRAS"
