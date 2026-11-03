"""Fluxo de login no PJe TJMG via popup da extensão Loy Trust.

Etapas (conforme prints da extensão v1.5.57):
  1. Abrir popup.html da extensão.
  2. Se a tela de login estiver presente (campo #username), logar com email+senha.
  3. Selecionar Certificado e Portal (react-select) — ou usar os pré-selecionados.
  4. Clicar em "Acessar com Loy Trust" e capturar a aba do PJe já logado.
  5. Verificar que chegamos ao Painel do Advogado.
"""

from __future__ import annotations

import logging
import os

from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from .config import Config

log = logging.getLogger(__name__)


async def _shot(page: Page, cfg: Config, name: str) -> None:
    """Salva um screenshot de diagnóstico em data/logs."""
    try:
        path = cfg.logs_dir / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("screenshot: %s", path)
    except Exception as e:  # pragma: no cover - diagnóstico best-effort
        log.debug("falha ao salvar screenshot %s: %s", name, e)


async def open_popup(context: BrowserContext, cfg: Config) -> Page:
    """Abre o popup da extensão como uma aba comum e devolve a Page."""
    page = await context.new_page()
    await page.goto(cfg.popup_url, wait_until="domcontentloaded")
    # A SPA React monta o body em runtime; espera algum conteúdo aparecer.
    await page.wait_for_timeout(800)
    log.info("Popup aberto: %s", cfg.popup_url)
    return page


async def is_login_form_present(popup: Page) -> bool:
    """True se o campo de e-mail (#username) estiver visível."""
    try:
        return await popup.locator("#username").is_visible(timeout=4000)
    except PWTimeout:
        return False
    except Exception:
        return False


async def do_login(popup: Page, cfg: Config) -> None:
    """Preenche email + senha, marca 'Lembrar informações' e clica em Entrar."""
    log.info("Tela de login detectada — autenticando no Loy Trust.")
    await popup.locator("#username").fill(cfg.loy_email)
    await popup.locator("#password").fill(cfg.loy_password)

    # Marca "Lembrar informações" para a sessão persistir no perfil.
    try:
        remember = popup.get_by_text("Lembrar informações", exact=False)
        if await remember.count() > 0:
            await remember.click()
    except Exception as e:
        log.debug("não consegui marcar 'Lembrar informações': %s", e)

    await _shot(popup, cfg, "01_login_preenchido")

    # Botão principal "Entrar" (exato, para não pegar "Entrar com Google" etc.)
    entrar = popup.get_by_role("button", name="Entrar", exact=True)
    if await entrar.count() == 0:
        entrar = popup.get_by_text("Entrar", exact=True)
    await entrar.first.click()

    # Aguarda sair da tela de login (campo username some) ou aparecer o passo do certificado.
    try:
        await popup.locator("#username").wait_for(state="hidden", timeout=20000)
    except PWTimeout:
        log.warning("Campo de login ainda visível após 'Entrar' — "
                    "possível erro de credencial ou etapa de OTP.")
    await _shot(popup, cfg, "02_pos_login")


