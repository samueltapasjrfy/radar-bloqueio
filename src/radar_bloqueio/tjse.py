"""Cliente HTTP do Portal do Advogado do TJSE (sistema próprio, anterior ao eproc).

Fluxo mapeado:
  1. POST tjnet/portaladv/consultas/respNumeracaoUnica.wsp (CNJ em partes) -> links respnumprocesso.wsp
     (1º grau + recursos no 2º grau, com número interno de 12 dígitos e token)
  2. GET  respnumprocesso.wsp?...&TMP_QTDE=   -> capa, partes e TODOS os movimentos (com texto)
  3. "Processos Dependentes / Vinculados" (ex.: cumprimento de sentença) entram no grupo
  4. anexos dos movimentos: tjnet/virtual/anexoConsultaSINJUV.wsp e pgrau/consultas/exibirIntegra*.wsp
As páginas do portal exigem Referer de dentro do próprio portal.
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import logging
import re
import time
from datetime import datetime

import httpx

from . import perfil
from .cnj import NumeroCNJ
from .pje_http import Autos, ProcessoNaoEncontrado, SessaoExpirada, _Ritmo, cliente_http
from .timeline import Capa, Documento, Evento, tipo_canonico

log = logging.getLogger(__name__)
HOST = "https://www.tjse.jus.br"
CONSULTAS = HOST + "/tjnet/portaladv/consultas/"


def _limpo(s: str) -> str:
    s = re.sub(r"<span title=\"[^\"]*\"", "<span", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


class TjseHttp:
    def __init__(self, cookies: httpx.Cookies, rotulo: str = "portaladv_se", min_intervalo: float = 1.0,
                 jitter: float = 0.8, max_relacionados: int = 6, chrome: bool = False):
        self.base = HOST + "/tjnet/portaladv/"
        self.rotulo = rotulo
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        self.max_relacionados = max_relacionados
        self._cl = cliente_http(cookies, 90, True, chrome)

    def close(self) -> None:
        self._cl.close()

    def _req(self, metodo: str, url: str, data: dict | None = None, referer: str | None = None) -> httpx.Response:
        if url.startswith("/"):
            url = HOST + url
        headers = {"Referer": referer or self.base + "menu.wsp"}
        for tentativa in range(4):
            self.ritmo.esperar()
            self.n_req += 1
            try:
                r = self._cl.request(metodo, url, data=data, headers=headers)
            except httpx.TransportError as e:
                log.warning("rede (%s); tentativa %d", e, tentativa + 1)
                time.sleep(2 ** tentativa * 2)
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 ** tentativa * 5)
                continue
            if "login" in str(r.url).lower().rsplit("/", 1)[-1]:
                raise SessaoExpirada(str(r.url)[:120])
            return r
        raise RuntimeError(f"falha persistente em {url[:100]}")

    def sessao_valida(self) -> bool:
        try:
            r = self._req("GET", self.base + "main.wsp", referer=self.base + "index2.wsp")
        except SessaoExpirada:
            return False
        return r.status_code == 200 and "Escrit" in r.text and "Advogado" in r.text

    def _detalhe(self, link: str) -> str:
        url = link if link.startswith("http") or link.startswith("/") else CONSULTAS + link
        url = url.replace("&amp;", "&")
        if "TMP_QTDE" not in url:
            url += "&TMP_QTDE="
        r = self._req("GET", url, referer=CONSULTAS + "respNumeracaoUnica.wsp")
        if r.status_code == 403:
            raise SessaoExpirada("portal do TJSE recusou a consulta (403)")
        return r.text

    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15) -> Autos:
        d = numero.limpo
        r = self._req("POST", CONSULTAS + "respNumeracaoUnica.wsp", {
            "tmp.operacao": "", "tmp.sequencial": d[:7], "tmp.digito": d[7:9], "tmp.ano": d[9:13],
            "tmp.orgao": "825", "tmp.origem": d[16:], "TMP_QTDE": "0", "TMP_VOLTAR": ""},
            referer=CONSULTAS + "numProcessoUnico.wsp")
        if r.status_code == 403:
            raise SessaoExpirada("portal do TJSE recusou a consulta (403)")
        links = list(dict.fromkeys(re.findall(r'href="(respnumprocesso\.wsp\?[^"]+)"', r.text)))
        if not links:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (portal TJSE)")
        paginas = [self._detalhe(l) for l in links]
        principal, html = paginas[0], ""
        npro = _campo(principal, "Número") or ""
        vistos = {_campo(p, "Número") for p in paginas}
        for p in paginas[1:]:
            html += _bloco(p)
        # processos dependentes/vinculados (cumprimento de sentença etc.) do processo principal
        dep = re.search(r"Processos Dependentes / Vinculados:(.*?)(?:Recursos no 2|Partes do Processo)", principal, re.S)
        for link in re.findall(r'href="(respnumprocesso\.wsp\?[^"]+)"', dep.group(1) if dep else "")[:self.max_relacionados]:
            p = self._detalhe(link)
            if _campo(p, "Número") in vistos:
                continue
            vistos.add(_campo(p, "Número"))
            html += _bloco(p)
        return Autos(numero=numero.formatado, id_processo=npro, url=CONSULTAS + links[0],
                     html=principal + html, view_state="", sistema=self.rotulo, paginas=len(vistos))

    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str) -> tuple[str, bytes, str]:
        r = self._req("GET", link_id, referer=autos.url)
        if "anexoConsultaSINJUV" in link_id and "html" in r.headers.get("content-type", ""):
            # a página lista os arquivos do movimento; baixa os PDFs (operação DOWNLOAD)
            arquivos = []
            for np_, seq, cod, seqm, dt, ida in re.findall(
                    r"lerDocumento\('([^']*)', '([^']*)', '([^']*)', '([^']*)', '([^']*)', '([^']*)'\)", r.text):
                arquivos.append(f"BLOB:{ida}" if ida else
                                f"/tjnet/virtual/anexoConsultaSINJUV.wsp?tmp_numProcesso={np_}&tmp_sequencia={seq}"
                                f"&tmp_codMovimento={cod}&tmp_seqMovimento={seqm}&tmp_dtMovimento={dt}&tmp_operacao=DOWNLOAD")
            partes = []
            for url in arquivos[:4]:
                if url.startswith("BLOB:"):  # o blobServlet.wsp só faz o POST abaixo via JavaScript
                    rr = self._req("POST", HOST + "/resources/servicos/ConsultaBlobServlet.tjse", {
                        "tmp_idAnexo": url[5:], "tmp_idAnexoSecao": "", "tmp_seqAnexo": "", "tmp_idSistema": "tjnet"},
                        referer=HOST + "/resources/servicos/blobServlet.wsp")
                else:
                    rr = self._req("GET", htmlmod.unescape(url), referer=HOST + link_id)
                if rr.content:
                    partes.append(rr)
            if len(partes) == 1:
                r = partes[0]
            elif partes:  # vários PDFs: concatena como texto extraído depois, um a um
                from .textos import extrair_texto
                texto = "\n\n".join(extrair_texto(x.headers.get("content-type", ""), x.content)[0] for x in partes)
                return "text/plain", texto.encode(), ""
        if not r.content:
            raise RuntimeError(f"documento {doc_id} veio vazio")
        return r.headers.get("content-type", ""), r.content, ""


def _bloco(pagina: str) -> str:
    # "Número Único" é o CNJ; "Número" é a numeração interna do TJSE (ex.: 202590302552)
    numero = _campo(pagina, "Número Único") or _campo(pagina, "Número") or ""
    return (f'<div class="tjse-relacionado" data-numero="{numero}" '
            f'data-classe="{htmlmod.escape(_campo(pagina, "Classe") or "")}">{pagina}</div>')


def _campo(html: str, rotulo: str) -> str | None:
    m = re.search(rf"<b>\s*{rotulo}:\s*</b>\s*<br\s*/?>(.*?)(?:<br|<b>|</td>)", html, re.S)
    return _limpo(m.group(1)) if m else None


# ------------------------------------------------------------------------- parser
def parse_capa(html: str) -> Capa:
    principal = html.split('<div class="tjse-relacionado"')[0]
    capa = Capa()
    capa.classe = _campo(principal, "Classe") or ""
    capa.orgao_julgador = _campo(principal, "Competência") or _campo(principal, "Órgão Julgador") or ""
    capa.valor_causa = _campo(principal, "Valor da Causa") or ""
    capa.autuacao = _campo(principal, "Distribuido Em") or ""
    capa.jurisdicao = _campo(principal, "Fase") or ""
    rel = re.findall(r'class="tjse-relacionado" data-numero="([^"]*)" data-classe="([^"]*)"', html)
    if rel:
        capa.classe += " / " + " / ".join(f"{htmlmod.unescape(c)} ({n})" for n, c in rel)
        capa.associados = len(rel)
    m = re.search(r"Partes do Processo:(.*?)Movimentos do Processo", principal, re.S)
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1) if m else "", re.S):
        cells = [_limpo(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        cells = [c for c in cells if c]
        if len(cells) < 2 or cells[0] in ("Tipo",):
            continue
        linha = " - ".join(cells[1:])
        alvo = capa.polo_passivo if perfil.eh_cliente(cells[1]) else capa.polo_ativo
        if linha not in alvo:
            alvo.append(linha)
    return capa


_RE_DATA = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\s*<br\s*/?>\s*(\d{2}:\d{2})")


def _advogados(html: str) -> tuple[set[str], set[str]]:
    """(advogados do polo ativo, advogados da ré) pela tabela de partes de todas as páginas do grupo."""
    ativo, passivo = set(), set()
    for m in re.finditer(r"Partes do Processo:(.*?)Movimentos do Processo", html, re.S):
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
            cells = [c for c in (_limpo(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)) if c]
            if len(cells) < 3:
                continue
            nomes = {a.strip().upper() for a in re.findall(r"Advogad[oa]:\s*([^-]+?)\s+-", cells[2])}
            (passivo if perfil.eh_cliente(cells[1]) else ativo).update(nomes)
    return ativo - passivo, passivo


def parse_timeline(html: str) -> list[Evento]:
    advs = _advogados(html)
    partes = re.split(r'(<div class="tjse-relacionado" data-numero="[^"]*" data-classe="[^"]*">)', html)
    eventos = _parse_eventos(partes[0], "", advs)
    for i in range(1, len(partes) - 1, 2):
        m = re.search(r'data-numero="([^"]*)" data-classe="([^"]*)"', partes[i])
        eventos += _parse_eventos(partes[i + 1], f"[RELACIONADO {m.group(1)} - {htmlmod.unescape(m.group(2))}] ", advs)
    eventos.sort(key=lambda e: (e.data, e.hora), reverse=True)
    for i, e in enumerate(eventos):
        e.seq = i
    return eventos


def _polo(nome: str, texto: str, advs: tuple[set[str], set[str]] = (set(), set())) -> str:
    """Só juntadas/petições/depósitos têm autor; o portal diz o advogado que gerou o movimento."""
    t = texto.lower()
    if not re.match(r"juntada|peti[çc][ãa]o|protocolo", nome.lower()):
        return "INTERNO"
    up = texto.upper()
    por = re.search(r"REALIZADO POR (.{3,120})", up)
    if any(a and a in up for a in advs[1]) or (por and perfil.eh_cliente(por.group(1))):
        return "PASSIVO"
    if any(a and a in up for a in advs[0]):
        return "ATIVO"
    if perfil.eh_cliente(texto) or re.search(r"requerid[oa]|\br[ée]u\b|executad[oa]|apelante banco", t):
        return "PASSIVO"
    if re.search(r"requerente|\bautor[a]?\b|exequente|parte autora", t):
        return "ATIVO"
    return "INTERNO"


def _parse_eventos(html: str, prefixo: str, advs: tuple[set[str], set[str]] = (set(), set())) -> list[Evento]:
    ini = html.find("Movimentos do Processo")
    if ini < 0:
        return []
    saida = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html[ini:], re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 3:
            continue
        m = _RE_DATA.match(tds[0])
        if not m:
            continue
        quando = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%Y %H:%M")
        nome, desc = _limpo(tds[1]), _limpo(tds[2])
        local = _limpo(tds[3]) if len(tds) > 3 else ""
        docs = []
        for url in re.findall(r"wiOpen\('(/(?:tjnet/virtual/anexoConsultaSINJUV|pgrau/consultas/exibirIntegra\w*)\.wsp\?[^']+)'", tr):
            url = htmlmod.unescape(url)
            doc_id = hashlib.md5(url.encode()).hexdigest()[:12]
            segundo = bool(re.search(r"apela|recurso|agravo|embargos de declara", prefixo.lower())) or "pgrau" in url
            docs.append(Documento(doc_id=doc_id, tipo=tipo_canonico(nome, desc[:200], segundo),
                                  titulo=("Íntegra" if "Integra" in url else nome[:80]),
                                  link_id=url, anexo_de=docs[0].doc_id if docs else ""))
        texto = f"{nome} - {desc}" if desc else nome
        movs = [f"{prefixo}{texto}"[:1500]] + ([f"local: {local}"] if local else [])
        saida.append(Evento(seq=0, data=quando.date(), hora=quando.strftime("%H:%M"), polo=_polo(nome, texto, advs),
                            movimentos=movs, documentos=docs))
    return saida
