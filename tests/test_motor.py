from datetime import date

import pytest

from radar_bloqueio import perfil
from radar_bloqueio.cache import CacheDocs
from radar_bloqueio.fatos import extrair
from radar_bloqueio.motor import eproc_1g, numeros_migracao, sistemas_de, tribunal_de
from radar_bloqueio.risco import categoria, classificar, etapa
from radar_bloqueio.timeline import Capa, Documento, Evento

BMG = perfil.Perfil(nome="BMG", nomes=("BANCO BMG S.A.",), documentos=("61186680000174",),
                    advogados=(perfil.Advogado("RICARDO LOPES GODOY", "77167/MG"),))
SABESP = perfil.Perfil(nome="SABESP", nomes=("COMPANHIA DE SANEAMENTO BASICO DO ESTADO DE SAO PAULO",))


# ------------------------------------------------------------------ roteamento
@pytest.mark.parametrize("numero,esperado", [
    ("5004214-45.2025.8.13.0515", ["pje", "eproc"]),
    ("1000000-00.2024.8.26.0100", ["esaj", "eprocsp"]),
    ("1000000-00.2024.8.26.0000", ["esaj", "eproc2gsp"]),
    ("0800012-57.2021.8.18.0037", ["pje_pi"]),
    ("0000001-00.2024.8.19.0001", ["eproc_rj", "pje_rj", "pjerj_rj"]),
    ("0000001-00.2024.8.19.0000", ["eproc2g_rj"]),
    ("5000001-00.2024.4.02.5001", ["eproc_trf2es"]),
    ("5000001-00.2024.4.03.6100", ["pje_trf3"]),
    ("0000001-00.2024.5.02.0001", []),  # Justiça do Trabalho: sem sistema no radar de bloqueio
])
def test_sistemas_de(numero, esperado):
    assert sistemas_de(numero) == esperado


def test_tribunal_de():
    assert tribunal_de("1000000-00.2024.8.26.0100") == "TJSP"
    assert tribunal_de("5000001-00.2024.4.03.6100") == "TRF3"
    assert tribunal_de("0000001-00.2024.5.02.0001") == "TRT2"
    assert tribunal_de("0000001-00.2024.5.00.0000") == "TST"
    assert tribunal_de("123") is None


def test_migracao():
    assert eproc_1g("esaj") == "eprocsp"
    assert eproc_1g("esaj_al") == "eproc_al"
    r = {"motivos": ["... passará a tramitar no eproc sob o número 4001234-56.2025.8.26.0100 ..."]}
    assert numeros_migracao(r, "1000000-00.2024.8.26.0100") == ["40012345620258260100"]


# ------------------------------------------------------------------ perfil do cliente
def test_eh_cliente_por_nome_e_cnpj():
    assert BMG.eh_cliente("BANCO BMG SA (Réu)")
    assert BMG.eh_cliente("Banco Bmg S.A.")
    assert BMG.eh_cliente("RÉU: 61.186.680/0001-74")
    assert not BMG.eh_cliente("BANCO BRADESCO S.A.")
    assert not BMG.eh_cliente("BMGX PARTICIPACOES")  # palavra inteira
    assert SABESP.eh_cliente("Companhia de Saneamento Básico do Estado de São Paulo - SABESP")


def test_regex_advogados_inclui_oab_com_e_sem_ponto():
    import re
    rx = re.compile(BMG.regex_advogados)
    assert rx.search("oab/mg 77.167") and rx.search("oab 77167") and rx.search("ricardo lopes godoy")


def test_perfil_obrigatorio():
    with pytest.raises(RuntimeError):
        perfil.atual()


# ------------------------------------------------------------------ fatos e risco
def _evento(seq, d, polo, movs, docs=()):
    return Evento(seq=seq, data=d, hora="10:00", polo=polo, movimentos=list(movs),
                  documentos=[Documento(i, t, "", "") for i, t in docs])


