"""Cliente HTTP do Projudi do TJGO (sistema próprio, não é o fork CNJ nem o TJPR), com cookies da Loy.

Fluxo mapeado em 26/10/2026 (https://projudi.tjgo.jus.br):
  1. GET  Usuario?PaginaAtual=9                      -> "Serventias Disponíveis": uma por OAB do advogado
  2. GET  Usuario?PaginaAtual=7&a1=<id>&a2=2...      -> entra na serventia (OAB). O acesso aos ARQUIVOS
     depende da OAB com que o advogado está habilitado no processo (ex.: 77167-A/MG); com outra OAB o
     arquivo volta "Acesso não permitido ao Arquivo". Por isso a serventia é escolhida pela OAB da habilitação.
  3. POST BuscaProcesso (ProcessoNumero, status)     -> com 1 resultado já devolve a página do processo
     (capa + todas as movimentações). A busca filtra por status: tenta Ativo, Arquivado, Suspenso...
  4. GET  ProcessoParteAdvogado?PaginaAtual=7        -> advogados por parte (processo corrente da sessão)
  5. GET  MovimentacaoArquivo?AJAX=ajax&PaginaAtual=8&Id_Movimentacao=<id> -> JSON dos arquivos
  6. GET  BuscaProcesso?PaginaAtual=6&Id_MovimentacaoArquivo=<id>&hash=<hash>&id_proc=undefined
     -> redireciona para o PDF no S3 do tribunal
Sem captcha na área logada. HTML em ISO-8859-1.
"""

from __future__ import annotations

import html as htmlmod
import json
import logging
import os
import re
import time
from datetime import datetime

import httpx

from . import perfil
from .cnj import NumeroCNJ
from .pje_http import Autos, ProcessoNaoEncontrado, SessaoExpirada, _Ritmo, cliente_http
from .timeline import Capa, Documento, Evento, tipo_canonico

log = logging.getLogger(__name__)

HOST = "https://projudi.tjgo.jus.br"
# (nome, Id_ProcessoStatus, ProcessoStatusCodigo). Status vazio às vezes não acha nem os ativos: sempre explícito
STATUS = (("Ativo", "1", "0"), ("Arquivado", "2", "1"), ("Suspenso", "3", "2"), ("Arquivado Provisoriamente", "4", "3"),
          ("Desarquivado Provisoriamente", "5", "4"), ("Cálculo", "6", "6"))


def _oab_padrao(serventias: dict[str, str]) -> str:
    """OAB da habilitação usual: a do 1º advogado do escritório no perfil, se for uma das serventias
    do certificado; senão BLOQUEIO_OAB_PROJUDI_GO; senão a primeira serventia."""
    for a in perfil.atual().advogados:
        if a.oab_digitos in serventias:
            return a.oab_digitos
    env = os.getenv("BLOQUEIO_OAB_PROJUDI_GO", "")
    return env if env in serventias else next(iter(serventias), "")


