"""Quem é o cliente (a parte ré que defendemos) e quais advogados do escritório atuam por ele.

O motor nasceu para um cliente só; os leitores de cada sistema, as regras e o prompt
perguntam aqui "este nome é o cliente?". O perfil da análise em curso fica num ContextVar:
cada thread do worker analisa um processo por vez e define o seu.

Regra de fundo (vale para todo o motor): o cliente é o polo PASSIVO (réu/executado). Cliente
autor exigiria outro conjunto de regras, não só outro parâmetro.
"""

from __future__ import annotations

import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).lower().strip()


@dataclass(frozen=True)
class Advogado:
    nome: str
    oab: str = ""          # "77167/MG", "77167-A/MG" ou só os dígitos

    @property
    def oab_digitos(self) -> str:
        return re.sub(r"\D", "", self.oab.split("/")[0])


@dataclass(frozen=True)
class Perfil:
    nome: str                                   # rótulo curto ("BMG", "SABESP")
    nomes: tuple[str, ...] = ()                 # como aparece nos autos ("BANCO BMG", ...)
    documentos: tuple[str, ...] = ()            # CNPJs (só dígitos)
    advogados: tuple[Advogado, ...] = field(default_factory=tuple)

    @property
    def termos(self) -> list[str]:
        """Nomes normalizados, do mais longo ao mais curto (o rótulo curto também conta)."""
        t = {_norm(n) for n in (self.nome, *self.nomes) if n and len(_norm(n)) >= 3}
        return sorted(t, key=len, reverse=True)

    @property
    def regex_nomes(self) -> str:
        """Alternativa regex (texto normalizado) com os nomes do cliente."""
        return "|".join(re.escape(t) for t in self.termos) or r"(?!x)x"

    @property
    def regex_advogados(self) -> str:
        """Nomes e números de OAB dos advogados do escritório (texto normalizado)."""
        partes = [re.escape(_norm(a.nome)) for a in self.advogados if a.nome]
        for a in self.advogados:
            d = a.oab_digitos
            if len(d) >= 4:  # 77167 -> 77\.?167 (a OAB aparece com e sem ponto)
                partes.append(re.escape(d[:-3]) + r"\.?" + re.escape(d[-3:]))
        return "|".join(partes) or r"(?!x)x"

    def eh_cliente(self, texto: str) -> bool:
        """O nome/linha de parte é do cliente? (por nome ou CNPJ)"""
        t = _norm(texto)
        if any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", t) for n in self.termos):
            return True
        digitos = re.sub(r"\D", "", texto or "")
        return any(d and d in digitos for d in self.documentos)

    def advogado_no_texto(self, texto: str) -> bool:
        t = _norm(texto)
        return any(_norm(a.nome) in t for a in self.advogados if a.nome)

    @property
    def nomes_advogados(self) -> str:
        return ", ".join(a.nome for a in self.advogados if a.nome)


_ATUAL: ContextVar[Perfil | None] = ContextVar("perfil_cliente", default=None)


def atual() -> Perfil:
    p = _ATUAL.get()
    if p is None:
        raise RuntimeError("perfil do cliente não definido (use radar_bloqueio.perfil.usar)")
    return p


@contextmanager
def usar(p: Perfil):
    token = _ATUAL.set(p)
    try:
        yield p
    finally:
        _ATUAL.reset(token)


def eh_cliente(texto: str) -> bool:
    return atual().eh_cliente(texto)