CHECKLIST = {
    "ha_sentenca": True, "data_sentenca": "2025-01-10", "resultado_sentenca": "procedente",
    "condenacao_pecuniaria_reu": True, "transito_em_julgado": "transitou", "data_transito": "2025-03-01",
    "acordao_resultado": "nao_houve", "cs_iniciado": False, "data_inicio_cs": None, "intimacao_reu_para_pagar": None,
    "valor_executado": None, "houve_pagamento": "nao", "data_pagamento": None, "valor_pago": None,
    "comprovacao_pagamento": "nao_aplica", "acordo_pos_sentenca_pendente_pagamento": False,
    "autor_posicao": "nao_houve_pagamento", "impugnacao_embargos": "nao_apresentada", "apolice": "nao_apresentada",
    "ordem_pagamento_pendente": False, "prazo_ordem_pagamento": None, "pedido_bloqueio_autor": False,
    "data_pedido_bloqueio": None, "ordem_bloqueio_deferida": False, "bloqueio_realizado": False, "data_bloqueio": None,
    "extinto_ou_arquivado": False, "pendencias_reu": [], "evidencias": [], "resumo": "",
}


def test_juizado_transitado_nao_pago_e_alto():
    capa = Capa(classe="Procedimento do Juizado Especial Cível", polo_passivo=["BANCO BMG S.A. - RICARDO LOPES GODOY"])
    eventos = [_evento(0, date(2025, 3, 1), "SISTEMA", ["Transitado em julgado em 01/03/2025"]),
               _evento(1, date(2025, 1, 10), "INTERNO", ["Julgado procedente o pedido"], [("11", "Sentença")])]
    with perfil.usar(BMG):
        f = extrair(capa, eventos)
        assert f.juizado and f.advogado_habilitado is True
        r = classificar(dict(CHECKLIST), f, date(2025, 6, 1))
    assert r.grau == "ALTO" and etapa(r) == "CUMPRIMENTO" and categoria(r) == "JESP_NAO_PAGO"


def test_justica_comum_sem_cumprimento_e_baixo_e_sobe_sem_advogado_habilitado():
    capa = Capa(classe="Procedimento Comum Cível", polo_passivo=["BANCO BMG S.A."])
    eventos = [_evento(0, date(2025, 3, 1), "SISTEMA", ["Transitado em julgado em 01/03/2025"])]
    with perfil.usar(BMG):
        f = extrair(capa, eventos)
        assert f.advogado_habilitado is False
        r = classificar(dict(CHECKLIST), f, date(2025, 6, 1))
        assert r.grau == "BAIXO" and etapa(r) == "AGUARDA_CUMPRIMENTO" and categoria(r) is None
        # o checklist só vale com prova nos autos: o cumprimento precisa aparecer nos movimentos
        f2 = extrair(capa, [_evento(0, date(2025, 4, 1), "ATIVO", ["Juntada de Petição de cumprimento de sentença"])]
                     + eventos)
        chk = dict(CHECKLIST, cs_iniciado=True, data_inicio_cs="2025-04-01", intimacao_reu_para_pagar="2025-04-05")
        r2 = classificar(chk, f2, date(2025, 6, 1))
    # prazo do 523 vencido -> ALTO; sem advogado do escritório habilitado o motivo aparece
    assert r2.grau == "ALTO"
    assert any("Nenhum advogado do escritório" in m for m in r2.motivos)


def test_sem_advogados_no_perfil_nao_penaliza():
    capa = Capa(classe="Procedimento Comum Cível", polo_passivo=["SABESP"])
    with perfil.usar(SABESP):
        f = extrair(capa, [_evento(0, date(2025, 3, 1), "SISTEMA", ["Transitado em julgado em 01/03/2025"])])
        assert f.advogado_habilitado is None
        r = classificar(dict(CHECKLIST, cs_iniciado=True, data_inicio_cs="2025-04-01"), f, date(2025, 4, 2))
    assert not any("advogado do escritório" in m for m in r.motivos)