def _limpo(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


class ProjudiGoHttp:
    def __init__(self, cookies: httpx.Cookies, rotulo: str = "projudi_go",
                 min_intervalo: float = 1.0, jitter: float = 0.8, chrome: bool = False):
        self.rotulo = rotulo
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        self._serventias: dict[str, str] | None = None  # nº OAB -> a1
        self._oab_atual: str | None = None
        self._cl = cliente_http(cookies, 90, True, chrome)

    def close(self) -> None:
        self._cl.close()

    def _req(self, metodo: str, url: str, data: dict | None = None) -> httpx.Response:
        if not url.startswith("http"):
            url = f"{HOST}/{url.lstrip('/')}"
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
            if "sessão expirada" in _texto(r)[:4000].lower() or "logon?paginaatual=-200" in str(r.url).lower():
                raise SessaoExpirada(str(r.url)[:120])
            return r
        raise RuntimeError(f"falha persistente em {url[:100]}")

    # ------------------------------------------------------------------ serventia (OAB)
    def _listar_serventias(self) -> dict[str, str]:
        if self._serventias is None:
            r = self._req("GET", "Usuario?PaginaAtual=9")
            t = _texto(r)
            self._serventias = {}
            for a1, resto in re.findall(r'href="Usuario\?PaginaAtual=7&(?:amp;)?a1=(\d+)&(?:amp;)?a2=2[^"]*"(.{0,400}?)</a>', t, re.S):
                m = re.search(r"OAB/Matr[íi]cula:\s*(\d+)", _limpo(resto))
                if m:
                    self._serventias[m.group(1)] = a1
            if not self._serventias:
                raise SessaoExpirada("Projudi GO sem lista de serventias")
        return self._serventias

    def _entrar(self, oab: str) -> None:
        if self._oab_atual == oab:
            return
        a1 = self._listar_serventias().get(oab)
        if not a1:
            raise RuntimeError(f"Projudi GO: OAB {oab} não está entre as serventias do advogado")
        self._req("GET", "Usuario?PaginaAtual=9")
        self._req("GET", f"Usuario?PaginaAtual=7&a1={a1}&a2=2&a3=&a4=&a5=")
        self._req("GET", "BuscaProcesso?PaginaAtual=4")  # inicializa o formulário de busca da sessão
        self._oab_atual = oab

    def sessao_valida(self) -> bool:
        try:
            self._serventias = None
            self._oab_atual = None
            self._listar_serventias()
            return True
        except SessaoExpirada:
            return False

    # ------------------------------------------------------------------ autos
    def _buscar(self, numero: NumeroCNJ) -> str:
        for nome, ident, cod in STATUS:
            r = self._req("POST", "BuscaProcesso", {
                "PaginaAtual": "2", "PaginaAnterior": "4", "TipoConsultaProcesso": "null", "BuscaPublica": "null",
                "ServletRedirect": "null", "QuantidadeRegistrosPagina": "", "ProcessoNumero": numero.formatado,
                "Inquerito": "", "NomeParte": "", "PesquisarNomeExato": "false", "CpfCnpjParte": "",
                "ProcessoStatus": nome, "Id_ProcessoStatus": ident, "ProcessoStatusCodigo": cod,
                "ProcessoTipo": "", "Id_ProcessoTipo": "", "ProcessoTipoCodigo": "", "Serventia": "",
                "Id_Serventia": "", "imgSubmeter": "Buscar"})
            t = _texto(r)
            if "tabListaProcesso" in t:
                return t
            # mais de um resultado (ex.: o mesmo número em outra serventia): abre o que casa o número
            for href, rotulo in re.findall(r'href="(BuscaProcesso\?Id_Processo=\d+)"[^>]*>(.*?)</a>', t, re.S):
                if numero.formatado[:10] in _limpo(rotulo):
                    t2 = _texto(self._req("GET", htmlmod.unescape(href)))
                    if "tabListaProcesso" in t2:
                        return t2
        raise ProcessoNaoEncontrado(f"{numero.formatado} (projudi GO)")

    def _advogados(self) -> str:
        return _texto(self._req("GET", "ProcessoParteAdvogado?PaginaAtual=7"))

    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15) -> Autos:
        self._entrar(self._oab_atual or _oab_padrao(self._listar_serventias()))
        html = self._buscar(numero)
        advs = self._advogados()
        # o arquivo só abre na serventia da OAB com que o advogado do escritório está habilitado neste processo
        oab = next((o for _, o, adv in _tabela_advogados(advs) if perfil.atual().advogado_no_texto(adv)), None)
        if oab and oab != self._oab_atual and oab in self._listar_serventias():
            log.info("projudi GO: %s habilitado pela OAB %s; trocando de serventia", numero.formatado, oab)
            self._entrar(oab)
            html = self._buscar(numero)
            advs = self._advogados()
        idp = re.search(r'BuscaProcesso\?Id_Processo=(\d+)"', advs)
        return Autos(numero=numero.formatado, id_processo=idp.group(1) if idp else "", url=f"{HOST}/BuscaProcesso",
                     html=html + '<div class="projudi-go-advs">' + advs + "</div>", view_state="",
                     sistema=self.rotulo)

    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str) -> tuple[str, bytes, str]:
        r = self._req("GET", f"MovimentacaoArquivo?AJAX=ajax&PaginaAtual=8&Id_Movimentacao={link_id}")
        try:
            arquivos = json.loads(_texto(r))
        except ValueError:
            raise RuntimeError(f"documento {doc_id}: lista de arquivos inválida") from None
        partes = []
        for a in arquivos[:3]:
            if int(a.get("AcessoUsuario") or 0) < int(a.get("AcessoArquivo") or 0):
                continue  # arquivo sigiloso para o advogado
            rr = self._req("GET", f"BuscaProcesso?PaginaAtual=6&Id_MovimentacaoArquivo={a['id']}"
                                  f"&hash={a['hash']}&id_proc=undefined")
            if rr.content and "Acesso não permitido" not in _texto(rr)[:3000]:
                partes.append(rr)
        if not partes:
            raise RuntimeError(f"documento {doc_id} sem arquivo acessível")
        if len(partes) == 1:
            return partes[0].headers.get("content-type", ""), partes[0].content, ""
        from .textos import extrair_texto
        texto = "\n\n".join(extrair_texto(x.headers.get("content-type", ""), x.content)[0] for x in partes)
        return "text/plain", texto.encode(), ""


def _texto(r: httpx.Response) -> str:
    ct = r.headers.get("content-type", "").lower()
    if "pdf" in ct or "octet" in ct:
        return ""
    return r.content.decode("latin-1" if "8859" in ct else "utf-8", "replace")


