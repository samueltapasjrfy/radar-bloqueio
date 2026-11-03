"""Cliente HTTP do Projudi (versão CNJ usada pelo TJBA), com os cookies de uma sessão logada pela Loy.

Fluxo mapeado no Projudi TJBA:
  1. POST advogado/CentroAdvogado (login=<cód. OAB>)      -> escolhe a OAB da UF do tribunal
  2. listagens/ProcessosAdvogado?tipo=promovido|arquivado (por OAB, paginadas) -> índice CNJ -> DadosProcesso
     (data/projudi_<uf>_indice.json). A busca por número tem captcha e não é usada; processo em que o
     advogado não atua cai em "Controle de Acesso" (consentimento + captcha) e também não é acessado.
  3. GET  listagens/DadosProcesso                          -> capa, partes/advogados e todos os eventos
  4. GET  listagens/DownloadArquivo?arquivo=<id>           -> arquivo do evento (PDF/HTML)
"""

from __future__ import annotations

import html as htmlmod
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import httpx

from . import perfil
from .cnj import NumeroCNJ
from .config import home
from .pje_http import Autos, ProcessoNaoEncontrado, SessaoExpirada, _Ritmo, cliente_http
from .timeline import Capa, Documento, Evento, tipo_canonico

log = logging.getLogger(__name__)

# papéis que aparecem como cabeçalho de bloco de partes na capa
PAPEIS = ("Exequente", "Executado", "Promovente", "Promovido", "Requerente", "Requerido", "Autor", "Réu",
          "Reclamante", "Reclamado", "Recorrente", "Recorrido", "Impugnante", "Impugnado", "Embargante",
          "Embargado", "Testemunha", "Terceiro")


def _limpo(s: str) -> str:
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


