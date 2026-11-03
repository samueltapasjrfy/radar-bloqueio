"""e-SAJ TJSP (1º grau): cliente HTTP + parser para as estruturas comuns (Capa/Evento/Documento).

Fluxo mapeado (out/2026), tudo por HTTP com os cookies da sessão Loy:
  1. GET cpopg/search.do (número unificado)          -> 302 p/ show.do?processo.codigo=...
  2. GET show.do?...&gateway=true                     -> SSO do CAS (cookie CASTGC) autentica o
     cpopg; sem isso a pasta digital pede "senha do processo".
     A página traz capa, partes, "Processo principal" (em incidentes) e a lista de
     "Incidentes, ações incidentais, recursos e execuções de sentenças" — em SP o
     cumprimento de sentença costuma correr em incidente com número próprio.
  3. GET carregarMovimentacoesAjax.do?processo.codigo=X (+cursor) -> movimentações (keyset)
  4. GET abrirPastaDigital.do?processo.codigo=X       -> URL da pasta (com ticket)
     GET essa URL                                      -> árvore de documentos em JSON (requestScope)
  5. GET pastadigital/getPDF.do?<parametros>          -> PDF do documento
A pasta tem limite diário de acessos em processos em que o advogado não está vinculado;
por isso ela é aberta uma vez por processo e só os documentos selecionados são baixados.

Um "grupo" = processo principal + incidentes de cumprimento. `abrir_autos` devolve o grupo
inteiro serializado em JSON (Autos.html) e o parser monta UMA timeline com tudo.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import datetime

import httpx
from selectolax.lexbor import LexborHTMLParser

from . import perfil
from .cnj import NumeroCNJ
from .pje_http import UA, Autos, ProcessoNaoEncontrado, SessaoExpirada, _Ritmo, cliente_http
from .timeline import Capa, Documento, Evento

log = logging.getLogger(__name__)
BASE = "https://esaj.tjsp.jus.br/"
CS_CLASSES = ("cumprimento de sentenca", "execucao", "liquidacao")
DOC_INTERNO = ("decisao", "despacho", "sentenca", "certidao", "ato ordinatorio", "intimacao",
               "termo", "oficio", "mandado", "acordao", "edital", "carta", "aviso", "conclusao")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).lower().strip()


def _txt(node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ") if node else "").strip()


class LimiteConsulta(RuntimeError):
    pass


class EsajHttp:
    def __init__(self, cookies: httpx.Cookies, min_intervalo: float = 1.0, jitter: float = 0.8,
                 max_incidentes: int = 6, base: str = BASE, rotulo: str = "esaj", chrome: bool = False):
        self.base = base.rstrip("/") + "/"
        # TJMS publica a consulta em cpopg5/cposg5 (cpopg/ cai na página "não encontrada" do portal novo)
        self.sufixo = "5" if "tjms.jus.br" in self.base else ""
        self.rotulo = rotulo
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        self.max_incidentes = max_incidentes
        self._cl = cliente_http(cookies, 90, True, chrome)

    def close(self) -> None:
        self._cl.close()

    def _req(self, url: str, params: dict | None = None, ajax: bool = False) -> httpx.Response:
        if not url.startswith("http"):
            url = url.lstrip("/")
            if self.sufixo and re.match(r"(cpopg|cposg)/", url):
                url = url.replace("/", self.sufixo + "/", 1)
            url = self.base + url
        headers = {"X-Requested-With": "XMLHttpRequest"} if ajax else {}
        for tentativa in range(5):
            self.ritmo.esperar()
            self.n_req += 1
            try:
                r = self._cl.get(url, params=params, headers=headers)
            except httpx.TransportError as e:
                log.warning("rede (%s); tentativa %d", e, tentativa + 1)
                time.sleep(2 ** tentativa * 2)
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 ** tentativa * 5)
                continue
            final = str(r.url)
            if "sajcas/login" in final and ("username" in r.text or "certificado" in r.text.lower()):
                raise SessaoExpirada(final[:120])
            # e-SAJ limita consultas simultâneas: recua e tenta de novo
            m = re.search(r'id="mensagemRetorno".*?<li[^>]*>(.*?)</li>', r.text, re.S)
            if m and "simult" in _norm(m.group(1)):
                espera = 15 * (tentativa + 1)
                log.warning("e-SAJ limitou consultas; aguardando %ss", espera)
                time.sleep(espera)
                continue
            return r
        raise LimiteConsulta(f"falha persistente em {url[:100]}")

    def sessao_valida(self) -> bool:
        try:
            r = self._req("esaj/api/auth/session", ajax=True)
            if r.status_code == 404:  # e-SAJ antigo (ex.: TJAM): sem a API; vale o link de logout
                r = self._req("cpopg/open.do")
                return "sajcas/logout" in r.text.lower() or "esajlogout" in r.text.lower()
            return r.status_code == 200 and '"oabs":[{' in r.text
        except SessaoExpirada:
            return False

    # ------------------------------------------------------------------ processo
    def _abrir_show(self, numero: str | None = None, show_url: str | None = None) -> tuple[str, str]:
        if numero:
            r = self._req("cpopg/search.do", {
                "conversationId": "", "cbPesquisa": "NUMPROC",
                "numeroDigitoAnoUnificado": numero[:15], "foroNumeroUnificado": numero[-4:],
                "dadosConsulta.valorConsultaNuUnificado": numero, "dadosConsulta.valorConsulta": "",
                "dadosConsulta.tipoNuProcesso": "UNIFICADO"})
            if "processo.codigo=" not in str(r.url):
                # vários resultados (ex.: incidentes com o mesmo número-base) ou nenhum
                links = re.findall(r'href="(/cpopg5?/show\.do\?processo\.codigo=[^"]+)"', r.text)
                alvo = next((l for l in links if numero in l), links[0] if links else None)
                if not alvo:
                    raise ProcessoNaoEncontrado(f"{numero} (e-SAJ)")
                show_url = alvo.replace("&amp;", "&")
            else:
                show_url = str(r.url)
        sep = "&" if "?" in show_url else "?"
        r = self._req(show_url + ("" if "gateway=true" in show_url else f"{sep}gateway=true"))
        cod = re.search(r"processo\.codigo=([A-Z0-9]+)", str(r.url)) or \
            re.search(r"processo\.codigo=([A-Z0-9]+)", show_url)
        if not cod or "numeroProcesso" not in r.text:
            raise ProcessoNaoEncontrado(f"{numero or show_url} (e-SAJ)")
        return cod.group(1), r.text

    def _movimentacoes(self, codigo: str) -> str:
        r = self._req("cpopg/carregarMovimentacoesAjax.do", {"processo.codigo": codigo}, ajax=True)
        partes = [r.text]
        cursor = re.search(r'id="cursorMovimentacoesPaginado" value="([^"]+)"', r.text)
        for _ in range(30):
            if not cursor:
                break
            r = self._req("cpopg/carregarMovimentacoesAjax.do",
                          {"processo.codigo": codigo, "cursor": cursor.group(1)}, ajax=True)
            partes.append(r.text)
            cursor = re.search(r'id="trCursorMovimentacoes"[^>]*data-cursor="([^"]+)"', r.text)
        # A 1ª página vem numa tabela; as seguintes, como <tr> soltos. Junta só as linhas.
        linhas = [m for parte in partes for m in re.findall(
            r'<tr[^>]*containerMovimentacao[^>]*>.*?</tr>', parte, re.S)]
        return "<table><tbody>" + "".join(linhas) + "</tbody></table>"

    def _pasta(self, codigo: str, show_html: str) -> list:
        if not re.search(r'href="/cpopg5?/abrirPastaDigital\.do', show_html):
            return []  # pasta exige senha (advogado não vinculado) ou processo físico
        url = self._req("cpopg/abrirPastaDigital.do", {"processo.codigo": codigo}).text.strip()
        if not url.startswith("http"):
            if "limite" in _norm(url):
                log.warning("limite diário de acesso à pasta digital atingido")
            return []
        h = self._req(url).text
        m = re.search(r"requestScope\s*=\s*(\[.*?\]);", h, re.S)
        return json.loads(m.group(1)) if m else []

    def _bloco(self, codigo: str, html: str, papel: str) -> dict:
        num = re.search(r'id="numeroProcesso"[^>]*>\s*([\d.\-]+)', html)
        classe = re.search(r'id="classeProcesso"[^>]*title="([^"]*)"', html)
        return {"codigo": codigo, "papel": papel, "numero": num.group(1) if num else codigo,
                "classe": classe.group(1) if classe else "",
                "show": _so_conteudo(html),
                "movs": self._movimentacoes(codigo), "arvore": self._pasta(codigo, html)}

    # ------------------------------------------------------------------ 2º grau (cposg)
    @staticmethod
    def _consulta_2g(numero: str) -> str | None:
        """cposg = TJ (origem .0000); cposgcr = Colégio Recursal dos juizados (origem 9xxx)."""
        if numero.endswith(".0000"):
            return "cposg"
        if re.search(r"\.8\.26\.9\d{3}$", numero):
            return "cposgcr"
        return None

    def _abrir_2g(self, numero: str) -> tuple[str, str]:
        cons = self._consulta_2g(numero)
        r = self._req(f"{cons}/search.do", {
            "conversationId": "", "paginaConsulta": "1", "cbPesquisa": "NUMPROC", "tipoNuProcesso": "UNIFICADO",
            "numeroDigitoAnoUnificado": numero[:15], "foroNumeroUnificado": numero[-4:],
            "dePesquisaNuUnificado": numero, "dePesquisa": "", "uuidCaptcha": "", "pbEnviar": "Pesquisar"})
        if "cdProcessoMovimentacoes" not in r.text:
            links = re.findall(rf'href="(/{cons}/show\.do\?processo\.codigo=[^"]+)"', r.text)
            if not links:
                raise ProcessoNaoEncontrado(f"{numero} (e-SAJ 2º grau)")
            r = self._req(links[0].replace("&amp;", "&"))
        cod = re.search(r"cdProcessoMovimentacoes = '(\w+)'", r.text)
        if not cod:
            raise ProcessoNaoEncontrado(f"{numero} (e-SAJ 2º grau)")
        return cod.group(1), r.text

    def _movimentacoes_2g(self, codigo: str, cons: str = "cposg") -> str:
        """Movimentações do cposg, convertidas para as classes do cpopg (mesmo parser)."""
        partes, cursor = [], None
        for _ in range(30):
            params = {"cdProcesso": codigo} | ({"cursor": cursor} if cursor else {})
            r = self._req(f"{cons}/carregarMovimentacoesAjax.do", params, ajax=True)
            partes.append(r.text)
            meta = re.search(r'id="metaPaginaMovimentacoes"[^>]*>', r.text)
            if meta:
                if 'data-tem-mais-paginas="true"' not in meta.group(0):
                    break
                cursor = (re.search(r'data-cursor="([^"]+)"', meta.group(0)) or [None, None])[1]
            else:
                c = re.search(r'id="linkMaisRecolherMovimentacoes"[^>]*data-cursor="([^"]+)"', r.text)
                cursor = c.group(1) if c else None
            if not cursor:
                break
        linhas = [m for parte in partes for m in re.findall(r'<tr[^>]*movimentacaoProcesso[^>]*>.*?</tr>', parte, re.S)]
        html = "".join(linhas)
        html = re.sub(r"\bdataMovimentacaoProcesso\b", "dataMovimentacao", html)
        html = re.sub(r"\bdescricaoMovimentacaoProcesso\b", "descricaoMovimentacao", html)
        html = re.sub(r"\bmovimentacaoProcesso\b", "containerMovimentacao", html)
        return "<table><tbody>" + html + "</tbody></table>"

    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15) -> Autos:
        if self._consulta_2g(numero.formatado):
            return self._abrir_autos_2g(numero)
        cod, html = self._abrir_show(numero.formatado)
        return self._grupo(numero, cod, html)

    def _abrir_autos_2g(self, numero: NumeroCNJ) -> Autos:
        """Recurso no TJ (agravo etc.): o risco está no processo de 1ª instância. Abre a origem
        (com seus incidentes de cumprimento) e acrescenta as movimentações do recurso ao grupo."""
        cod2, html2 = self._abrir_2g(numero.formatado)
        classe = re.search(r'id="classeProcesso"[^>]*>\s*<span title="([^"]+)"', html2)
        bloco2 = {"codigo": cod2, "papel": "recurso",
                  "numero": f"{numero.formatado} - {(classe.group(1) if classe else 'RECURSO').upper()} 2º GRAU",
                  "show": _so_conteudo(html2), "movs": self._movimentacoes_2g(cod2, self._consulta_2g(numero.formatado)),
                  "arvore": []}
        origem = re.search(r'href="(https?://[^"]+/cpopg5?/show\.do\?processo\.codigo=[^"]+)"', html2)
        # Colégio Recursal: o nº de 1ª instância vem só como texto, sem link
        i = html2.find("1ª Instância")
        origem_num = re.search(r"\d{7}-\d{2}\.\d{4}\.8\.26\.\d{4}", html2[i:i + 3000]) if i >= 0 else None
        if origem or origem_num:
            try:
                cod, html = (self._abrir_show(show_url=origem.group(1).replace("&amp;", "&")) if origem
                             else self._abrir_show(origem_num.group(0)))
                autos = self._grupo(numero, cod, html, extra=[bloco2])
                return autos
            except ProcessoNaoEncontrado:
                log.warning("%s: origem de 1ª instância não abriu; analisando só o recurso", numero.formatado)
        else:
            log.warning("%s: recurso sem origem no e-SAJ (1ª instância em outro sistema?)", numero.formatado)
        bloco2["papel"] = "principal"
        grupo = {"numero": numero.formatado, "blocos": [bloco2]}
        autos = Autos(numero=numero.formatado, id_processo=cod2, url=self.base + f"{self._consulta_2g(numero.formatado)}/show.do?processo.codigo=" + cod2,
                      html=json.dumps(grupo, ensure_ascii=False), view_state="", sistema=self.rotulo)
        autos.paginas = 1
        return autos

    def _grupo(self, numero: NumeroCNJ, cod: str, html: str, extra: list | None = None) -> Autos:
        grupo = {"numero": numero.formatado, "blocos": []}
        principal = re.search(r'class="processoPrinc" href="([^"]+)"', html)
        if principal:  # entrou por um incidente: o título está no processo principal
            cod_p, html_p = self._abrir_show(show_url=principal.group(1).replace("&amp;", "&"))
            grupo["blocos"].append(self._bloco(cod_p, html_p, "principal"))
            grupo["blocos"].append(self._bloco(cod, html, "incidente"))
            irmaos = [(h, r) for h, r in _incidentes(html_p) if f"codigo={cod}" not in h]
        else:
            grupo["blocos"].append(self._bloco(cod, html, "principal"))
            irmaos = _incidentes(html)
        # só cumprimento/execução/liquidação; filtra antes de limitar (recursos não ocupam vaga)
        irmaos = [(h, r) for h, r in irmaos if any(c in _norm(r) for c in CS_CLASSES)]
        if irmaos:
            for href, rotulo in irmaos[: self.max_incidentes]:
                try:
                    cod_i, html_i = self._abrir_show(show_url=href)
                    b = self._bloco(cod_i, html_i, "incidente")
                    # o link do principal diz "Cumprimento de sentença (0000909-16.2026.8.26.0201)":
                    # a página do incidente nem sempre traz o número CNJ
                    m = re.match(r"(.*?)\s*\((\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})\)", rotulo)
                    if m:
                        b["classe"] = b.get("classe") or m.group(1).strip()
                        if b["numero"] == cod_i:
                            b["numero"] = m.group(2)
                    grupo["blocos"].append(b)
                except ProcessoNaoEncontrado:
                    log.warning("incidente %s não abriu", rotulo)
        grupo["blocos"] += extra or []
        autos = Autos(numero=numero.formatado, id_processo=cod, url=self.base + f"cpopg{self.sufixo}/show.do?processo.codigo=" + cod,
                      html=json.dumps(grupo, ensure_ascii=False), view_state="", sistema=self.rotulo)
        autos.paginas = len(grupo["blocos"])
        return autos

    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str) -> tuple[str, bytes, str]:
        r = self._req("pastadigital/getPDF.do?" + link_id)
        if r.content[:4] != b"%PDF":
            raise RuntimeError(f"documento {doc_id} não veio em PDF ({r.headers.get('content-type')})")
        return "application/pdf", r.content, ""


def _so_conteudo(html: str) -> str:
    """Guarda só o miolo da página (capa, partes, incidentes) para o JSON não ficar enorme."""
    ini = html.find('id="containerDadosPrincipaisProcesso"')
    fim = html.find("Audiências", ini)
    return html[max(0, ini - 200): fim if fim > 0 else ini + 60000]


def _incidentes(html: str) -> list[tuple[str, str]]:
    return [(h.replace("&amp;", "&"), re.sub(r"\s+|&nbsp;", " ", t).strip())
            for h, t in re.findall(r'<a class="incidente" href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)]


# ------------------------------------------------------------------------- parser
def _rotulo(t: LexborHTMLParser, nome: str) -> str:
    for lab in t.css("span.unj-label"):
        if _norm(_txt(lab)) == _norm(nome):
            alvo = lab.next
            while alvo is not None and (alvo.tag in ("-text",) and not alvo.text().strip()):
                alvo = alvo.next
            return _txt(alvo) if alvo is not None else ""
    return ""


def _partes(t: LexborHTMLParser) -> list[tuple[str, str, list[str]]]:
    """[(tipo, nome, [advogados])] da tabela completa de partes."""
    tabela = t.css_first("#tableTodasPartes") or t.css_first("#tablePartesPrincipais")
    saida = []
    if tabela is None:
        return saida
    for tr in tabela.css("tr"):
        tipo = _txt(tr.css_first(".tipoDeParticipacao"))
        td = tr.css_first(".nomeParteEAdvogado")
        if not td:
            continue
        bruto = td.text(separator="\n")
        linhas = [re.sub(r"\s+", " ", x).strip() for x in bruto.split("\n") if x.strip()]
        nome = linhas[0] if linhas else ""
        advs = [linhas[i + 1] for i, x in enumerate(linhas[:-1]) if x.startswith("Advogad")]
        saida.append((tipo, nome, advs))
    return saida


def _eh_cliente(nome: str) -> bool:
    return perfil.eh_cliente(nome)


def parse_capa(autos_json: str) -> Capa:
    g = json.loads(autos_json)
    capa = Capa()
    blocos = g["blocos"]
    p = LexborHTMLParser(blocos[0]["show"])
    # bloco mais recente da 1ª instância (o recurso do 2º grau tem agravante/agravado, não autor/réu)
    ultimo = LexborHTMLParser(next((b for b in reversed(blocos) if b["papel"] != "recurso"), blocos[0])["show"])
    capa.classe = " / ".join(dict.fromkeys(
        filter(None, (_txt(LexborHTMLParser(b["show"]).css_first("#classeProcesso")) or
                      _rotulo(LexborHTMLParser(b["show"]), "Classe") for b in blocos))))
    capa.orgao_julgador = f"{_txt(p.css_first('#foroProcesso'))} - {_txt(p.css_first('#varaProcesso'))}"
    capa.valor_causa = _txt(ultimo.css_first("#valorAcaoProcesso")) or _txt(p.css_first("#valorAcaoProcesso"))
    capa.autuacao = _txt(p.css_first("#dataHoraDistribuicaoProcesso"))
    capa.jurisdicao = _txt(p.css_first("#areaProcesso"))
    capa.competencia = capa.orgao_julgador
    # partes do bloco mais recente (incidente de CS mostra quem está habilitado na execução)
    for tipo, nome, advs in _partes(ultimo) or _partes(p):
        linha = f"{nome} ({tipo}) — advogados: {', '.join(advs) or 'nenhum'}"
        if _eh_cliente(nome):
            capa.polo_passivo.append(linha)
        elif _norm(tipo).startswith(("reqte", "autor", "exeqte", "exequente", "reqte.")):
            capa.polo_ativo.append(linha)
        else:
            capa.polo_passivo.append(linha) if _norm(tipo).startswith(("reqdo", "reu", "exectdo")) else None
    capa.associados = max(0, len(blocos) - 1)
    return capa


def advogados(autos_json: str) -> tuple[set[str], set[str]]:
    """(advogados da ré, advogados do polo ativo) — para atribuir o polo das petições."""
    g = json.loads(autos_json)
    reu, ativo = set(), set()
    for b in g["blocos"]:
        for tipo, nome, advs in _partes(LexborHTMLParser(b["show"])):
            alvo = reu if _eh_cliente(nome) else ativo if _norm(tipo).startswith(("reqte", "exeqte", "autor")) else None
            if alvo is not None:
                alvo.update(_norm(a) for a in advs)
    return reu, ativo


def _data(s: str) -> datetime | None:
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip()[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def parse_timeline(autos_json: str) -> list[Evento]:
    g = json.loads(autos_json)
    eventos: list[Evento] = []
    for b in g["blocos"]:
        rot = "" if b["papel"] == "principal" else f"[INCIDENTE {_numero_do_bloco(b)}] "
        t = LexborHTMLParser(b["movs"])
        for tr in t.css("tr.containerMovimentacao"):
            d = _data(_txt(tr.css_first(".dataMovimentacao")))
            desc = tr.css_first(".descricaoMovimentacao")
            if not d or desc is None:
                continue
            compl = _txt(desc.css_first("span"))
            titulo = _txt(desc).replace(compl, "").strip()
            movs = [rot + titulo + (f" — {compl}" if compl else "")]
            eventos.append(Evento(0, d.date(), "00:00", "INTERNO", movs, []))
        ultimo_ev: Evento | None = None
        ultimo_dt: datetime | None = None
        for doc in sorted(b.get("arvore") or [], key=lambda x: x.get("data", {}).get("dtInclusao") or ""):
            dd = doc.get("data", {})
            d = _data(dd.get("dtInclusao") or "")
            filhos = doc.get("children") or []
            if not d or not filhos:
                continue
            titulo = dd.get("title", "")
            n = _norm(titulo)
            polo = "INTERNO" if any(n.startswith(k) for k in DOC_INTERNO) else "PARTE"
            params = filhos[0]["data"].get("parametros", "")
            # getPDF de um documento inteiro: troca a página inicial/final pelo intervalo completo
            paginas = sum(c["data"].get("nuPaginas", 0) for c in filhos)
            params = re.sub(r"numFinal=\d+", f"numFinal={paginas}", params) if len(filhos) > 1 else params
            documento = Documento(doc_id=str(dd.get("cdDocumento")), tipo=titulo,
                                  titulo=f"{rot.strip()} {paginas}p".strip(), link_id=params)
            # A pasta lista cada arquivo do protocolo como documento próprio (petição, guia,
            # recibo...). Arquivos juntados até 90 s depois do anterior viram anexos dele.
            if ultimo_ev is not None and polo != "INTERNO" and ultimo_ev.polo != "INTERNO" \
                    and ultimo_dt and 0 <= (d - ultimo_dt).total_seconds() <= 90:
                documento.anexo_de = ultimo_ev.documentos[0].doc_id
                ultimo_ev.documentos.append(documento)
                ultimo_dt = d
                continue
            ev = Evento(0, d.date(), d.strftime("%H:%M"), polo,
                        [f"{rot}Documento juntado: {titulo}"] if rot else [], [documento])
            eventos.append(ev)
            ultimo_ev, ultimo_dt = ev, d
    # mais recente primeiro; documentos (com hora) depois dos movimentos do mesmo dia
    eventos.sort(key=lambda e: (e.data, e.hora), reverse=True)
    for i, e in enumerate(eventos):
        e.seq = i
    return eventos


def classes_incidentes(autos_json: str) -> dict[str, str]:
    """Número -> classe dos incidentes do grupo (a classe fica fora do prefixo dos movimentos)."""
    try:
        g = json.loads(autos_json)
    except ValueError:
        return {}
    return {_numero_do_bloco(b): b.get("classe") or "" for b in g.get("blocos", []) if b["papel"] != "principal"}


def _numero_do_bloco(b: dict) -> str:
    return b.get("numero") or b["codigo"]


def polo_por_assinatura(texto: str, adv_reu: set[str], adv_ativo: set[str]) -> str | None:
    """No e-SAJ a pasta não diz quem juntou; o rodapé do PDF diz quem assinou."""
    m = re.search(r"assinado digitalmente por ([^,\n]+)", texto)
    if not m:
        return None
    nome = _norm(m.group(1))
    if any(nome == a or nome in a or a in nome for a in adv_reu):
        return "PASSIVO"
    if any(nome == a or nome in a or a in nome for a in adv_ativo):
        return "ATIVO"
    if any(k in nome for k in ("juiz", "juiza", "escrevente", "diretor", "chefe")):
        return "INTERNO"
    return "TERCEIRO"