def test_bloqueio_realizado():
    with perfil.usar(BMG):
        f = extrair(Capa(classe="Cumprimento de sentença"), [_evento(0, date(2025, 5, 1), "INTERNO", ["Despacho"])])
        r = classificar(dict(CHECKLIST, bloqueio_realizado=True, data_bloqueio="2025-05-01"), f, date(2025, 6, 1))
    assert r.ha_risco == "BLOQUEIO JÁ REALIZADO" and etapa(r) == "BLOQUEIO_EFETIVADO"
    assert categoria(r) == "BLOQUEIO_REALIZADO"


# caso real (TJGO, 5742872-14.2025.8.09.0029): SISBAJUD em 20/10, a ré deposita em 30/10, certidão de
# desbloqueio em 03/11, alvará ao autor e "arquive-se" em 04/11. O motor mostrava "bloqueio em 03/11".
EVENTOS_GO = [
    _evento(0, date(2026, 11, 5), "INTERNO", ["Mov. 132 - Alvará Expedido - Caixa Econômica Federal"],
            [("132", "Alvará Expedido")]),
    _evento(1, date(2026, 11, 4), "INTERNO", ["Mov. 131 - Despacho -> Mero Expediente - Expedir alvará / Arquivar"],
            [("131", "Despacho")]),
    _evento(2, date(2026, 11, 3), "INTERNO",
            ["Mov. 129 - Certidão Expedida - COMPROVANTE DE SISBAJUD COM BUSCAS ATIVAS/DESBLOQ/ EXCLUSÃO DE CACE/CERTIDÃO"],
            [("129", "Certidão")]),
    _evento(3, date(2026, 10, 30), "INTERNO", ["Mov. 127 - Juntada -> Petição"], [("127", "Petição")]),
    _evento(4, date(2026, 10, 20), "INTERNO", ["Mov. 125 - Despacho -> Mero Expediente - Sisbajud/Renajud"],
            [("125", "Despacho -> Mero Expediente - Sisbajud/Renajud")]),
    _evento(5, date(2026, 4, 15), "SISTEMA", ["Transitado em julgado em 15/04/2026"]),
]
CHK_GO = dict(CHECKLIST, cs_iniciado=True, data_inicio_cs="2026-09-23", pedido_bloqueio_autor=True,
              data_pedido_bloqueio="2026-10-20", ordem_bloqueio_deferida=True, bloqueio_realizado=True,
              data_bloqueio="2026-11-03")


def test_desbloqueio_com_alvara_encerra_o_bloqueio_e_a_data_do_desbloqueio_nao_e_a_do_bloqueio():
    capa = Capa(classe="Cumprimento de sentença", polo_passivo=["BANCO BMG S.A."])
    with perfil.usar(BMG):
        f = extrair(capa, EVENTOS_GO)
        assert [d for d, _ in f.liberacoes] == [date(2026, 11, 3)]
        assert date(2026, 11, 3) not in [d for d, _ in f.bloqueios]
        r = classificar(dict(CHK_GO), f, date(2026, 11, 6))
    assert r.grau == "SEM RISCO" and r.conclusao == "BLOQUEIO ENCERRADO" and etapa(r) == "ENCERRADO"
    assert categoria(r) is None
    assert any("03/11/2026 é a data do desbloqueio" in m for m in r.motivos)
    assert any("desbloqueio em 03/11/2026 (alvará em 05/11/2026)" in m for m in r.motivos)


def test_novo_pedido_depois_do_desbloqueio_reabre_e_desbloqueio_sozinho_volta_as_regras_gerais():
    capa = Capa(classe="Cumprimento de sentença", polo_passivo=["BANCO BMG S.A."])
    novo = [_evento(9, date(2026, 11, 10), "INTERNO", ["Penhora on-line determinada"])]
    with perfil.usar(BMG):
        r = classificar(dict(CHK_GO), extrair(capa, novo + EVENTOS_GO), date(2026, 11, 12))
        assert r.ha_risco == "BLOQUEIO JÁ REALIZADO"
        # só o desbloqueio (sem alvará, pagamento ou arquivamento): não é mais "bloqueio realizado"
        so_desbloqueio = EVENTOS_GO[2:]
        r2 = classificar(dict(CHK_GO), extrair(capa, so_desbloqueio), date(2026, 11, 6))
    assert r2.ha_risco != "BLOQUEIO JÁ REALIZADO" and r2.grau == "ALTO"
    assert any("desfeito: desbloqueio total" in m for m in r2.motivos)