class ProjudiHttp:
    def __init__(self, base_url: str, cookies: httpx.Cookies, uf: str, rotulo: str = "projudi",
                 min_intervalo: float = 1.0, jitter: float = 0.8, chrome: bool = False):
        self.base = base_url.rstrip("/") + "/"
        self.uf = uf.upper()
        self.rotulo = rotulo
        self.ritmo = _Ritmo(min_intervalo, jitter)
        self.n_req = 0
        self._oab_ok = False
        self._cl = cliente_http(cookies, 90, True, chrome)

    def close(self) -> None:
        self._cl.close()

    def _req(self, metodo: str, url: str, data: dict | None = None) -> httpx.Response:
        if url.startswith("/"):
            url = self.base.split("/projudi/")[0] + url
        elif not url.startswith("http"):
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
            if "login" in final.rsplit("/", 1)[-1] or "sessão expirou" in r.text.lower()[:3000]:
                raise SessaoExpirada(final[:120])
            return r
        raise RuntimeError(f"falha persistente em {url[:100]}")

    def _escolher_oab(self) -> None:
        if self._oab_ok:
            return
        r = self._req("GET", "advogado/CentroAdvogado")
        cod = None
        for linha in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
            m = re.search(r"selecionarOAB\('(\d+)'", linha)
            if not m:
                continue
            cells = [_limpo(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", linha, re.S)]
            if cod is None:
                cod = m.group(1)
            if self.uf in cells:
                cod = m.group(1)
                break
        if cod:
            self._req("POST", "advogado/CentroAdvogado", {"login": cod})
        self._oab_ok = True

    def sessao_valida(self) -> bool:
        try:
            r = self._req("GET", "advogado/CentroAdvogado")
        except SessaoExpirada:
            return False
        return r.status_code == 200 and ("selecionarOAB" in r.text or "frameCentroAdvogado" in r.text)

    def _oabs(self) -> list[str]:
        r = self._req("GET", "advogado/CentroAdvogado")
        return list(dict.fromkeys(re.findall(r"selecionarOAB\('(\d+)'", r.text)))

    def indexar(self, destino: Path, tipos=("promovido", "arquivado")) -> dict[str, str]:
        """CNJ -> id do processo, pelas listagens de todas as OABs do advogado (sem captcha)."""
        indice: dict[str, str] = json.loads(destino.read_text())["indice"] if destino.exists() else {}
        for cod in self._oabs():
            self._req("POST", "advogado/CentroAdvogado", {"login": cod})
            for tipo in tipos:
                pagina, total = 1, 1
                while pagina <= total:
                    r = self._req("POST", f"listagens/ProcessosAdvogado?tipo={tipo}",
                                  {"pagina": str(pagina), "coluna": "Processo.NUMEROPROCESSO", "ordem": "ASC"})
                    m = re.search(r"página \d+ de (\d+)", r.text)
                    total = int(m.group(1)) if m else 1
                    for idp, cnj in re.findall(r'DadosProcesso\?numeroProcesso=(\d+)"[^>]*>\s*(\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})', r.text):
                        indice[cnj] = idp
                    pagina += 1
                    if pagina % 10 == 0:
                        destino.write_text(json.dumps({"data": date.today().isoformat(), "indice": indice}))
                log.info("projudi: OAB %s %s -> %d processos no índice", cod, tipo, len(indice))
        destino.write_text(json.dumps({"data": date.today().isoformat(), "indice": indice}))
        self._oab_ok = False
        return indice

    def _arquivo_indice(self) -> Path:
        return home() / "data" / f"projudi_{self.uf.lower()}_indice.json"

    def _indice(self, refazer_se_velho: bool = False) -> dict[str, str]:
        """Índice CNJ -> id do processo. Número ausente e índice de outro dia: refaz pelas listagens."""
        arq = self._arquivo_indice()
        dados = json.loads(arq.read_text()) if arq.exists() else {"data": "", "indice": {}}
        if refazer_se_velho and dados.get("data") != date.today().isoformat():
            log.info("projudi %s: índice de %s; refazendo pelas listagens do advogado", self.uf, dados.get("data") or "nunca")
            arq.parent.mkdir(parents=True, exist_ok=True)
            return self.indexar(arq)
        return dados["indice"]

    def abrir_autos(self, numero: NumeroCNJ, max_paginas: int = 15) -> Autos:
        idp = self._indice().get(numero.formatado) or self._indice(refazer_se_velho=True).get(numero.formatado)
        if not idp:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (projudi: fora das listagens do advogado)")
        url = self.base + f"listagens/DadosProcesso?numeroProcesso={idp}"
        r = self._req("GET", url)
        if "Controle de Acesso" in r.text and "consentimentoAcesso" in r.text:
            raise RuntimeError("Projudi: advogado não atua nos autos (acesso exige consentimento e captcha)")
        if "Eventos do Processo" not in r.text:
            raise ProcessoNaoEncontrado(f"{numero.formatado} (projudi: sem eventos)")
        return Autos(numero=numero.formatado, id_processo=idp, url=url,
                     html=r.text, view_state="", sistema=self.rotulo)

    def baixar_documento(self, autos: Autos, link_id: str, doc_id: str) -> tuple[str, bytes, str]:
        r = self._req("GET", link_id)
        if not r.content:
            raise RuntimeError(f"documento {doc_id} veio vazio")
        return r.headers.get("content-type", ""), r.content, ""


# ------------------------------------------------------------------------- parser
def _partes(html: str) -> list[tuple[str, list[str], list[str]]]:
    """[(papel, nomes das partes, nomes dos advogados)] da capa."""
    ini = html.find("DADOS DO PROCESSO")
    fim = html.find("Proc. Principal", ini)
    trecho = html[ini:fim if fim > 0 else ini + 200000]
    rotulos = "|".join(re.escape(p) for p in PAPEIS)
    blocos = re.split(rf"<td[^>]*>\s*(?:<[^>]+>\s*)*({rotulos})\s*(?:<[^>]+>\s*)*</td>", trecho)
    saida = []
    for i in range(1, len(blocos) - 1, 2):
        papel, corpo = blocos[i], blocos[i + 1]
        partes_html, _, adv_html = corpo.partition("OAB/")
        texto = _limpo(partes_html)
        nomes = re.findall(r"([A-ZÀ-Ú0-9][A-ZÀ-Ú0-9 .&/'-]{4,}?)\s+(?:\d{2,3}\.\d{3}\.\d{3}[/-]\d|\s*&nbsp;|Mostrar)", texto)
        advs = re.findall(r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{4,}?)\s*\(CPF:", _limpo(adv_html).replace("MADEP", ""))
        saida.append((papel, [n.strip() for n in nomes], [a.strip() for a in advs]))
    return saida


def parse_capa(html: str) -> Capa:
    capa = Capa()
    texto = _limpo(html)
    m = re.search(r"Classe:\s*(.+?)(?:«|Tipo de A)", texto)
    capa.classe = m.group(1).strip() if m else ""
    m = re.search(r"Ju[ií]zo:\s*(.+?)\s+Juiz\b", texto)
    capa.orgao_julgador = m.group(1).strip() if m else ""
    m = re.search(r"Valor da Causa\s*:?\s*(R\$ ?[\d.,]+)", texto)
    capa.valor_causa = m.group(1) if m else ""
    m = re.search(r"Data de Distribui[çc][ãa]o\s*(.+?)\s*h\b", texto)
    capa.autuacao = m.group(1).strip() if m else ""
    m = re.search(r"Fase Processual\s*:?\s*(\S+)", texto)
    capa.competencia = m.group(1) if m else ""
    for papel, nomes, advs in _partes(html):
        if papel in ("Testemunha", "Terceiro") or not nomes:
            continue
        linha = " / ".join(nomes) + (" - Advogados: " + ", ".join(advs) if advs else "")
        (capa.polo_passivo if any(perfil.eh_cliente(n) for n in nomes) else capa.polo_ativo).append(linha)
    return capa


def parse_timeline(html: str) -> list[Evento]:
    advs_ativo, advs_passivo = set(), set()
    for papel, nomes, advs in _partes(html):
        alvo = advs_passivo if any(perfil.eh_cliente(n) for n in nomes) else advs_ativo
        alvo.update(a.upper() for a in advs)
    ini = html.find('id="Arquivos"')
    corpo = html[ini:] if ini >= 0 else html
    pedacos = re.split(r'<td\s+height="40" width="33">\s*(\d+)\s*</td>', corpo)
    vistos: dict[int, Evento] = {}
    for i in range(1, len(pedacos) - 1, 2):
        num, bloco = int(pedacos[i]), pedacos[i + 1]
        if num in vistos:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", bloco.split("<span id=\"obs")[0], re.S)
        if len(tds) < 3:
            continue
        desc, data_txt, por = _limpo(tds[0]), _limpo(tds[1]), _limpo(tds[2])
        try:
            quando = datetime.strptime(data_txt, "%d/%m/%y")
        except ValueError:
            continue
        por_up = por.upper()
        polo = ("PASSIVO" if por_up in advs_passivo or perfil.eh_cliente(por_up)
                else "ATIVO" if por_up in advs_ativo else "INTERNO")
        docs = []
        for href, nome in re.findall(r'<a href="(/projudi/listagens/DownloadArquivo\?arquivo=\d+)"\s*>(.*?)</a>',
                                     bloco, re.S):
            doc_id = href.rsplit("=", 1)[1]
            docs.append(Documento(doc_id=doc_id, tipo=tipo_canonico(desc, "", "turma recursal" in desc.lower()),
                                  titulo=f"{desc[:60]} ({_limpo(nome)})",
                                  link_id=href, anexo_de=docs[0].doc_id if docs else ""))
        movs = [f"Evento {num} - {desc}"] + ([f"por {por[:80]}"] if por and polo != "INTERNO" else [])
        vistos[num] = Evento(seq=0, data=quando.date(), hora="00:00", polo=polo, movimentos=movs, documentos=docs)
    eventos = [vistos[k] for k in sorted(vistos, reverse=True)]
    for i, e in enumerate(eventos):
        e.seq = i
    return eventos
