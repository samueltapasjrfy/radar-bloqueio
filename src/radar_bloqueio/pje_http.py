"""Cliente HTTP do PJe (sem navegador), usando os cookies de uma sessão logada pela Loy.

Fluxo mapeado no PJe TJMG 1º grau (JSF/RichFaces):
  1. GET  peticaoavulsa.seam                      -> ViewState
  2. POST peticaoavulsa.seam (A4J, Pesquisar)     -> linha do processo com idProcesso
  3. POST peticaoavulsa.seam (A4J, clAP)          -> URL dos autos (?id=..&ca=..)
  4. GET  listProcessoCompletoAdvogado.seam       -> timeline completa (movimentos + documentos)
  5. POST listProcessoCompletoAdvogado.seam (A4J) -> "seleciona" o documento na conversa
  6. GET  /seam/resource/rest/pje-legacy/documento/download/<id> -> HTML ou PDF
     (sem o passo 5 o servidor responde 204 + Location para o SSO)

O ritmo é controlado por `min_intervalo` + jitter para não disparar o WAF do tribunal.
"""

from __future__ import annotations

import contextlib
import html as htmlmod
import json
import logging
import random
import re
import threading
import time
import urllib.parse as up
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .cnj import NumeroCNJ

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
PETICIONAR = "Processo/CadastroPeticaoAvulsa/peticaoavulsa.seam"
AUTOS = "Processo/ConsultaProcesso/Detalhe/listProcessoCompletoAdvogado.seam"
DOWNLOAD = "seam/resource/rest/pje-legacy/documento/download/{}"

_RE_VS = re.compile(r'name="javax\.faces\.ViewState" id="javax\.faces\.ViewState" value="([^"]+)"')


class SessaoExpirada(RuntimeError):
    """O PJe mandou para o SSO/login: é preciso logar de novo pela Loy."""


class AcessoTerceiro(SessaoExpirada):
    """PJe pediu aceite de acesso de terceiro (Res. CNJ 121). Herda de SessaoExpirada para o
    chamador renovar o login e tentar de novo; persistindo, o advogado de fato não atua nos autos."""


class ProcessoNaoEncontrado(RuntimeError):
    pass


@dataclass
class Autos:
    numero: str
    id_processo: str
    url: str
    html: str
    view_state: str
    linha_busca: list[str] = field(default_factory=list)
    paginas: int = 1
    sistema: str = "pje"


class _Ritmo:
    """Intervalo mínimo entre requisições, compartilhado entre threads."""

    def __init__(self, min_intervalo: float, jitter: float):
        self.min_intervalo, self.jitter = min_intervalo, jitter
        self._lock = threading.Lock()
        self._prox = 0.0

    def esperar(self) -> None:
        with self._lock:
            agora = time.monotonic()
            alvo = max(agora, self._prox)
            self._prox = alvo + self.min_intervalo + random.uniform(0, self.jitter)
        if alvo > agora:
            time.sleep(alvo - agora)


def carregar_cookies(path: Path) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in json.loads(path.read_text()):
        jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return jar


class ClienteChrome:
    """Mesma interface usada do httpx.Client, mas com a assinatura TLS/HTTP2 do Chrome
    (curl_cffi). Necessário onde há WAF de bot (ex.: Akamai no PJe TJRN)."""

    def __init__(self, cookies: httpx.Cookies, timeout: float, follow_redirects: bool):
        from curl_cffi import requests as cr
        self._erro = cr.errors.RequestsError
        self.s = cr.Session(impersonate="chrome", timeout=timeout,
                            headers={"Accept-Language": "pt-BR,pt;q=0.9"})
        for c in cookies.jar:
            self.s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        self.follow = follow_redirects

    def request(self, metodo, url, headers=None, content=None, data=None, params=None):
        try:
            return self.s.request(metodo, url, headers=headers, data=content if content is not None else data,
                                  params=params, allow_redirects=self.follow)
        except self._erro as e:
            raise httpx.TransportError(str(e)) from e

    def get(self, url, params=None, headers=None):
        return self.request("GET", url, headers=headers, params=params)

    def post(self, url, data=None, headers=None):
        return self.request("POST", url, headers=headers, data=data)

    def close(self) -> None:
        self.s.close()