async def set_react_select(popup: Page, label: str, value: str, cfg: Config) -> None:
    """Define o valor de um react-select identificado pelo seu rótulo.

    Idempotente: se o valor exibido já contém `value`, não faz nada.
    """
    if not value:
        log.info("Sem valor configurado para '%s' — usando o pré-selecionado.", label)
        return

    # Container do react-select logo após o rótulo.
    label_el = popup.get_by_text(label, exact=True).first
    control = label_el.locator(
        "xpath=following::*[contains(@class,'control')][1]"
    )

    # Casamento por TOKENS: a opção certa contém TODOS os termos de `value`
    # (ignora tipo de traço e "º/°"). Desambigua casos como "TJRJ(PJE) - 1º Grau"
    # vs "...2º Grau" — passar a opção parcial pegava a errada.
    import re as _re

    def _norm(s: str) -> str:
        return (s or "").lower().replace("º", "").replace("°", "").replace("ª", "")

    tokens = [t for t in _re.split(r"[\s–—\-]+", value) if t.strip()]
    norm_tokens = [_norm(t) for t in tokens]

    # Já está com o valor desejado? (todos os tokens presentes)
    try:
        current = (await control.inner_text(timeout=2000)).strip()
        if norm_tokens and all(t in _norm(current) for t in norm_tokens):
            log.info("'%s' já está em '%s' — ok.", label, current)
            return
    except Exception:
        pass

    log.info("Selecionando '%s' = '%s' (tokens=%s)", label, value, tokens)
    await control.click()
    # react-select é isSearchable: digitar o 1º token FILTRA (sem depender do traço).
    await popup.keyboard.type(tokens[0] if tokens else value, delay=20)
    await popup.wait_for_timeout(500)

    # Escolhe a opção cujo texto contém TODOS os tokens.
    opts = popup.locator("[role='option']")
    n = await opts.count()
    escolhido = None
    for i in range(n):
        try:
            txt = _norm((await opts.nth(i).inner_text()).strip())
        except Exception:
            continue
        if all(t in txt for t in norm_tokens):
            escolhido = opts.nth(i)
            break
    if escolhido is not None:
        await escolhido.click()
    elif n > 0:
        log.warning("Nenhuma opção casou TODOS os tokens de '%s'; usando a 1ª filtrada.", value)
        await opts.first.click()
    else:
        await popup.keyboard.press("Enter")


# Sistema-alvo: "pje" (padrão) ou "eproc". Define o trecho de URL do portal e os textos
# que só existem na área logada.
SISTEMA_URL = {"pje": "pje", "eproc": "eproc", "esaj": "esaj", "projudi": "projudi", "portaladv": "portaladv", "pjerj": "portalservicos"}
# e-SAJ: a URL é a mesma logado ou não; o sinal é o link de logout do cabeçalho
# (ideia do robô esaj-sp). Telas de login nunca contam como sucesso.
SISTEMA_LOGADO = {"pje": ("painel",), "eproc": ("acao=painel", "acao=principal"), "esaj": (),
                  "projudi": ("centroadvogado", "/projudi/usuario", "/projudi/advogado", "projudi/autenticacao.do",
                              "projudi/?nocache", "projudi/tjpr/autenticacao.do",  # TJRR (fork TJPR)
                              "projudi.tjgo.jus.br/usuario"),  # TJGO: escolha de serventia (OAB)
                  "portaladv": ("portaladv/index",), "pjerj": ("portalservicos",)}
ESAJ_SEL_LOGOUT = '[class="esajLogout"]'
ESAJ_AUTH_MARKERS = ("Painel do advogado", "Peticionamento Eletrônico", "Consultas Processuais", "Sair")
URLS_NAO_AUTENTICADAS = ("sajcas/login", "/login", "logout", "externo_controlador", "entrar_cert")
EPROC_AUTH_MARKERS = ("Painel do Advogado", "Citações/Intimações", "Meus Processos", "Sair",
                      "Consulta Processual", "Processos")
PJE_AUTH_MARKERS = ("Painel do Advogado", "PETICIONAR", "Peticionar",
                    "EXPEDIENTES", "Acesso rápido", "MINHAS PETIÇÕES")


async def access_portal(popup: Page, context: BrowserContext, cfg: Config,
                        tentativas: int = 3) -> Page:
    """Clica em 'Acessar com Loy Trust' e devolve a aba do PJe autenticado.

    O Loy Trust passa por uma página intermediária (get-access.html) que faz o SSO
    por certificado e só então abre o PJe em outra aba. Esse SSO às vezes demora ou
    falha de forma transitória — então fazemos polling em TODAS as abas e, se não
    aparecer aba autenticada, reabrimos o popup e clicamos 'Acessar' de novo.
    """
    await _shot(popup, cfg, "03_pre_acessar")
    for t in range(1, tentativas + 1):
        # Garante que o popup está na tela certa (após um clique ele navega p/ get-access).
        if "popup.html" not in (popup.url or ""):
            await popup.goto(cfg.popup_url, wait_until="domcontentloaded")
            await popup.wait_for_timeout(800)
            await set_react_select(popup, "Certificado", cfg.loy_cert, cfg)
            await set_react_select(popup, "Portal", cfg.loy_portal, cfg)

        try:
            botao = popup.get_by_text("Acessar com Loy Trust", exact=False)
            await botao.first.click(timeout=10000)
        except Exception as e:
            log.warning("Clique em 'Acessar' falhou (tentativa %d): %s", t, e)
            continue

        try:
            return await _wait_for_pje_autenticado(context, cfg, timeout_s=45)
        except RuntimeError:
            log.warning("SSO não concluiu (tentativa %d/%d) — reabrindo popup.",
                        t, tentativas)

    raise RuntimeError("Login no PJe não confirmado após várias tentativas — "
                       "ver screenshots em data/logs.")


