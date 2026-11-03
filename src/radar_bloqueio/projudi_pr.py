"""Cliente HTTP do Projudi do TJPR (também usado pelo TJAM), com cookies de sessão logada pela Loy.

Fluxo mapeado (URLs com parâmetro cifrado _tj, obtidas das próprias páginas):
  1. GET  <página pós-login> (autenticacao.do)          -> link "selectLogin" do perfil Advogado
  2. GET  selectLogin                                    -> menu com buscaProcessosQualquerInstancia.do?_tj=...
  3. POST buscaProcessosQualquerInstancia.do?actionType=pesquisar (numeroProcesso) -> processo.do?_tj=...
  4. GET  processo.do                                    -> movimentações (seq, data, evento, quem movimentou e papel)
  5. POST visualizacaoProcesso.do (selectedIcon=tabPartes) -> partes e advogados por polo
  6. POST movimentacaoArquivoDocumento.do?_tj (tipoLayout=1) -> links arquivo.do?_tj=... (PDF)
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


def _limpo(s: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


class ProjudiPrHttp:
    def __init__(self, inicio_url: str, cookies: httpx.Cookies, rotulo: str = "projudi_pr",
                 min_intervalo: float = 1.0, jitter: float = 0.8, chrome: bool = False):
        self.host = "/".join(inicio_url.split("/")[:3])
        self.inicio = inicio_url
        self.rotulo = rotulo
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        self._busca: str | None = None
        self._cl = cliente_http(cookies, 90, True, chrome)

    def close(self) -> None:
        self._cl.close()

    def _req(self, metodo: str, url: str, data: dict | None = None, headers: dict | None = None) -> httpx.Response:
        if url.startswith("/"):
            url = self.host + url
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
            final = str(r.url).lower()
            host = final.split("/")[2] if "//" in final else ""
            if host.startswith("sso.") or "openid-connect" in final or (
                    "logon.do" in final and "actiontype=inicio" not in final):
                raise SessaoExpirada(final[:120])
            return r
        raise RuntimeError(f"falha persistente em {url[:100]}")

    def _menu(self) -> str:
        """Entra no perfil Advogado e devolve a URL de busca de processos."""
        if self._busca:
            return self._busca
        r = self._req("GET", self.inicio)
        sel = re.search(r'href="(/projudi/autenticacao\.do\?actionType=selectLogin[^"]*)"[^>]*>\s*Advogado', r.text) \
            or re.search(r'href="(/projudi/autenticacao\.do\?actionType=selectLogin[^"]*)"', r.text)
        if sel:
            r = self._req("GET", htmlmod.unescape(sel.group(1)))
        elif "frameset" in r.text.lower():  # TJAM: página em frames; o menu está no frame principal
            r = self._req("GET", "/projudi/usuario/logon.do?actionType=inicio")
        m = re.search(r'href="(/projudi/processo/buscaProcessosQualquerInstancia\.do\?_tj=[0-9a-f]+)"', r.text)
        # TJRR: o menu vem em chamadas add_menu("Processos 1º Grau", "/processo/...") sem o /projudi
        m2 = re.search(r'add_menu\("Processos 1º Grau", "(/processo/buscaProcessosQualquerInstancia\.do\?_tj=[0-9a-f]+)"',
                       r.text)
        if not m and not m2:
            raise SessaoExpirada("Projudi sem menu de busca (sessão caiu?)")
        self._busca = m.group(1) if m else "/projudi" + m2.group(1)
        return self._busca

    def sessao_valida(self) -> bool:
        try:
            self._menu()
            return True
        except SessaoExpirada:
            return False

    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15) -> Autos:
        self._req("GET", self._menu())
        r = self._req("POST", "/projudi/processo/buscaProcessosQualquerInstancia.do?actionType=pesquisar", {
            "instanciaSuperior": "false", "page": "1", "codVara": "", "numeroProcesso": numero.formatado,
            "flagNumeroProcessoUnico": "true", "tipoPesqNome": "F", "tipoPesqNomeMae": "F",
            "tipoPesqNomePai": "F", "idClasseProcessual": ""})
        m = re.search(r'href="(/projudi/processo\.do\?_tj=[0-9a-f]+)"', r.text)
        if not m:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (projudi)")
        r = self._req("GET", m.group(1))
        idp = re.search(r'name="id" value="(\d+)"', r.text)
        if not idp or "mov1Grau" not in r.text and "movimentacoes" not in r.text:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (projudi: sem movimentações)")
        html = r.text
        rp = self._req("POST", f"/projudi/visualizacaoProcesso.do?actionType=visualizar&id={idp.group(1)}",
                       {"id": idp.group(1), "selectedIcon": "tabPartes"})
        html += '<div class="projudi-partes">' + rp.text + "</div>"
        return Autos(numero=numero.formatado, id_processo=idp.group(1), url=self.host + m.group(1),
                     html=html, view_state="", sistema=self.rotulo)

    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str) -> tuple[str, bytes, str]:
        ref = {"Referer": autos.url}  # o TJRR recusa o arquivo ("A sessao expirou") sem o Referer dos autos
        r = self._req("POST", link_id, {"tipoLayout": "1"}, ref)
        arquivos = re.findall(r'href="(/projudi/arquivo\.do\?_tj=[0-9a-f]+)"', r.text)
        partes = []
        for a in list(dict.fromkeys(arquivos))[:3]:
            # o link chama diech9ohTie3() (GET de uma imagem marcadora) antes de baixar
            self._req("GET", "/projudi/img/align_10.png", headers=ref)
            rr = self._req("GET", a, headers=ref)
            if rr.content:
                partes.append(rr)
        if not partes:
            raise RuntimeError(f"documento {doc_id} sem arquivo")
        if len(partes) == 1:
            return partes[0].headers.get("content-type", ""), partes[0].content, ""
        from .textos import extrair_texto
        texto = "\n\n".join(extrair_texto(x.headers.get("content-type", ""), x.content)[0] for x in partes)
        return "text/plain", texto.encode(), ""


# ------------------------------------------------------------------------- parser
def _partes(html: str) -> dict[str, list[tuple[str, list[str]]]]:
    """{'ATIVO'|'PASSIVO': [(nome, [advogados])]} da aba Partes."""
    ini = html.find('<div class="projudi-partes">')
    corpo = html[ini:] if ini >= 0 else ""
    saida: dict[str, list[tuple[str, list[str]]]] = {"ATIVO": [], "PASSIVO": []}
    # cabeçalho do bloco varia: "Polo Ativo/Passivo", "Autor/Réu", "Exequente/Executado"...
    blocos = re.split(r">\s*(Polo Ativo|Polo Passivo|Autor|Réu|Requerente|Requerido|Exequente|Executado|"
                      r"Reclamante|Reclamado|Promovente|Promovido|Recorrente|Recorrido|Terceiro[^<]*)\s*<", corpo)
    for i in range(1, len(blocos) - 1, 2):
        corpo_polo = blocos[i + 1]
        if blocos[i].startswith("Terceiro"):
            continue
        polo = "PASSIVO" if perfil.eh_cliente(_limpo(corpo_polo)) else "ATIVO"
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", corpo_polo, re.S):
            texto = _limpo(tr)
            advs = re.findall(r"OAB \S+ - ([^|]+?)(?=\s+OAB |\s*$)", texto)
            cells = [c for c in (_limpo(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)) if c]
            if advs and cells:
                saida[polo].append((cells[0], [a.strip() for a in advs]))
    return saida


def parse_capa(html: str) -> Capa:
    capa = Capa()
    texto = _limpo(html.split('<div class="projudi-partes">')[0])
    m = re.search(r"Classe Processual:\s*(?:\d+ - )?(.+?)\s+Assunto Principal", texto)
    capa.classe = m.group(1) if m else ""
    m = re.search(r"Status:\s*(.+?)\s+(?:- Precedente|Classe Processual)", texto)
    capa.jurisdicao = m.group(1) if m else ""
    m = re.search(r"(Juizado Especial[^|]{0,80}?|Vara[^|]{0,80}?)\s+(?:Juiz|Compet|Assunto|$)", texto)
    capa.orgao_julgador = m.group(1).strip() if m else ""
    for polo, lista in _partes(html).items():
        for nome, advs in lista:
            (capa.polo_passivo if polo == "PASSIVO" else capa.polo_ativo).append(
                nome + (" - Advogados: " + ", ".join(advs) if advs else ""))
    return capa


def parse_timeline(html: str) -> list[Evento]:
    partes = _partes(html)
    advs = {p: {a.upper() for _, lst in partes[p] for a in lst} for p in partes}
    principal = html.split('<div class="projudi-partes">')[0]
    eventos = []
    for papel_id, tr in re.findall(r'<tr class="(?:odd|even)" id="mov\w*,(\w+),[^"]*"\s*>(.*?)</tr>', principal, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 5:
            continue
        seq = _limpo(tds[1])
        dt = _limpo(tds[2])
        m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", dt)
        m2 = re.match(r"(\d{2}/\d{2}/\d{4}) (\d{2}:\d{2})", dt)
        if m:
            quando = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        elif m2:  # a página alterna o formato conforme a preferência da sessão
            quando = datetime.strptime(f"{m2.group(1)} {m2.group(2)}", "%d/%m/%Y %H:%M")
        else:
            continue
        nome = _limpo(re.search(r"<b>(.*?)</b>", tds[3], re.S).group(1)) if "<b>" in tds[3] else _limpo(tds[3])
        desc = _limpo(tds[3])[len(nome):].strip()
        por = _limpo(re.sub(r"<font.*?</font>", "", tds[4], flags=re.S))
        papel = _limpo(re.search(r"<font[^>]*>(.*?)</font>", tds[4], re.S).group(1)) if "<font" in tds[4] else papel_id
        por_up = por.upper()
        if por_up in advs["PASSIVO"]:
            polo = "PASSIVO"
        elif por_up in advs["ATIVO"]:
            polo = "ATIVO"
        elif papel_id == "ADVOGADO" and "MINIST" not in papel.upper():
            polo = "PASSIVO"  # advogado fora do polo ativo (ex.: substabelecido da ré)
        elif papel_id == "OUTROS":
            polo = "SISTEMA"
        else:
            polo = "INTERNO"
        docs = []
        link = re.search(r'"(/projudi/processo/movimentacaoArquivoDocumento\.do\?_tj=[0-9a-f]+)"', tr)
        if link:
            doc_id = hashlib.md5(link.group(1).encode()).hexdigest()[:12]
            docs.append(Documento(doc_id=doc_id, tipo=tipo_canonico(nome, "", "TURMA" in desc.upper()),
                                  titulo=nome[:80], link_id=link.group(1)))
        movs = [f"Seq. {seq} - {nome}" + (f" - {desc}" if desc else "")]
        if por and polo != "INTERNO":
            movs.append(f"por {por[:80]} ({papel})")
        eventos.append(Evento(seq=0, data=quando.date(), hora=quando.strftime("%H:%M"), polo=polo,
                              movimentos=movs, documentos=docs))
    eventos.sort(key=lambda e: (e.data, e.hora), reverse=True)
    for i, e in enumerate(eventos):
        e.seq = i
    return eventos