def cliente_http(cookies: httpx.Cookies, timeout: float, follow_redirects: bool, chrome: bool):
    if chrome:
        return ClienteChrome(cookies, timeout, follow_redirects)
    return httpx.Client(cookies=cookies, timeout=timeout, follow_redirects=follow_redirects,
                        headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})


class PjeHttp:
    """Um cliente por sessão. É seguro para threads, mas cada processo deve usar
    `abrir_autos` + `baixar_documento` em sequência (o ViewState é da conversa)."""

    def __init__(self, base_url: str, cookies: httpx.Cookies,
                 min_intervalo: float = 0.7, jitter: float = 0.6, rotulo: str | None = None,
                 chrome: bool = False):
        self.base = base_url.rstrip("/") + "/"
        self.rotulo = rotulo
        self.cookies = cookies
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        # Um único cliente: o PJe rotaciona cookies (afinidade/conversa) e eles precisam
        # valer para as requisições seguintes.
        self._cl = cliente_http(cookies, 60, False, chrome)

    def _cliente(self) -> "contextlib.nullcontext[httpx.Client]":
        return contextlib.nullcontext(self._cl)

    def close(self) -> None:
        self._cl.close()

    def _req(self, cl: httpx.Client, metodo: str, url: str, form: list | None = None,
             referer: str | None = None) -> httpx.Response:
        headers = {"Referer": referer} if referer else {}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        for tentativa in range(4):
            self.ritmo.esperar()
            self.n_req += 1
            try:
                r = cl.request(metodo, url, headers=headers,
                               content=up.urlencode(form) if form is not None else None)
            except httpx.TransportError as e:
                log.warning("rede (%s) em %s; tentativa %d", e, url[-60:], tentativa + 1)
                time.sleep(2 ** tentativa * 2)
                continue
            if r.status_code in (429, 502, 503, 504):
                log.warning("HTTP %s em %s; recuando", r.status_code, url[-60:])
                time.sleep(2 ** tentativa * 5)
                continue
            if "errorUnexpected" in r.text[:600]:
                raise RuntimeError(f"PJe devolveu erro inesperado em {url[-80:]}")
            # O PJe manda Location p/ o SSO até em respostas 200 com conteúdo; só conta
            # como sessão caída quando não veio corpo (204) ou é redirect de fato.
            loc = r.headers.get("location", "")
            if (r.status_code == 204 or 300 <= r.status_code < 400) and (
                    "sso.cloud.pje" in loc or "login" in loc):
                raise SessaoExpirada(loc[:120])
            return r
        raise RuntimeError(f"falha persistente em {url}")

    def sessao_valida(self) -> bool:
        try:
            r = self._req(self._cl, "GET", self.base + PETICIONAR)
        except SessaoExpirada:
            return False
        return bool(_RE_VS.search(r.text))

    # ------------------------------------------------------------------ busca
    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15) -> Autos:
        url_pet = self.base + PETICIONAR
        with self._cliente() as cl:
            r = self._req(cl, "GET", url_pet)
            m = _RE_VS.search(r.text)
            if not m:
                raise SessaoExpirada("tela de peticionar sem ViewState (sessão caiu?)")
            base = [
                ("AJAXREQUEST", "_viewRoot"),
                ("fPP:numeroProcesso:numeroSequencial", numero.sequencial),
                ("fPP:numeroProcesso:numeroDigitoVerificador", numero.digito),
                ("fPP:numeroProcesso:Ano", numero.ano),
                ("fPP:numeroProcesso:ramoJustica", numero.segmento),
                ("fPP:numeroProcesso:respectivoTribunal", numero.tribunal),
                ("fPP:numeroProcesso:NumeroOrgaoJustica", numero.origem),
                ("fPP", "fPP"), ("autoScroll", ""),
                ("javax.faces.ViewState", m.group(1)),
                ("conversationPropagation", "join"),
            ]
            r = self._req(cl, "POST", url_pet, base + [
                ("fPP:searchProcessosPeticao", "fPP:searchProcessosPeticao"),
                ("AJAX:EVENTS_COUNT", "1")], referer=url_pet)
            m = re.search(r'id="fPP:processosTable:(\d+):clAP"', r.text)
            if not m:
                raise ProcessoNaoEncontrado(numero.formatado)
            id_proc = m.group(1)
            linha = _linha_resultado(r.text, numero.formatado)
            k = f"fPP:processosTable:{id_proc}:clAP"
            r = self._req(cl, "POST", url_pet, base + [
                ("idProcesso", id_proc), (k, k), ("ajaxSingle", k),
                ("AJAX:EVENTS_COUNT", "1")], referer=url_pet)
            m = re.search(r"(listProcessoCompletoAdvogado\.seam\?id=\d+&(?:amp;)?ca=[0-9a-f]+)", r.text)
            ng = re.search(r"autos-digitais/(\d+)\?ca=([0-9a-f]+)", r.text)
            if m:
                url = self.base + "Processo/ConsultaProcesso/Detalhe/" + m.group(1).replace("&amp;", "&")
            elif ng:  # PJe com tela nova (ng2): a mesma chave abre a tela legada dos autos
                url = self.base + AUTOS + f"?id={ng.group(1)}&ca={ng.group(2)}"
            elif re.search(r"Resolu[çc][ãa]o CNJ n?.{0,3}\s*121", r.text):
                # o PJe pede aceite de acesso de terceiro (fica registrado no nome do advogado). Com sessão
                # antiga isso aparece até em processo com o advogado habilitado (PI, 30/10/2026): renova o login antes de desistir
                raise AcessoTerceiro("PJe: advogado não atua nos autos (aceite de acesso de terceiro, Res. CNJ 121)")
            else:
                raise RuntimeError("não veio a URL dos autos no clique 'Abrir'")
            r = self._req(cl, "GET", url, referer=url_pet)
            vs = _RE_VS.search(r.text)
            autos = Autos(numero=numero.formatado, id_processo=id_proc, url=url, html=r.text,
                          view_state=vs.group(1) if vs else "", linha_busca=linha,
                          sistema=self.rotulo or ("pje2g" if "recursal" in self.base or "pje2g" in self.base else "pje"))
            self._carregar_paginas(autos, max_paginas)
            return autos

    def _carregar_paginas(self, autos: Autos, max_paginas: int) -> None:
        """A timeline vem paginada (rolagem infinita). Busca as páginas seguintes e
        anexa os fragmentos em autos.html, dentro de um <form id="divTimeLine">."""
        m = re.search(r'id="totalPaginas" value="(\d+)"', autos.html)
        total = int(m.group(1)) if m else 1
        fn = re.search(r"novaPagina=function\(\)\{A4J\.AJAX\.Submit\('divTimeLine',null,"
                       r"\{'similarityGroupingId':'(divTimeLine:[^']+)'", autos.html)
        if total <= 1 or not fn:
            return
        if total > max_paginas:
            log.warning("%s: timeline com %d páginas; lendo só %d", autos.numero, total, max_paginas)
        k = fn.group(1)
        extras = []
        for _ in range(min(total, max_paginas) - 1):
            r = self._req(self._cl, "POST", self.base + AUTOS, [
                ("AJAXREQUEST", "_viewRoot"), ("divTimeLine", "divTimeLine"),
                ("divTimeLine:txtPesquisa", ""), ("javax.faces.ViewState", autos.view_state),
                (k, k), ("ajaxSingle", k), ("AJAX:EVENTS_COUNT", "1")], referer=autos.url)
            ini = r.text.find('id="divTimeLine:eventosTimeLineElement"')
            extras.append(r.text[ini:] if ini >= 0 else r.text)
        # Cada resposta re-renderiza a timeline acumulada (página 1..N): basta a última.
        autos.html += '<form id="divTimeLine" class="acumulada">' + extras[-1] + "</form>"
        autos.paginas = min(total, max_paginas)

    # -------------------------------------------------------------- documentos
    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str) -> tuple[str, bytes, str]:
        """Seleciona o documento na timeline e baixa. Devolve (content-type, bytes, juntado_por)."""
        url_autos = self.base + AUTOS
        with self._cliente() as cl:
            # recuperarDocSelecionado(id) responde errorUnexpected fora do navegador; o
            # clique no link da timeline (já com todas as páginas carregadas) funciona.
            alvo = [(link_id, link_id), ("ajaxSingle", link_id)]
            form = [("AJAXREQUEST", "_viewRoot"), ("divTimeLine", "divTimeLine"),
                    ("divTimeLine:txtPesquisa", ""), ("javax.faces.ViewState", autos.view_state),
                    *alvo, ("AJAX:EVENTS_COUNT", "1")]
            r = self._req(cl, "POST", url_autos, form, referer=autos.url)
            vs = _RE_VS.search(r.text)
            if vs:
                autos.view_state = vs.group(1)
            m = re.search(r"Juntado por (.+?) em (\d{2}/\d{2}/\d{4} \d{2}:\d{2})", _texto_simples(r.text))
            juntado = f"{m.group(1)} em {m.group(2)}" if m else ""
            # PJe mais novo (ex.: TJBA): /download/<TRIBUNAL>/<grau>/<idProcesso>/<idDocumento>
            novo = re.search(r"seam/resource/rest/pje-legacy/documento/download/[A-Z]+/\w+/\d+/", autos.html)
            caminho = novo.group(0) + doc_id if novo else DOWNLOAD.format(doc_id)
            r = self._req(cl, "GET", self.base + caminho, referer=autos.url)
            if r.status_code == 204 or not r.content:
                raise RuntimeError(f"documento {doc_id} veio vazio (seleção não pegou)")
            return r.headers.get("content-type", ""), r.content, juntado


    # -------------------------------------------------------------- associados
    def associados(self, autos: Autos) -> list[dict]:
        """Processos da aba "Associados" (Dependência, Prevenção, Desmembramento...). A seção
        Dependência — onde fica o cumprimento de sentença apenso — só carrega com o painel
        aberto no servidor (submit do formulário). Devolve [{numero, classe, assunto, partes,
        tipo, papel}] com tipo = seção (Dependência, Prevenção...) e papel = Prevento/Preventor."""
        m = re.search(r"'parameters':\{'associadosTab':'associadosTab','id':(\d+)\}", autos.html)
        if not m:
            return []
        r = self._req(self._cl, "POST", self.base + AUTOS, [
            ("AJAXREQUEST", "_viewRoot"), ("associadosTab", "associadosTab"), ("id", m.group(1)),
            ("javax.faces.ViewState", autos.view_state), ("AJAX:EVENTS_COUNT", "1")], referer=autos.url)
        vs = _RE_VS.search(r.text)
        f = "divProcessosAssociadosDependencia"
        r = self._req(self._cl, "POST", self.base + AUTOS, [
            (f, f), (f + ":_link_hidden_", ""), (f + ":j_idcl", ""),
            ("javax.faces.ViewState", vs.group(1) if vs else autos.view_state),
            (f + ":toggleProcessosAssociadosDependencia", "true")], referer=autos.url)
        vs = _RE_VS.search(r.text)
        if vs:  # a página dos autos foi re-renderizada: o download de documentos segue com ela
            autos.view_state = vs.group(1)
        return parse_associados(r.text)


RE_ASSOC = re.compile(r"(\S+) (\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}) - (.*?) Distribu[ií]do em: \d{2}/\d{2}/\d{4} "
                      r"(Depend[eê]ncia|Preven[cç][aã]o|Desmembramento|Vincula[cç][aã]o Direta)\s*\((\w+)\)")


def parse_associados(html: str) -> list[dict]:
    t = _texto_simples(htmlmod.unescape(re.sub(r"<script.*?</script>", " ", html, flags=re.S)))
    saida, vistos = [], set()
    for m in RE_ASSOC.finditer(t):
        classe, numero, resto, tipo, papel = m.groups()
        if numero in vistos:
            continue
        vistos.add(numero)
        saida.append({"numero": numero, "classe": classe, "descricao": resto.strip(), "tipo": tipo,
                      "papel": papel})
    return saida


def _texto_simples(html: str) -> str:
    t = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", t)


def _linha_resultado(html: str, numero: str) -> list[str]:
    m = re.search(r'<tr class="rich-table-row[^"]*">(.*?)</tr>', html, re.S)
    if not m:
        return []
    cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
    return [_texto_simples(c).strip() for c in cells]
