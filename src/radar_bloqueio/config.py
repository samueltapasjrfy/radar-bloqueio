"""Configuração do motor (variáveis de ambiente + pasta de trabalho).

O pacote não lê `.env`: quem usa (worker do monorepo, CLI) carrega o ambiente antes.
Tudo o que é gravado em disco fica em `BLOQUEIO_HOME` (padrão: a pasta atual):

    $BLOQUEIO_HOME/extensions/loytrust   extensão Loy (python -m radar_bloqueio.setup_extension)
    $BLOQUEIO_HOME/data/perfil           perfil do Chromium usado só para logar
    $BLOQUEIO_HOME/data/sessao/          cookies de cada sistema (<sistema>.json, .url, .ls.json)
    $BLOQUEIO_HOME/data/docs/            cache do texto dos documentos (imutáveis nos tribunais)
    $BLOQUEIO_HOME/data/logs/            screenshots de diagnóstico do login
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "sim"}


def chrome_unpacked_extension_id(ext_dir: Path) -> str:
    digest = hashlib.sha256(str(ext_dir.resolve()).encode("utf-8")).hexdigest()
    return "".join(chr(ord("a") + int(c, 16)) for c in digest[:32])


def home() -> Path:
    return Path(os.getenv("BLOQUEIO_HOME") or Path.cwd()).resolve()


@dataclass(frozen=True)
class Config:
    loy_email: str
    loy_password: str
    loy_cert: str
    loy_portal: str
    headless: bool
    pje_base_url: str
    root: Path
    ext_dir: Path
    profile_dir: Path
    downloads_dir: Path
    logs_dir: Path
    raw_dir: Path
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    sistema: str = "pje"          # família do sistema da sessão (pje | eproc | esaj | ...)
    eproc_base_url: str = "https://eproc1g.tjmg.jus.br/eproc/"

    @property
    def extension_id(self) -> str:
        return chrome_unpacked_extension_id(self.ext_dir)

    @property
    def popup_url(self) -> str:
        return f"chrome-extension://{self.extension_id}/popup.html"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


def load_config(raiz: Path | None = None) -> Config:
    raiz = (raiz or home()).resolve()
    data = raiz / "data"
    dirs = {k: data / k for k in ("downloads", "logs", "raw", "sessao", "docs")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return Config(
        loy_email=os.getenv("LOY_EMAIL", ""),
        loy_password=os.getenv("LOY_PASSWORD", ""),
        loy_cert=os.getenv("LOY_CERT", "").strip(),
        loy_portal=os.getenv("LOY_PORTAL", "").strip(),
        headless=_bool(os.getenv("HEADLESS"), default=True),
        pje_base_url=os.getenv("PJE_BASE_URL", "https://pje.tjmg.jus.br/pje/"),
        root=raiz,
        # o ID da extensão unpacked deriva deste caminho: mantenha-o fixo
        ext_dir=Path(os.getenv("LOY_EXT_DIR") or raiz / "extensions" / "loytrust"),
        profile_dir=data / os.getenv("BLOQUEIO_PERFIL", "perfil"),
        downloads_dir=dirs["downloads"],
        logs_dir=dirs["logs"],
        raw_dir=dirs["raw"],
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("BLOQUEIO_MODELO") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
        eproc_base_url=os.getenv("EPROC_BASE_URL", "https://eproc1g.tjmg.jus.br/eproc/"),
    )
