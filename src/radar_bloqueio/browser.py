"""Inicialização do navegador com a extensão Loy Trust carregada (contexto persistente)."""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright

from .config import Config

log = logging.getLogger(__name__)

# Máscara anti-fingerprint de headless. O WAF do TJRJ BLOQUEIA a tela de peticionar quando
# detecta headless (user-agent "HeadlessChrome" + navigator.webdriver=true) — login passa,
# mas a página JSF de trabalho não renderiza. Com UA de Chrome normal + webdriver oculto, o
# RJ roda headless fim a fim (testado). É TRANSPARENTE para MG/BA (que já funcionavam): não
# muda como as páginas operam, só como o browser se apresenta. Por isso aplicamos sempre.
_REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_STEALTH_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome = window.chrome || {runtime:{}};"
    "Object.defineProperty(navigator,'languages',{get:()=>['pt-BR','pt','en-US','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
)


async def launch_context(p: Playwright, cfg: Config, profile_dir: str | None = None) -> BrowserContext:
    """Abre um contexto persistente do Chromium (for Testing) com a extensão carregada.

    Importante: o Google Chrome ESTÁVEL 149 bloqueia a flag `--load-extension`
    (lockdown iniciado no Chrome 137+), então usamos o Chromium for Testing que o
    Playwright baixa — ele permite carregar extensão unpacked. Como a Loy Trust faz
    a assinatura via nuvem/web (não usa o cofre de certificados do SO), funciona
    igual. O `user_data_dir` persiste cookies + sessão do Loy Trust entre execuções.

    `profile_dir` permite usar um perfil dedicado (ex.: rodar navegadores em paralelo
    sem conflitar no mesmo user-data-dir).
    """
    if not cfg.ext_dir.exists():
        raise FileNotFoundError(
            f"Extensão não encontrada em {cfg.ext_dir}. "
            "Rode antes: python -m radar_bloqueio.setup_extension"
        )

    args = [
        f"--disable-extensions-except={cfg.ext_dir}",
        f"--load-extension={cfg.ext_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    # O headless CLÁSSICO do Chromium não carrega extensões (a Loy não sobe → login
    # falha em popup.html). O NOVO headless (`--headless=new`) suporta extensão, então
    # quando pedem headless usamos headless=False + a flag --headless=new.
    if cfg.headless:
        args.append("--headless=new")

    udd = profile_dir or str(cfg.profile_dir)
    Path(udd).mkdir(parents=True, exist_ok=True)
    context = await p.chromium.launch_persistent_context(
        user_data_dir=udd,
        headless=False,
        args=args,
        # Playwright injeta --disable-extensions por padrão, o que desligaria a
        # Loy Trust; removemos por string exata.
        ignore_default_args=["--disable-extensions"],
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
        downloads_path=str(cfg.downloads_dir),
        user_agent=_REAL_UA,  # UA de Chrome normal (sem "HeadlessChrome") — anti-WAF do RJ
    )
    await context.add_init_script(_STEALTH_JS)  # esconde navigator.webdriver etc.
    log.info("Contexto Chromium aberto (headless=%s). Extension ID: %s",
             cfg.headless, cfg.extension_id)
    return context


async def verify_extension_loaded(context: BrowserContext, cfg: Config, timeout: float = 15.0) -> str:
    """Confirma que a extensão subiu, retornando o ID real do service worker.

    Compara com o ID calculado a partir do caminho; loga aviso se divergir.
    """
    sw = None
    if context.service_workers:
        sw = context.service_workers[0]
    else:
        try:
            sw = await context.wait_for_event("serviceworker", timeout=timeout * 1000)
        except Exception:
            log.warning("Service worker da extensão não detectado em %ss; "
                        "usando ID calculado pelo caminho.", timeout)
            return cfg.extension_id

    real_id = sw.url.split("/")[2]
    if real_id != cfg.extension_id:
        log.warning("ID do service worker (%s) difere do calculado (%s). "
                    "Usando o real.", real_id, cfg.extension_id)
    return real_id