def test_desbloqueio_parcial_e_pedido_nao_desfazem_o_bloqueio():
    capa = Capa(classe="Cumprimento de sentença", polo_passivo=["BANCO BMG S.A."])
    base = [_evento(5, date(2026, 5, 2), "INTERNO", ["Bloqueio realizado via SISBAJUD"])]
    with perfil.usar(BMG):
        for mov in ("Transferência 3.413,90 desbloqueio do remanescente", "Juntada de pedido de desbloqueio",
                    "Determino o desbloqueio do valor excedente"):
            f = extrair(capa, [_evento(0, date(2026, 5, 20), "INTERNO", [mov])] + base)
            assert not f.liberacoes, mov
            r = classificar(dict(CHECKLIST, bloqueio_realizado=True, data_bloqueio="2026-05-02"), f, date(2026, 6, 1))
            assert r.ha_risco == "BLOQUEIO JÁ REALIZADO", mov


def test_execucao_extinta_depois_do_bloqueio_encerra():
    capa = Capa(classe="Cumprimento de sentença", polo_passivo=["BANCO BMG S.A."])
    eventos = [_evento(0, date(2026, 6, 1), "INTERNO", ["Extinta a execução ou o cumprimento da sentença"]),
               _evento(1, date(2026, 5, 2), "INTERNO", ["Bloqueio realizado via SISBAJUD"])]
    with perfil.usar(BMG):
        r = classificar(dict(CHECKLIST, bloqueio_realizado=True, data_bloqueio="2026-05-02"),
                        extrair(capa, eventos), date(2026, 6, 10))
    assert r.grau == "SEM RISCO" and r.conclusao == "BLOQUEIO ENCERRADO"


def test_decorrido_prazo_da_re_pelo_perfil():
    with perfil.usar(BMG):
        f = extrair(Capa(), [_evento(0, date(2025, 5, 1), "SISTEMA",
                                     ["Decorrido prazo de BANCO BMG S.A. em 30/04/2025"])])
    assert f.decorridos_reu == [date(2025, 4, 30)]


# ------------------------------------------------------------------ cache
def test_cache_por_sistema(tmp_path):
    c = CacheDocs(tmp_path)
    c.salvar("pje_ba", "123", texto="a\x00b", paginas=2)
    assert c.obter("pje_ba", "123")["texto"] == "ab"
    assert c.obter("eproc_rj", "123") is None  # o mesmo id em outro sistema é outro documento
    assert c.varios("pje_ba", ["123", "9"]).keys() == {"123"}


def test_pagamento_nos_autos_sinaliza_sem_mudar_o_grau():
    from radar_bloqueio.fatos import pagamento_nos_autos
    sentenca = _evento(3, date(2025, 1, 10), "INTERNO", ["Julgado procedente o pedido"], [("11", "Sentença")])
    custas = Evento(seq=2, data=date(2025, 3, 2), hora="", polo="PASSIVO", movimentos=["Juntada de Petição"],
                    documentos=[Documento("20", "Petição", "", ""), Documento("21", "Guia", "custas DARE", "", anexo_de="20")])
    pagto = Evento(seq=1, data=date(2025, 4, 2), hora="", polo="PASSIVO", movimentos=["Juntada de Petição"],
                   documentos=[Documento("30", "Petição", "informa pagamento", ""),
                               Documento("31", "Documento de Comprovação", "comprovante de depósito judicial", "",
                                         anexo_de="30")])
    antigo = Evento(seq=4, data=date(2024, 5, 2), hora="", polo="PASSIVO", movimentos=["Juntada"],
                    documentos=[Documento("40", "Petição", "", ""), Documento("41", "Comprovante", "pagamento", "",
                                                                             anexo_de="40")])
    with perfil.usar(BMG):
        f = extrair(Capa(classe="Procedimento Comum"), [pagto, custas, sentenca, antigo])
        p = pagamento_nos_autos(f)
        assert p and p["doc_id"] == "30" and p["data"] == "2025-04-02" and p["quantidade"] == 1
        assert "comprovante de depósito" in p["descricao"]
        f2 = extrair(Capa(), [custas, sentenca])
        assert pagamento_nos_autos(f2) is None  # custas não pagam a condenação


