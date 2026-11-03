"""Processos antigos do TJRJ (DCP, 1ª instância) pela API da consulta processual do Portal de Serviços.

O login (Loy, portal "TJRJ(PJERJ)") deixa um JWT no localStorage de www3.tjrj.jus.br; a sessão
guarda esse token em data/sessao/<perfil>.ls.json e as chamadas usam "Authorization: Bearer".
Para advogado autenticado o portal não pede captcha (recuperar-site-key responde 204).
  1. POST api/processos/por-numeracao-unica  {codigoProcesso: CNJ} -> [{numProcesso, tipoProcesso}]
  2. POST api/processos/por-numero/movimentos {tipoProcesso, codigoProcesso} -> capa, partes,
     advogados, TODOS os movimentos (com texto de atos ordinatórios), guias de depósito e mandados
O 2º grau (e-JUD) exige reCAPTCHA e não é consultado. Não há acesso aos PDFs por aqui.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import httpx

from . import perfil
from .cnj import NumeroCNJ
from .pje_http import UA, Autos, ProcessoNaoEncontrado, SessaoExpirada, _Ritmo
from .timeline import Capa, Evento

log = logging.getLogger(__name__)
API = "https://www3.tjrj.jus.br/consultaprocessual/api/processos/"


class TjrjHttp:
    def __init__(self, cookies: httpx.Cookies, token: str, rotulo: str = "pjerj_rj",
                 min_intervalo: float = 1.0, jitter: float = 0.8):
        if not token:
            raise SessaoExpirada("TJRJ: login sem token do portal")
        self.rotulo = rotulo
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        auth = token if token.startswith("Bearer") else f"Bearer {token}"
        self._cl = httpx.Client(cookies=cookies, timeout=90, headers={
            "User-Agent": UA, "Authorization": auth, "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*"})

    def close(self) -> None:
        self._cl.close()

    def _post(self, caminho: str, corpo: dict) -> httpx.Response:
        for tentativa in range(4):
            self.ritmo.esperar()
            self.n_req += 1
            try:
                r = self._cl.post(API + caminho, json=corpo)
            except httpx.TransportError as e:
                log.warning("rede (%s); tentativa %d", e, tentativa + 1)
                time.sleep(2 ** tentativa * 2)
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 ** tentativa * 5)
                continue
            if r.status_code in (401, 403):
                raise SessaoExpirada(f"TJRJ API {r.status_code}")
            return r
        raise RuntimeError(f"falha persistente em {caminho}")

    def sessao_valida(self) -> bool:
        try:
            r = self._post("por-numeracao-unica", {"codigoProcesso": "0000000-00.0000.8.19.0001"})
        except SessaoExpirada:
            return False
        return r.status_code in (200, 412, 404)

    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15) -> Autos:
        r = self._post("por-numeracao-unica", {"codigoProcesso": numero.formatado})
        lista = r.json() if r.status_code == 200 and r.text.startswith("[") else []
        primeira = [x for x in lista if str(x.get("tipoProcesso")) == "1"]
        if not primeira:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (TJRJ DCP{' - só 2ª instância' if lista else ''})")
        x = primeira[0]
        r = self._post("por-numero/movimentos", {"tipoProcesso": "1", "codigoProcesso": x["numProcesso"],
                                                 "indProcVolumoso": "N", "ultimaOrdemExibida": None})
        if r.status_code != 200 or "movimentosProc" not in r.text:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (TJRJ DCP: sem movimentos)")
        return Autos(numero=numero.formatado, id_processo=x["numProcesso"], url=API + "por-numero/movimentos",
                     html=r.text, view_state="", sistema=self.rotulo)

    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str):
        raise RuntimeError("TJRJ DCP: documentos não disponíveis pela API")


# ------------------------------------------------------------------------- parser
def _data(s: str | None):
    try:
        return datetime.strptime((s or "").strip()[:10], "%d/%m/%Y").date()
    except ValueError:
        return None


def _advogados(d: dict) -> tuple[set[str], set[str]]:
    """(advogados do autor, advogados da ré) pela ordem em personagensProcesso."""
    ativo, passivo, alvo = set(), set(), None
    for p in d.get("personagensProcesso") or []:
        desc, nome = (p.get("descPers") or ""), (p.get("nome") or "")
        if desc == "Advogado" and alvo is not None:
            alvo.add(re.sub(r"^\(\w+\)\s*", "", nome).upper())
        elif desc != "Advogado":
            alvo = passivo if perfil.eh_cliente(nome) else ativo if desc in ("Autor", "Requerente", "Exequente") else None
    return ativo, passivo


def parse_capa(html: str) -> Capa:
    d = json.loads(html)
    capa = Capa(classe=d.get("descRito") or "", orgao_julgador=f"{d.get('descVara') or ''} - {d.get('nome') or ''}",
                jurisdicao=d.get("descrComp") or "", autuacao=d.get("dataDis") or "")
    ativo, passivo = _advogados(d)
    for p in d.get("personagensProcesso") or []:
        if p.get("descPers") == "Advogado":
            continue
        nome = p.get("nome") or ""
        if perfil.eh_cliente(nome):
            capa.polo_passivo.append(f"{nome} ({p.get('descPers')}) - Advogados: {', '.join(sorted(passivo))}")
        elif p.get("descPers") in ("Autor", "Requerente", "Exequente"):
            capa.polo_ativo.append(f"{nome} ({p.get('descPers')})")
    if d.get("segundaInstProc"):
        capa.classe += " / recurso no TJ: " + ", ".join(x.get("codCnj") or "" for x in d["segundaInstProc"])
    return capa


def parse_timeline(html: str) -> list[Evento]:
    d = json.loads(html)
    ativo, passivo = _advogados(d)
    eventos = []
    for m in d.get("movimentosProc") or []:
        exib = m.get("movimentosExibicao") or [{}]
        nome = m.get("descrMov") or exib[0].get("tipoMovimento") or ""
        quando = _data(m.get("dtMovimento") or m.get("dt") or m.get("dtAtualizacao"))
        if not quando:
            continue
        partes = []
        for e in exib:
            for det in e.get("detalhesMovimento") or []:
                cod, desc = (det.get("codigo") or "").strip(), (det.get("descricao") or "").strip()
                if desc and cod not in ("Data:", "Data da juntada:", "Data da digitação:"):
                    partes.append(f"{cod} {desc}".strip())
            for k in ("textosVinculadosProc", "resumoOuIntegra"):
                v = e.get(k)
                if isinstance(v, dict):  # ato do juiz: {"descTipoAto": "Sentença", "descResumida": "..."}
                    partes.append(f"{v.get('descTipoAto') or 'Ato'}: {v.get('descResumida') or v.get('descIntegra') or ''}")
                elif v:
                    partes.append(str(v))
        partes = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", p))[:3000] for p in partes]
        texto = f"{nome} - {' ; '.join(partes)}" if partes else nome
        up = texto.upper()
        polo = ("PASSIVO" if any(a in up for a in passivo) else "ATIVO" if any(a in up for a in ativo)
                else "INTERNO")
        eventos.append(Evento(seq=0, data=quando, hora="00:00", polo=polo, movimentos=[texto[:3000]]))
    for g in d.get("lstGuiaDeposito") or []:
        quando = _data(g.get("dtPagamento"))
        if quando:
            valor = f"R$ {g.get('valorPago'):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") \
                if isinstance(g.get("valorPago"), (int, float)) else ""
            eventos.append(Evento(seq=0, data=quando, hora="00:00", polo="SISTEMA", movimentos=[
                f"Guia de depósito judicial nº {g.get('numGuia')} paga em {g.get('dtPagamento')}, {valor} "
                f"(situação: {g.get('descSituacao')}; depositante não informado pela consulta)"]))
    if d.get("lstMandadoPagamento"):
        ult = eventos[0].data if eventos else None
        lst = ", ".join(f"nº {x.get('codMandado')} ({x.get('descrSitMandado')})" for x in d["lstMandadoPagamento"])
        if ult:
            eventos.append(Evento(seq=0, data=ult, hora="00:00", polo="SISTEMA",
                                  movimentos=[f"Mandados de pagamento (alvarás) no processo: {lst}"]))
    eventos.sort(key=lambda e: (e.data, e.hora), reverse=True)
    for i, e in enumerate(eventos):
        e.seq = i
    return eventos