# ------------------------------------------------------------------------- parser
def _tabela_advogados(html: str) -> list[tuple[str, str, str]]:
    """[(parte, nº OAB, advogado)] da página "Advogados no Processo"."""
    corpo = html.split('<div class="projudi-go-advs">')[-1]
    ini = corpo.find("tabListaAdvogadoParte")
    saida = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", corpo[ini:] if ini >= 0 else "", re.S):
        tds = [_limpo(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) >= 4:
            m = re.match(r"(\d+)", tds[2])
            saida.append((tds[0], m.group(1) if m else tds[2], tds[3]))
    return saida


def _polos(html: str) -> list[tuple[str, list[str]]]:
    """[(polo, nomes)] dos blocos "Polo Ativo | Exequente" / "Polo Passivo | Executado" da capa."""
    capa = html.split("<!-- MOVIMENTA")[0]
    saida = []
    for legenda, corpo in re.findall(r"<legend>\s*Polo (Ativo|Passivo)[^<]*</legend>(.*?)(?=<legend>\s*(?:Polo|Outras)|$)",
                                     capa, re.S):
        nomes = [_limpo(n) for n in re.findall(r'<span class="span1 nomes destaque-nome"[^>]*>(.*?)</span>', corpo, re.S)]
        saida.append(("PASSIVO" if legenda == "Passivo" else "ATIVO", [n for n in nomes if n]))
    return saida


def _campo(texto: str, rotulo: str, fim: str) -> str:
    m = re.search(rf"{rotulo}\s+(.+?)\s+{fim}", texto)
    return m.group(1).strip() if m else ""


def parse_capa(html: str) -> Capa:
    capa = Capa()
    texto = _limpo(html.split("<!-- MOVIMENTA")[0])
    capa.orgao_julgador = _campo(texto, "Outras Informações Serventia", "Classe")
    classe = _campo(texto, r"\bClasse", r"Assunto\(s\)")
    capa.classe = classe.split("->")[-1].strip() if "->" in classe else classe
    capa.valor_causa = _campo(texto, "Valor da Causa", "Valor Condena")
    capa.autuacao = _campo(texto, "Dt. Distribuição", "Segredo")
    capa.competencia = _campo(texto, "Fase Processual", "Dt. Distribui")
    capa.jurisdicao = _campo(texto, r"\bStatus", "Prioridade")
    advs: dict[str, list[str]] = {}
    for parte, _, adv in _tabela_advogados(html):
        advs.setdefault(parte.upper(), []).append(adv)
    for polo, nomes in _polos(html):
        for nome in nomes:
            linha = nome + (" - Advogados: " + ", ".join(advs[nome.upper()]) if nome.upper() in advs else "")
            (capa.polo_passivo if polo == "PASSIVO" or perfil.eh_cliente(nome) else capa.polo_ativo).append(linha)
    return capa


def parse_timeline(html: str) -> list[Evento]:
    polo_parte = {n.upper(): p for p, nomes in _polos(html) for n in nomes}
    polo_adv: dict[str, str] = {}
    for parte, _, adv in _tabela_advogados(html):
        polo = "PASSIVO" if perfil.eh_cliente(parte) else polo_parte.get(parte.upper(), "ATIVO")
        polo_adv[adv.upper()] = polo
    corpo = html.split('<div class="projudi-go-advs">')[0]
    ini = corpo.find('id="tabListaProcesso"')
    corpo = corpo[ini:] if ini >= 0 else ""
    eventos = []
    for tr in re.findall(r"<tr movi_codigo[^>]*>(.*?)</tr>", corpo, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 4:
            continue
        num = _limpo(tds[0])
        tipo_m = re.search(r'<span class="filtro_tipo_movimentacao">(.*?)</span>', tds[1], re.S)
        tipo = _limpo(tipo_m.group(1)) if tipo_m else ""
        compl = _limpo(tds[1][tipo_m.end():] if tipo_m else tds[1])
        try:
            quando = datetime.strptime(_limpo(tds[2]), "%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
        usuario = _limpo(tds[3])
        u = usuario.upper()
        if u in polo_adv:
            polo = polo_adv[u]
        elif u.startswith("SISTEMA"):
            polo = "SISTEMA"
        else:
            polo = "INTERNO"  # servidor/magistrado ("Nome - Central de Apoio") ou advogado não habilitado
        docs = []
        m = re.search(r"buscarArquivosMovimentacaoJSON\('(\d+)'", tr)
        if m:
            docs.append(Documento(doc_id=m.group(1)[-12:], tipo=tipo_canonico(tipo, "", "TURMA" in compl.upper()),
                                  titulo=(f"{tipo} - {compl}" if compl else tipo)[:80], link_id=m.group(1)))
        movs = [f"Mov. {num} - {tipo}" + (f" - {compl}" if compl else "")]
        if usuario and polo in ("ATIVO", "PASSIVO"):
            movs.append(f"por {usuario[:80]}")
        eventos.append(Evento(seq=0, data=quando.date(), hora=quando.strftime("%H:%M"), polo=polo,
                              movimentos=movs, documentos=docs))
    eventos.sort(key=lambda e: (e.data, e.hora), reverse=True)
    for i, e in enumerate(eventos):
        e.seq = i
    return eventos