def _marcadores(cfg: Config) -> tuple[str, ...]:
    return {"eproc": EPROC_AUTH_MARKERS, "esaj": ESAJ_AUTH_MARKERS}.get(cfg.sistema, PJE_AUTH_MARKERS)


async def _esaj_logout_visivel(pg: Page) -> bool:
    try:
        return await pg.locator(ESAJ_SEL_LOGOUT).count() > 0
    except Exception:
        return False


async def _escolher_perfil_eproc(pg: Page, cfg: Config) -> bool:
    """eproc pós-SSO pode parar em 'entrar_sso' pedindo o perfil (advogado com OAB em mais de
    uma UF). Escolhe a OAB da UF do tribunal (TJSP -> SP..., TJMG -> MG...)."""
    try:
        linhas = pg.locator("[onclick^='acaoLogar']")
        n = await linhas.count()
        if not n:
            return False
        uf = (os.getenv("EPROC_UF_PERFIL") or (cfg.loy_portal[2:4] if cfg.loy_portal.startswith("TJ") else "")).upper()
        alvo = None
        for i in range(n):
            txt = (await linhas.nth(i).inner_text()).strip().upper()
            if uf and txt.startswith(uf):
                alvo = linhas.nth(i)
                break
        alvo = alvo or linhas.first
        log.info("eproc pediu perfil; escolhendo '%s'", (await alvo.inner_text()).strip().replace("\n", " "))
        await alvo.click()
        await pg.wait_for_load_state("domcontentloaded")
        return True
    except Exception as e:
        log.debug("escolha de perfil eproc falhou: %s", e)
        return False


async def _wait_for_pje_autenticado(context: BrowserContext, cfg: Config,
                                    timeout_s: float = 90.0) -> Page:
    """Aguarda (em qualquer aba) uma página do PJe já autenticada (Painel)."""
    import asyncio

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    candidato: Page | None = None

    while loop.time() < deadline:
        for pg in list(context.pages):
            url = pg.url or ""
            low = url.lower()
            # Genérico p/ qualquer instância PJe (TJMG, TJRJ, ...): aba http com 'pje'
            # no host/caminho, que NÃO seja a extensão/SSO/logout.
            if not low.startswith("http"):
                continue
            if cfg.sistema == "eproc" and ("entrar_sso" in low or "entrar_cert" in low):
                if await _escolher_perfil_eproc(pg, cfg):
                    continue
            if "chrome-extension" in low or "get-access" in low \
                    or any(u in low for u in URLS_NAO_AUTENTICADAS) \
                    or "ssologin" in low or "logout" in low:
                continue
            if SISTEMA_URL[cfg.sistema] not in low:
                continue
            candidato = pg
            # Sinal mais forte e tribunal-agnóstico: a URL do PAINEL do advogado só
            # é acessível autenticado (senão redireciona p/ ssoLogin).
            if cfg.sistema == "esaj" and await _esaj_logout_visivel(pg):
                await pg.bring_to_front()
                log.info("e-SAJ autenticado (link de logout) — %s", url.split("?")[0])
                return pg
            if any(k in low for k in SISTEMA_LOGADO[cfg.sistema]):
                await pg.bring_to_front()
                log.info("PJe autenticado (painel por URL) — %s", url.split("?")[0])
                return pg
            for needle in _marcadores(cfg):
                try:
                    if await pg.get_by_text(needle, exact=False).count() > 0:
                        await pg.bring_to_front()
                        log.info("PJe autenticado (aba detectada) — '%s' em %s",
                                 needle, url.split("?")[0])
                        return pg
                except Exception:
                    continue
        await asyncio.sleep(0.5)

    if candidato is not None:
        log.warning("Aba do PJe encontrada mas sem marcador de painel confirmado: %s",
                    candidato.url)
        return candidato
    raise RuntimeError("Nenhuma aba autenticada do PJe apareceu após 'Acessar' "
                       f"({timeout_s:.0f}s). Ver screenshots em data/logs.")


