"""Ponto de entrada do motor: analisa o risco de bloqueio de UM processo, de ponta a ponta.

    motor = Motor(load_config())
    r = motor.analisar("5004214-45.2025.8.13.0515", perfil, anterior=None)

Roteamento: o CNJ diz o tribunal (TJ da UF ou TRF da região) e a ordem dos sistemas a tentar
(`sistemas_de`). Processo migrado (e-SAJ -> eproc, PJe -> eproc) é seguido até o destino e as
duas timelines viram uma só. Recurso no eproc 2º grau junta o processo de origem.

Concorrência: `analisar` é síncrono e pode rodar em várias threads. Cada tribunal tem uma trava
(um processo por vez por tribunal e por máquina: as sessões e o ritmo de cada portal são
compartilhados); tribunais diferentes andam em paralelo. O login no navegador é um por vez.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date

from . import cnj as cnjmod
from . import eproc, perfil as perfilmod
from .analise import _parse, analisar_processo, apenso_confirmado
from .cache import CacheDocs
from .config import Config
from .llm import AnalisadorLLM
from .perfil import Perfil
from .pje_http import ProcessoNaoEncontrado, SessaoExpirada
from .sessao import Sessao

log = logging.getLogger(__name__)

TR_UF = dict(zip([f"{i:02d}" for i in range(1, 28)], (
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SE SP TO").split()))
ORIGEM_1G = {"eproc2gsp": "eprocsp", "eproc2g": "eproc"}
# Sistemas de 1º grau por UF, na ordem de tentativa (só os que a Loy oferece). O 2º grau
# (origem .0000) usa pje2g/eproc2g da mesma UF; o e-SAJ atende os dois graus.
SISTEMAS_UF = {
    "RJ": ["eproc", "pje", "pjerj"], "ES": ["pje"], "BA": ["pje", "projudi"], "SE": ["eproc", "portaladv"],
    "AL": ["esaj", "eproc"], "CE": ["pje", "esaj"], "PB": ["pje"], "RN": ["pje"], "AM": ["esaj"], "PE": ["pje"],
    "PR": ["projudi", "eproc"], "RS": ["eproc"], "PA": ["pje"], "PI": ["pje"], "MA": ["pje"], "DF": ["pje"],
    "AP": ["pje"], "MS": ["esaj", "eproc"], "MT": ["pje"], "AC": ["esaj", "eproc"], "SC": ["eproc"],
    "RO": ["pje"], "TO": ["eproc"], "GO": ["projudi"], "RR": ["projudi"],
}
SEM_2G = {("RJ", "pje2g"), ("RJ", "pjerj2g")}  # a Loy não tem esse portal
RE_NUM_MIGRACAO = re.compile(r"sob o n[uú]mero\s*:?\s*(\d[\d.\-]{18,24}\d)", re.I)


class NaoSuportado(Exception):
    """O tribunal do número não tem sistema no motor (ex.: Justiça do Trabalho, Eleitoral)."""


class NaoEncontrado(Exception):
    """Nenhum sistema do tribunal achou o processo."""


def _digitos(numero: str) -> str:
    return re.sub(r"\D", "", numero or "")


def tribunal_de(numero: str) -> str | None:
    """Sigla do tribunal pelo CNJ: TJSP, TRF3, TRT2, TST, ... (None se o número for inválido)."""
    d = _digitos(numero)
    if len(d) != 20:
        return None
    seg, tr = d[13], d[14:16]
    if seg == "8":
        return f"TJ{TR_UF[tr]}" if tr in TR_UF else None
    if seg == "4":
        return f"TRF{int(tr)}" if tr != "00" else "CJF"
    if seg == "5":
        return "TST" if tr == "00" else f"TRT{int(tr)}"
    return {"1": "STF", "2": "CNJ", "3": "STJ", "6": f"TRE{tr}", "7": f"JM{tr}", "9": f"TJM{tr}"}.get(seg)


def sistemas_de(numero: str) -> list[str]:
    """Sistemas a tentar, em ordem. Lista vazia = tribunal sem suporte."""
    d = _digitos(numero)
    if len(d) != 20:
        return []
    seg, tr, origem = d[13], d[14:16], d[16:20]
    segundo = origem == "0000"
    if seg == "8" and tr == "26":  # TJSP: e-SAJ e, se não estiver lá, eproc (1º ou 2º grau)
        return ["esaj", "eproc2gsp" if segundo else "eprocsp"]
    if seg == "8" and tr == "13":  # TJMG
        return ["pje2g", "eproc2g"] if segundo else ["pje", "eproc"]
    if seg == "4":  # Justiça Federal (TRF da região)
        sis = {"01": ["pje_trf1"], "02": ["eproc_trf2es" if origem[:2] == "50" else "eproc_trf2rj"],
               "03": ["pje_trf3"], "04": ["eproc_trf4sc", "eproc_trf4rs", "eproc_trf4pr"],
               "05": ["pje_trf5"], "06": ["eproc_trf6mg", "pje_trf6mg"]}.get(tr, [])
        if segundo:
            sis = [s.replace("pje_", "pje2g_").replace("eproc_", "eproc2g_") for s in sis]
        return sis
    uf = TR_UF.get(tr) if seg == "8" else None
    if uf not in SISTEMAS_UF:
        return []
    fams = SISTEMAS_UF[uf]
    # 2º grau: origem .0000; o TJRS numera os processos do eproc 2º grau com origem .7000
    if segundo or (uf == "RS" and origem == "7000"):
        fams = [f if f in ("esaj", "portaladv", "projudi") else f + "2g" for f in fams]
        fams = [f for f in fams if (uf, f) not in SEM_2G]
    return [f"{f}_{uf.lower()}" for f in fams]


def eproc_1g(sistema: str) -> str | None:
    """eproc de 1º grau da mesma UF (destino das migrações do e-SAJ)."""
    if sistema == "esaj":
        return "eprocsp"
    uf = sistema.split("_")[1] if "_" in sistema else None
    return f"eproc_{uf}" if uf and "eproc" in SISTEMAS_UF.get(uf.upper(), []) else None


def numeros_migracao(r: dict, numero: str) -> list[str]:
    """Números novos que o e-SAJ informa na migração ("passará a tramitar no eproc sob o número X")."""
    proprio = _digitos(numero)
    achados = [_digitos(m) for m in RE_NUM_MIGRACAO.findall(" ".join(r.get("motivos") or []))]
    return list(dict.fromkeys(n for n in achados if len(n) == 20 and n != proprio))


class Motor:
    def __init__(self, cfg: Config, llm: AnalisadorLLM | None = None, cache: CacheDocs | None = None):
        self.cfg = cfg
        self.llm = llm or (AnalisadorLLM(cfg.openai_api_key, cfg.openai_model) if cfg.openai_api_key else None)
        self.cache = cache or CacheDocs(cfg.data_dir / "docs")
        self._sessoes: dict[str, Sessao] = {}
        self._clientes: dict[str, object] = {}
        self._travas: dict[str, threading.Lock] = {}
        self._mapa = threading.Lock()

    # ------------------------------------------------------------------ sessões
    def _trava(self, chave: str) -> threading.Lock:
        with self._mapa:
            return self._travas.setdefault(chave, threading.Lock())

    def cliente(self, sistema: str, renovar: bool = False):
        if sistema not in self._sessoes:
            self._sessoes[sistema] = Sessao(self.cfg, sistema)
        if renovar or sistema not in self._clientes:
            s = self._sessoes[sistema]
            self._clientes[sistema] = s.renovar() if renovar else s.obter()
        return self._clientes[sistema]

    def requisicoes(self) -> int:
        return sum(getattr(c, "n_req", 0) for c in list(self._clientes.values()))

    def fechar(self) -> None:
        for c in list(self._clientes.values()):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._clientes.clear()

    # ------------------------------------------------------------------ análise
    def analisar(self, numero: str, cliente: Perfil, anterior: dict | None = None, hoje: date | None = None,
                 forcar_llm: bool = False) -> dict:
        """Resultado da análise (ver `analise.analisar_processo`). Levanta NaoSuportado ou NaoEncontrado."""
        numero = cnjmod.parse(numero).formatado
        sistemas = sistemas_de(numero)
        if not sistemas:
            raise NaoSuportado(f"{tribunal_de(numero) or 'tribunal'}: sem sistema para o radar de bloqueio")
        with perfilmod.usar(cliente), self._trava(tribunal_de(numero) or sistemas[0]):
            return self._rotear(numero, sistemas, anterior, hoje or date.today(), forcar_llm)

    def _uma(self, numero: str, sistema: str, anterior, hoje, forcar_llm, comp: tuple | None = None) -> dict:
        """comp = (sistema, número[, rótulo]) de outro processo analisado no mesmo grupo: continuação
        migrada (padrão) ou cumprimento de sentença apenso citado nos autos."""
        for tentativa in (1, 2):
            try:
                c2 = (self.cliente(comp[0]), *comp[1:]) if comp else None
                s1 = ORIGEM_1G.get(sistema) or (sistema.replace("eproc2g_", "eproc_")
                                                 if sistema.startswith("eproc2g_") else None)
                if not comp and s1:
                    # recurso no eproc 2º grau: o risco está no processo de origem (1º grau)
                    def c2(autos, s1=s1):
                        n = eproc.originario(autos.html)
                        return (self.cliente(s1), n, "ORIGINÁRIO 1º GRAU") if n else None
                return analisar_processo(numero, self.cliente(sistema), self.cache, self.llm, hoje,
                                         anterior=anterior, forcar_llm=forcar_llm, complemento=c2)
            except SessaoExpirada:
                if tentativa == 2:
                    raise
                log.warning("sessão %s caiu; renovando login", sistema)
                self.cliente(sistema, renovar=True)
                if comp:
                    self.cliente(comp[0], renovar=True)
        raise RuntimeError("inalcançável")

    def _apenso_citado(self, r: dict, numero: str, hoje) -> tuple | None:
        """Cumprimento de sentença em autos apartados citado nos documentos (CS eletrônico de processo
        físico, cumprimento provisório, PJe sem aba Associados): abre o número citado e confirma pelas
        partes antes de juntar no grupo."""
        fatos = r.get("fatos") or {}
        if fatos.get("apensos"):
            return None
        d0 = _digitos(numero)
        cands = [c for c in fatos.get("citados") or [] if _digitos(c)[13:] == d0[13:]][:3]  # mesmo tribunal e origem
        for c in cands:
            for s_c in sistemas_de(c):
                try:
                    a = self.cliente(s_c).abrir_autos(cnjmod.parse(c))
                except ProcessoNaoEncontrado:
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: citado %s não abriu no %s: %s", numero, c, s_c, e)
                    continue
                capa_c, _ = _parse(a)
                rot = apenso_confirmado(r.get("capa") or {}, capa_c)
                log.info("%s: citado %s (%s, %s) -> %s", numero, c, s_c, capa_c.classe, rot or "não é apenso")
                # só o cumprimento apenso entra no grupo; o "principal" citado fica como alerta (juntar
                # a ação errada traria sentença/pagamento de outro contrato e poderia baixar o risco)
                if rot and rot.startswith("APENSO"):
                    return (s_c, c, rot)
                break
        return None

    def _rotear(self, numero: str, sistemas: list[str], anterior, hoje, forcar_llm) -> dict:
        r, sistema = None, sistemas[0]
        for k, sistema in enumerate(sistemas):
            try:
                r = self._uma(numero, sistema, anterior, hoje, forcar_llm)
            except ProcessoNaoEncontrado:
                log.info("%s não encontrado no %s", numero, sistema)
                continue
            novos = numeros_migracao(r, numero)
            destino = eproc_1g(sistema) if sistema.startswith("esaj") else None
            if r["fase"] == "PROCESSO MIGRADO" and destino and not novos:
                # a publicação não trouxe o número novo (ex.: "@pNumProcessoMigrado"): procura no
                # eproc pelo número originário
                try:
                    novos = self.cliente(destino).por_originario(numero)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: busca por originário no eproc falhou: %s", numero, e)
            if r["fase"] == "PROCESSO MIGRADO" and destino and novos:
                # migrou com número novo (em geral o incidente de cumprimento): junta os dois
                try:
                    log.info("%s migrado do %s como %s; juntando com o %s", numero, sistema, novos[0], destino)
                    r = self._uma(numero, sistema, anterior, hoje, forcar_llm, (destino, novos[0]))
                    break
                except ProcessoNaoEncontrado:
                    log.info("%s não encontrado no %s", novos[0], destino)
            if r["fase"] == "PROCESSO MIGRADO" and k + 1 < len(sistemas):
                log.info("%s migrado do %s; analisando no %s", numero, sistema, sistemas[k + 1])
                continue
            if sistema.startswith("eproc") and (r["uso"].get("n_eventos") or 99) < 15 \
                    and not (r["checklist"] or {}).get("ha_sentenca"):
                # recém-chegado ao eproc (migração): o histórico está no sistema de origem. Sem isso,
                # o processo parece "sem sentença" e some o risco (inclusive bloqueio já realizado)
                for origem in sistemas[k + 1:]:
                    try:
                        log.info("%s: eproc com %s eventos e sem sentença; juntando com o %s",
                                 numero, r["uso"].get("n_eventos"), origem)
                        r = self._uma(numero, origem, anterior, hoje, forcar_llm, (sistema, numero))
                        break
                    except ProcessoNaoEncontrado:
                        log.info("%s não encontrado no %s", numero, origem)
            break
        if r is None:
            raise NaoEncontrado(f"{numero} não encontrado em {', '.join(sistemas)}")
        if "+" not in (r.get("sistema") or ""):
            comp = self._apenso_citado(r, numero, hoje)
            if comp:
                log.info("%s: juntando apenso citado %s (%s)", numero, comp[1], comp[2])
                try:
                    r = self._uma(numero, sistema, anterior, hoje, forcar_llm, comp)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: grupo com apenso citado falhou (%s); fica a análise isolada", numero, e)
        return r
