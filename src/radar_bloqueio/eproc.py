"""eproc TJMG: cliente HTTP + parser para as mesmas estruturas do PJe (Capa/Evento/Documento).

Fluxo mapeado (eproc 9.22):
  1. GET  painel (URL com hash, guardada no login)  -> action do formPesquisaRapida
  2. POST processo_pesquisa_rapida (txtNumProcessoPesquisaRapida) -> 302 p/ processo_selecionar
     A página traz capa + até 100 eventos com links de documentos (já com hash).
  3. POST processo_selecionar_pagina {pagina: k}     -> demais eventos (#tblEventosNovos)
  4. GET  acessar_documento (href do evento)         -> wrapper com <iframe>
     GET  acessar_documento_implementacao (src)      -> PDF ou HTML do documento
Cada evento traz o polo (data-parte AUTOR/REU/INTERNO) e, quando há prazo, o status,
a data final e a parte intimada — informação que o PJe não dá estruturada.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime

import httpx
from selectolax.lexbor import LexborHTMLParser

from .cnj import NumeroCNJ
from .pje_http import UA, Autos, ProcessoNaoEncontrado, SessaoExpirada, _Ritmo, cliente_http
from .timeline import Capa, Documento, Evento

log = logging.getLogger(__name__)

POLO = {"AUTOR": "ATIVO", "REU": "PASSIVO", "INTERNO": "INTERNO"}


class EprocHttp:
    def __init__(self, base_url: str, cookies: httpx.Cookies, inicio_url: str,
                 min_intervalo: float = 0.7, jitter: float = 0.6, rotulo: str | None = None,
                 chrome: bool = False):
        self.base = base_url.rstrip("/") + "/"
        self.rotulo = rotulo
        self.inicio_url = inicio_url
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        self._acao_pesquisa: str | None = None
        self._cl = cliente_http(cookies, 60, True, chrome)

    def close(self) -> None:
        self._cl.close()

    def _req(self, metodo: str, url: str, data: dict | None = None) -> httpx.Response:
        if not url.startswith("http"):
            url = self.base + url
        for tentativa in range(4):
            self.ritmo.esperar()
            self.n_req += 1
            try:
                r = self._cl.request(metodo, url, data=data)
            except httpx.TransportError as e:
                log.warning("rede (%s); tentativa %d", e, tentativa + 1)
                time.sleep(2 ** tentativa * 2)
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 ** tentativa * 5)
                continue
            final = str(r.url).lower()
            if "sso.cloud" in final or "acao=entrar" in final or "acao=login" in final \
                    or ("txtUsuario" in r.text[:20000] and "pwdSenha" in r.text[:20000]):
                raise SessaoExpirada(final[:120])
            return r
        raise RuntimeError(f"falha persistente em {url}")

    def _acao(self) -> str:
        if not self._acao_pesquisa:
            r = self._req("GET", self.inicio_url)
            m = re.search(r'id="formPesquisaRapida"\s+action="([^"]+)"', r.text)
            if not m:
                raise SessaoExpirada("painel do eproc sem formulário de pesquisa")
            self._acao_pesquisa = m.group(1).replace("&amp;", "&")
        return self._acao_pesquisa

    def sessao_valida(self) -> bool:
        try:
            self._acao_pesquisa = None
            self._acao()
            return True
        except SessaoExpirada:
            return False

    def por_originario(self, numero: str) -> list[str]:
        """Processos do eproc cujo 'Originário/Relacionado' é `numero` (ex.: migrado do e-SAJ
        com número novo que a publicação não informou)."""
        digitos = re.sub(r"\D", "", numero)
        r = self._req("POST", self._acao(), {"txtNumProcessoPesquisaRapida": digitos, "btnPesquisaRapidaSubmit": ""})
        m = re.search(r'data-acaoConsultaAssinada="([^"]+)"\s+value="NP"', r.text)
        if not m:
            return []
        rr = self._req("POST", m.group(1).replace("&amp;", "&"),
                       {"tipoPesquisa": "NP", "numProcessoOriginario": digitos, "chkExibirBaixados": "S"})
        try:
            resultados = rr.json().get("resultados") or []
        except ValueError:
            return []
        achados = [re.sub(r"\D", "", str(x.get("nr_processo") or "")) for x in resultados]
        return [n for n in dict.fromkeys(achados) if len(n) == 20 and n != digitos]

    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15, max_relacionados: int = 6) -> Autos:
        r = self._req("POST", self._acao(), {"txtNumProcessoPesquisaRapida": numero.limpo,
                                              "btnPesquisaRapidaSubmit": ""})
        if "acao=processo_selecionar" not in str(r.url) or "tblEventos" not in r.text:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (eproc)")
        html = r.text
        autos = Autos(numero=numero.formatado, id_processo=numero.limpo, url=str(r.url),
                      html=html, view_state="", sistema=self.rotulo or (
                          ("eproc2gsp" if "eproc2g" in self.base else "eprocsp") if "tjsp" in self.base
                          else "eproc2g" if "eproc2g" in self.base else "eproc"))
        # processos relacionados (ex.: cumprimento de sentença em autos próprios) entram no grupo
        relacionados = [x for x in _relacionados(html)
                        if any(k in x[2].upper() for k in ("CUMPRIMENTO", "EXECU", "LIQUIDA"))]
        for href, num_rel, classe_rel in relacionados[:max_relacionados]:
            try:
                rr = self._req("GET", href)
                if "tblEventos" in rr.text:
                    autos.html += f'<div class="eproc-relacionado" data-numero="{num_rel}" data-classe="{classe_rel}">{rr.text}</div>'
                    autos.paginas += 1
            except ProcessoNaoEncontrado:
                pass
        total = re.search(r"window\.totalPaginas = (\d+)", html)
        url_pag = re.search(r"urlPaginacao = '([^']+)'", html)
        if total and url_pag and int(total.group(1)) > 0:
            extras = []
            for k in range(1, min(int(total.group(1)), max_paginas) + 1):
                rp = self._req("POST", url_pag.group(1).replace("&amp;", "&"), {"pagina": str(k)})
                extras.append(rp.text)
            autos.html += "".join(f'<div class="eproc-pagina">{x}</div>' for x in extras)
            autos.paginas = 1 + len(extras)
        return autos

    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str) -> tuple[str, bytes, str]:
        r = self._req("GET", link_id.replace("&amp;", "&"))
        m = re.search(r'id="conteudoIframe" src="([^"]+)"', r.text)
        # documento nativo do eproc (sentença/decisão/despacho): o texto vem por ajax, não por iframe
        m = m or re.search(r'url:\s*"(controlador\.php\?acao=acessar_documento_implementacao[^"]+)"', r.text)
        if m:
            r = self._req("GET", m.group(1).replace("&amp;", "&"))
        if not r.content:
            raise RuntimeError(f"documento {doc_id} veio vazio")
        return r.headers.get("content-type", ""), r.content, ""


def originario(html: str) -> str | None:
    """Número do processo de origem (1º grau) de um recurso no eproc 2G ('Originário' em #tableRelacionado)."""
    m = re.search(r'<table id="tableRelacionado".*?</table>', html, re.S)
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(0) if m else "", re.S):
        if "origin" in tr.lower():
            n = re.search(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", tr)
            if n:
                return n.group(0)
    return None


def _relacionados(html: str) -> list[tuple[str, str, str]]:
    """[(href, número, classe)] da tabela 'Processos relacionados' (#tableRelacionado)."""
    m = re.search(r'<table id="tableRelacionado".*?</table>', html, re.S)
    if not m:
        return []
    saida = []
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(0), re.S):
        a = re.search(r'href="([^"]+)"[^>]*>([^<]+)</a>', tr)
        tds = [re.sub(r"<[^>]+>|&nbsp;", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        tds = [x for x in tds if x and x != "|"]
        if a:
            saida.append((a.group(1).replace("&amp;", "&"), a.group(2).split("/")[0].strip(),
                          tds[2] if len(tds) > 2 else ""))
    return saida


# ------------------------------------------------------------------------- parser
def _txt(node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ") if node else "").strip()


def parse_capa(html: str) -> Capa:
    t = LexborHTMLParser(html)
    capa = Capa()
    lbl = t.css_first("#lblClasse")
    if lbl is not None and lbl.parent is not None:
        capa.classe = _txt(lbl.parent).replace("Classe da ação:", "").strip()
    capa.orgao_julgador = _txt(t.css_first("#txtOrgaoJulgador"))
    capa.autuacao = _txt(t.css_first("#txtAutuacao"))
    texto = re.sub(r"\s+", " ", t.body.text(separator=" ") if t.body else "")
    m = re.search(r"Competência:? (.{3,80}?) Nova Competência", texto)
    capa.competencia = m.group(1).strip() if m else ""
    m = re.search(r"Valor da Causa:? ?(R\$ ?[\d.,]+)", texto, re.I)
    capa.valor_causa = m.group(1) if m else ""
    capa.jurisdicao = "PROCESSO MIGRADO" if "Processo Migrado" in texto else ""
    rel = re.findall(r'class="eproc-relacionado" data-numero="([^"]*)" data-classe="([^"]*)"', html)
    if rel:
        capa.classe += " / " + " / ".join(f"{c} ({n})" for n, c in rel)
        capa.associados = len(rel)
    tabela = t.css_first("#tblPartesERepresentantes")
    if tabela is not None:
        for lixo in tabela.css("style, script, .sr-only"):
            lixo.decompose()
        for td in tabela.css("td.autorReu"):
            parte = td.css_first(".infraNomeParte")  # <a> ou <span>, conforme a versão do eproc
            tipo = parte.attributes.get("data-parte", "") if parte is not None else ""
            alvo = capa.polo_ativo if tipo == "AUTOR" else capa.polo_passivo if tipo == "REU" else None
            if alvo is not None:
                alvo.append(_txt(td))
    return capa


_RE_TITULO = re.compile(r"^(.*?)\s+\d+\s+(Sem Sigilo|Sigilo|Segredo).*$", re.S)


def parse_timeline(html: str) -> list[Evento]:
    partes = re.split(r'(<div class="eproc-relacionado" data-numero="[^"]*" data-classe="[^"]*">)', html)
    eventos = _parse_eventos(partes[0], "")
    for i in range(1, len(partes) - 1, 2):
        m = re.search(r'data-numero="([^"]*)" data-classe="([^"]*)"', partes[i])
        eventos += _parse_eventos(partes[i + 1], f"[RELACIONADO {m.group(1)} - {m.group(2)}] ")
    eventos.sort(key=lambda e: (e.data, e.hora), reverse=True)
    for i, e in enumerate(eventos):
        e.seq = i
    return eventos


def _parse_eventos(html: str, prefixo: str) -> list[Evento]:
    t = LexborHTMLParser(html)
    vistos: dict[int, Evento] = {}
    for tr in t.css("tr[id^='trEvento']"):
        tds = tr.css("td")
        if len(tds) < 4:
            continue
        num_m = re.match(r"\d+", _txt(tds[0]))
        if not num_m:
            continue
        num = int(num_m.group(0))
        if num in vistos:
            continue
        try:
            quando = datetime.strptime(_txt(tds[1])[:19], "%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
        polo = POLO.get(tr.attributes.get("data-parte", ""), "INTERNO")
        desc = _txt(tds[2])
        movs = [f"{prefixo}Evento {num} - {desc}"]
        usuario = _txt(tr.css_first("span.sr-only")) if tr.css_first("span.sr-only") else ""
        docs: list[Documento] = []
        if len(tds) >= 5:
            for a in tds[4].css("a.infraLinkDocumento"):
                titulo_attr = a.attributes.get("title", "")
                mt = _RE_TITULO.match(titulo_attr)
                tipo = re.sub(r"\s+", " ", mt.group(1) if mt else titulo_attr).strip()
                doc = Documento(doc_id=a.attributes.get("data-doc", ""), tipo=tipo,
                                titulo=_txt(a), link_id=a.attributes.get("href", ""),
                                anexo_de=docs[0].doc_id if docs else "")
                docs.append(doc)
        ev = Evento(seq=0, data=quando.date(), hora=quando.strftime("%H:%M"), polo=polo,
                    movimentos=movs + ([f"por {usuario[:80]}"] if usuario and polo != "INTERNO" else []),
                    documentos=docs)
        vistos[num] = ev
    return [vistos[k] for k in sorted(vistos, reverse=True)]


def prazos_abertos(eventos: list[Evento], hoje: date) -> list[str]:
    saida = []
    for e in eventos:
        for m in e.movimentos:
            p = re.match(r"Prazo (ABERTO|AGUARDANDO\S*)(.*) até (\d{2}/\d{2}/\d{4})", m)
            if p:
                saida.append(f"{m} (evento de {e.data:%d/%m/%Y})")
    return saida
