"""Extração de texto de documentos do PJe (HTML do editor ou PDF anexado)."""

from __future__ import annotations

import re

import pymupdf as fitz
from selectolax.lexbor import LexborHTMLParser


def extrair_texto(content_type: str, dados: bytes, max_paginas: int = 30) -> tuple[str, int]:
    """Devolve (texto, n_paginas). PDF escaneado sem camada de texto volta vazio."""
    if dados[:4] == b"%PDF" or "pdf" in content_type:
        try:
            with fitz.open(stream=dados, filetype="pdf") as pdf:
                n = pdf.page_count
                partes = [pdf[i].get_text() for i in range(min(n, max_paginas))]
            return _limpar("\n".join(partes)), n
        except Exception:
            return "", 0
    # eproc e Projudi servem ISO-8859-1: decodificar como UTF-8 apagava as letras acentuadas
    cs = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    try:
        html = dados.decode(cs.group(1) if cs else "utf-8")
    except (UnicodeDecodeError, LookupError):
        html = dados.decode("latin-1")
    t = LexborHTMLParser(html)
    for s in t.css("script,style"):
        s.decompose()
    corpo = t.body.text(separator="\n") if t.body else html
    return _limpar(corpo), 1


def _limpar(s: str) -> str:
    s = s.replace("\x00", "").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()