async def is_pje_logged_in(page: Page, cfg: Config, timeout: float = 30.0) -> bool:
    """Verifica se a página está autenticada no PJe (Painel do Advogado)."""
    low = (page.url or "").lower()
    if SISTEMA_URL[cfg.sistema] not in low:
        log.warning("Página não está no domínio do PJe: %s", page.url)
        return False
    # Painel do advogado por URL = autenticado (tribunal-agnóstico).
    if any(u in low for u in URLS_NAO_AUTENTICADAS):
        return False
    if cfg.sistema == "esaj" and await _esaj_logout_visivel(page):
        return True
    if any(k in low for k in SISTEMA_LOGADO[cfg.sistema]):
        log.info("PJe autenticado (painel por URL): %s", page.url.split("?")[0])
        return True
    for needle in _marcadores(cfg):
        try:
            if await page.get_by_text(needle, exact=False).count() > 0:
                log.info("PJe autenticado — encontrado '%s'.", needle)
                return True
        except Exception:
            continue
    log.warning("No domínio do PJe mas sem sinais do Painel. URL=%s", page.url)
    return False


async def conta_ativa(popup: Page) -> str | None:
    """Lê o e-mail da conta Loy logada (exibido no popup), ou None se deslogado."""
    try:
        body = await popup.inner_text("body")
    except Exception:
        return None
    for line in body.splitlines():
        line = line.strip().lower()
        if "@" in line and " " not in line:
            return line
    return None


async def logout_loy(context: BrowserContext, cfg: Config) -> None:
    """Logout forçado: limpa storage (local+sync) e cookies da extensão/sessão."""
    if context.service_workers:
        try:
            await context.service_workers[0].evaluate(
                "() => new Promise(r => chrome.storage.local.clear("
                "() => chrome.storage.sync.clear(r)))"
            )
        except Exception as e:
            log.debug("falha ao limpar storage: %s", e)
    try:
        await context.clear_cookies()
    except Exception as e:
        log.debug("falha ao limpar cookies: %s", e)


async def login(context: BrowserContext, cfg: Config) -> Page:
    """Orquestra o login completo e devolve a Page do PJe autenticado.

    Garante que a conta logada é a configurada (cfg.loy_email): se houver uma
    sessão de OUTRA conta em cache, faz logout e loga na conta certa. As permissões
    (peticionar/intimação) dependem da conta+certificado, então usar a conta errada
    muda o comportamento (ex.: botão de assinar oculto).
    """
    popup = await open_popup(context, cfg)

    if await is_login_form_present(popup):
        await do_login(popup, cfg)
    else:
        ativo = await conta_ativa(popup)
        alvo = (cfg.loy_email or "").strip().lower()
        if alvo and ativo and ativo != alvo:
            log.warning("Conta Loy ativa (%s) ≠ configurada (%s) — trocando de conta.",
                        ativo, alvo)
            await logout_loy(context, cfg)
            await popup.goto(cfg.popup_url, wait_until="domcontentloaded")
            await popup.wait_for_timeout(1200)
            if await is_login_form_present(popup):
                await do_login(popup, cfg)
            else:
                log.warning("Form de login não apareceu após logout — seguindo mesmo assim.")
        else:
            log.info("Sessão do Loy Trust já ativa (conta: %s).", ativo or "?")

    # Confere a conta resultante (best-effort).
    ativo = await conta_ativa(popup)
    if ativo:
        log.info("Conta Loy logada: %s", ativo)

    await set_react_select(popup, "Certificado", cfg.loy_cert, cfg)
    await set_react_select(popup, "Portal", cfg.loy_portal, cfg)

    pje_page = await access_portal(popup, context, cfg)
    ok = await is_pje_logged_in(pje_page, cfg)
    await _shot(pje_page, cfg, "04_pje_painel")
    if not ok:
        raise RuntimeError("Login no PJe não confirmado — ver screenshots em data/logs.")
    log.info("✅ Login no PJe concluído: %s", pje_page.url)
    return pje_page