def test_pagamento_lido_pela_ia_entra_e_peticao_sem_comprovante_nao():
    from radar_bloqueio.fatos import pagamento_nos_autos
    sentenca = _evento(0, date(2025, 1, 10), "INTERNO", ["Julgado procedente o pedido"], [("11", "Sentença")])
    with perfil.usar(BMG):
        f = extrair(Capa(), [sentenca])
        chk = {"houve_pagamento": "integral", "comprovacao_pagamento": "peticao_com_comprovante",
               "data_pagamento": "2025-05-03", "valor_pago": 1234.5,
               "evidencias": [{"campo": "houve_pagamento", "doc_id": "#77", "data": "2025-05-03", "trecho": "x"}]}
        p = pagamento_nos_autos(f, chk)
        assert p == {"data": "2025-05-03", "doc_id": "77", "quantidade": 1,
                     "descricao": "Pagamento integral de R$ 1.234,50 com comprovante nos autos"}
        assert pagamento_nos_autos(f, {**chk, "comprovacao_pagamento": "so_peticao_sem_comprovante"}) is None


def test_pagamento_de_correu_nao_conta():
    from radar_bloqueio.fatos import pagamento_nos_autos
    sentenca = _evento(2, date(2025, 1, 10), "INTERNO", ["Julgado procedente o pedido"], [("11", "Sentença")])
    pagto = Evento(seq=1, data=date(2025, 4, 2), hora="", polo="PASSIVO", movimentos=["Juntada de Petição"],
                   documentos=[Documento("30", "Petição", "informa pagamento", ""),
                               Documento("31", "Comprovante", "comprovante de depósito judicial", "", anexo_de="30")])
    capa = Capa(polo_passivo=["BANCO BMG S.A. (RÉU)", "BANCO DO BRASIL SA - CNPJ 00.000.000/0001-91 (RÉU/RÉ)",
                              "FULANO - OAB MG1 (ADVOGADO)"])
    outro = "BANCO DO BRASIL S.A., já qualificado, vem, por seus advogados, informar o depósito..."
    nosso = "BANCO BMG S.A., já qualificado nos autos, vem informar o pagamento... Ricardo Lopes Godoy OAB/MG 77.167"
    with perfil.usar(BMG):
        f = extrair(capa, [pagto, sentenca])
        assert f.outros_reus == 1
        assert pagamento_nos_autos(f, None, {"30": outro}) is None      # é do corréu
        assert pagamento_nos_autos(f, None, {}) is None                 # sem prova de quem pagou
        assert pagamento_nos_autos(f, None, {"30": nosso})["doc_id"] == "30"
        chk = {"houve_pagamento": "integral", "comprovacao_pagamento": "peticao_com_comprovante",
               "data_pagamento": "2025-04-02", "evidencias": [{"campo": "houve_pagamento", "doc_id": "30"}]}
        assert pagamento_nos_autos(f, chk, {"30": outro}) is None       # a IA leu o pagamento do corréu
        so_nosso = extrair(Capa(polo_passivo=["BANCO BMG S.A. (RÉU)"]), [pagto, sentenca])
        assert so_nosso.outros_reus == 0 and pagamento_nos_autos(so_nosso, None, {})["doc_id"] == "30"
