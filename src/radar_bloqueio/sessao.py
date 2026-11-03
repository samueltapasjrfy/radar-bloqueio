"""Sessões dos tribunais: o navegador (Loy) só entra para logar e exportar cookies.

Um perfil do Chromium por máquina ($BLOQUEIO_HOME/data/perfil) e um login por vez (trava
global): o login dura ~20 s e depois tudo é HTTP. Os cookies de cada sistema ficam em
data/sessao/<sistema>.json (+ .url com a página em que o login pousou, + .ls.json no TJRJ) e são
reaproveitados enquanto o tribunal aceitar. Ao detectar SSO/login, `renovar()` loga de novo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import replace

from playwright.async_api import async_playwright

from .browser import launch_context, verify_extension_loaded
from .config import Config
from .eproc import EprocHttp
from .esaj import EsajHttp
from .projudi import ProjudiHttp
from .projudi_pr import ProjudiPrHttp
from .projudi_go import ProjudiGoHttp
from .tjrj import TjrjHttp
from .tjse import TjseHttp
from .loytrust import login
from .pje_http import PjeHttp, SessaoExpirada, carregar_cookies

log = logging.getLogger(__name__)

PORTAIS = {"pje": "TJMG(PJE) - 1º Grau", "eproc": "TJMG(EPROC) - 1º Grau",
           "pje2g": "TJMG(PJE) - 2º Grau", "eproc2g": "TJMG(EPROC) - 2º Grau",
           "esaj": "TJSP(ESAJ)", "eprocsp": "TJSP(EPROC) - 1º Grau", "eproc2gsp": "TJSP(EPROC) - 2º Grau"}
URLS = {"pje": "https://pje.tjmg.jus.br/pje/", "pje2g": "https://pjerecursal.tjmg.jus.br/pje/",
        "eproc": "https://eproc1g.tjmg.jus.br/eproc/", "eproc2g": "https://eproc2g.tjmg.jus.br/eproc/",
        "eprocsp": "https://eproc1g.tjsp.jus.br/eproc/", "eproc2gsp": "https://eproc2g.tjsp.jus.br/eproc/"}

# Demais TJs: chave "<familia>_<uf>" (pje_rj, pje2g_rj, eproc_rj, eproc2g_rj, esaj_al, ...).
# O portal da Loy segue o padrão "TJRJ(PJE) - 1º Grau"; a URL base sai da página em que o
# login pousa (gravada em data/sessao/<perfil>.url).
CHROME = set(filter(None, os.getenv("SISTEMAS_CHROME", "pje_rn,pje2g_rn,pje_pb,pje2g_pb,pje_trf3,pje2g_trf3").split(",")))
FAMILIAS = ("eproc2g", "eproc", "pje2g", "pje", "esaj", "projudi", "portaladv", "pjerj")


def separar(sistema: str) -> tuple[str, str | None]:
    """'pje2g_rj' -> ('pje2g', 'RJ'); 'eprocsp' -> ('eproc', None)."""
    if "_" in sistema:
        fam, uf = sistema.split("_", 1)
        return fam, uf.upper()
    return next(f for f in FAMILIAS if sistema.startswith(f)), None


def portal_de(sistema: str) -> str:
    if sistema in PORTAIS:
        return PORTAIS[sistema]
    fam, uf = separar(sistema)
    if uf.startswith("TRF"):  # Justiça Federal: "TRF1(PJE) - 1º Grau", "TRF2RJ(EPROC) - 1º Grau"
        return f"{uf}({'EPROC' if fam.startswith('eproc') else 'PJE'}) - {'2' if fam.endswith('2g') else '1'}º Grau"
    if fam == "esaj":
        return f"TJ{uf}(ESAJ)"
    if fam == "projudi":
        return f"TJ{uf}(Projudi)"
    if fam == "portaladv":
        return f"TJ{uf}(PORTALADV)"
    if fam == "pjerj":
        return "TJRJ(PJERJ)"
    nome = "EPROC" if fam.startswith("eproc") else "PJE"
    return f"TJ{uf}({nome}) - {'2' if fam.endswith('2g') else '1'}º Grau"


def base_da_url(familia: str, url: str) -> str:
    """URL base do sistema a partir da página pós-login."""
    low = url.lower()
    if familia == "eproc":
        return url.split("controlador.php")[0]
    if familia == "projudi":
        i = low.find("/projudi/")
        return url[:i + len("/projudi/")]
    if familia == "esaj":
        i = low.find("/esaj/")
        return url[:i + 1] if i >= 0 else url.split("/", 3)[0] + "//" + url.split("/", 3)[2] + "/"
    for marca in ("/painel", "/processo/", "/advogado/", "/login.seam", "/home.seam"):
        i = low.find(marca)
        if i >= 0:
            return url[:i + 1]
    return url.rsplit("/", 1)[0] + "/"


async def _login_exportar(cfg: Config, destino, destino_url) -> None:
    async with async_playwright() as p:
        ctx = await launch_context(p, cfg, profile_dir=str(cfg.profile_dir))
        try:
            await verify_extension_loaded(ctx, cfg)
            ultimo, page = None, None
            for tentativa in range(1, 4):
                try:
                    page = await login(ctx, cfg)
                    break
                except Exception as e:  # Loy/SSO instável: no máximo 3 tentativas
                    ultimo = e
                    log.warning("login %s falhou (t%d): %s", cfg.sistema, tentativa, str(e)[:150])
                    await asyncio.sleep(3)
            if page is None:
                raise RuntimeError(f"login Loy ({cfg.sistema}) falhou 3x: {ultimo}")
            tj = cfg.loy_portal[:4].lower()  # "tjrj": cookies do domínio do tribunal
            # alguns sistemas (Projudi TJAM) deslogam no onbeforeunload ao fechar a janela: desarma antes
            for pg in ctx.pages:
                try:
                    await pg.evaluate("""() => { window.onbeforeunload = null;
                        document.querySelectorAll('frameset,body').forEach(e => { e.onbeforeunload = null;
                            e.removeAttribute('onbeforeunload'); });
                        document.cookie = 'projudiContCookie=9; path=/'; }""")
                except Exception:  # noqa: BLE001
                    pass
            cookies = [c for c in await ctx.cookies()
                       if cfg.sistema in c["domain"] or "pje" in c["domain"] or tj in c["domain"]]
            destino.write_text(json.dumps(cookies, indent=1))
            destino_url.write_text(page.url)
            if cfg.sistema == "pjerj":  # o portal do TJRJ autentica a API por JWT no localStorage
                ls = {}
                for _ in range(0 if any(c["name"] == "TOKENJWT" for c in cookies) else 20):
                    await page.wait_for_timeout(1000)
                    st = await ctx.storage_state()
                    ls = {x["name"]: x["value"] for o in st["origins"] if "www3.tjrj.jus.br" in o["origin"]
                          for x in o["localStorage"]}
                    if ls.get("Auth"):
                        break
                destino.with_suffix(".ls.json").write_text(json.dumps(ls))
        finally:
            await ctx.close()


# Um navegador por vez no perfil (o Chromium trava o user-data-dir) e entre threads do worker.
_LOGIN = threading.Lock()


class Sessao:
    def __init__(self, cfg: Config, sistema: str = "pje"):
        portal = os.getenv(f"LOY_PORTAL_{sistema.upper()}") or portal_de(sistema)
        familia = ("eproc" if sistema.startswith("eproc") else "esaj" if sistema.startswith("esaj")
                   else "projudi" if sistema.startswith("projudi")
                   else "portaladv" if sistema.startswith("portaladv")
                   else "pjerj" if sistema.startswith("pjerj") else "pje")
        self.cfg = replace(cfg, sistema=familia, loy_portal=portal,
                           pje_base_url=URLS["pje2g"] if sistema == "pje2g" else cfg.pje_base_url,
                           eproc_base_url=URLS.get(sistema, cfg.eproc_base_url) if familia == "eproc"
                           else cfg.eproc_base_url)
        self.familia = familia
        self.sistema = sistema
        base = cfg.root / "data" / "sessao"
        base.mkdir(parents=True, exist_ok=True)
        self.arquivo = base / f"{sistema}.json"
        self.arquivo_url = base / f"{sistema}.url"
        self.cliente = None

    def _novo_cliente(self):
        if self.cliente:
            self.cliente.close()
        jar = carregar_cookies(self.arquivo)
        inicio = self.arquivo_url.read_text().strip() if self.arquivo_url.exists() else ""
        generico = "_" in self.sistema
        # assinatura TLS do Chrome só onde o WAF bloqueia o httpx (o curl_cffi perde cookies
        # de mesmo nome em caminhos diferentes, o que derruba o login do e-SAJ)
        chrome = self.sistema in CHROME
        rotulo = self.sistema if generico else None
        if self.familia == "eproc":
            base = inicio.split("controlador.php")[0] if "controlador.php" in inicio else self.cfg.eproc_base_url
            self.cliente = EprocHttp(base, jar, inicio, rotulo=rotulo, chrome=chrome)
        elif self.familia == "pjerj":
            arq_ls = self.arquivo.with_suffix(".ls.json")
            ls = json.loads(arq_ls.read_text()) if arq_ls.exists() else {}
            # desde 25/10/2026 o portal não grava mais o JWT no localStorage; ele vem no cookie TOKENJWT
            jwt = next((c["value"] for c in json.loads(self.arquivo.read_text()) if c["name"] == "TOKENJWT"), "")
            self.cliente = TjrjHttp(jar, ls.get("Auth") or ls.get("SEGSESSIONID") or jwt, rotulo=self.sistema)
        elif self.familia == "portaladv":
            self.cliente = TjseHttp(jar, rotulo=self.sistema, chrome=chrome)
        elif self.familia == "projudi" and separar(self.sistema)[1] == "GO":
            self.cliente = ProjudiGoHttp(jar, rotulo=self.sistema, chrome=chrome)
        elif self.familia == "projudi" and separar(self.sistema)[1] in ("PR", "AM", "RR"):
            self.cliente = ProjudiPrHttp(inicio, jar, rotulo=self.sistema, chrome=chrome)
        elif self.familia == "projudi":
            self.cliente = ProjudiHttp(base_da_url("projudi", inicio), jar, uf=separar(self.sistema)[1],
                                       rotulo=self.sistema, chrome=chrome)
        elif self.familia == "esaj":
            self.cliente = (EsajHttp(jar, base=base_da_url("esaj", inicio), rotulo=self.sistema, chrome=chrome)
                            if generico else EsajHttp(jar))
        else:
            base = base_da_url("pje", inicio) if generico else self.cfg.pje_base_url
            self.cliente = PjeHttp(base, jar, rotulo=rotulo, chrome=chrome)
        return self.cliente

    def obter(self):
        if self.cliente:
            return self.cliente
        if self.arquivo.exists() and ((self.familia != "eproc" and "_" not in self.sistema)
                                      or self.arquivo_url.exists()):
            try:
                cli = self._novo_cliente()
                if cli.sessao_valida():
                    log.info("sessão %s reaproveitada (%s)", self.sistema, self.arquivo.name)
                    return cli
            except (SessaoExpirada, OSError, ValueError) as e:
                log.info("sessão %s salva inválida (%s); logando de novo", self.sistema, str(e)[:80])
        return self.renovar()

    def renovar(self):
        with _LOGIN:
            log.info("logando via Loy (%s)...", self.sistema)
            t = time.perf_counter()
            asyncio.run(_login_exportar(self.cfg, self.arquivo, self.arquivo_url))
        log.info("sessão %s nova em %.1fs", self.sistema, time.perf_counter() - t)
        cli = self._novo_cliente()
        if not cli.sessao_valida():
            raise SessaoExpirada(f"login concluiu mas o {self.sistema} não aceitou a sessão")
        return cli
