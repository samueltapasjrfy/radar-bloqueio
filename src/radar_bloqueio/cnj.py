"""Utilidades para o número único CNJ do processo.

Formato: NNNNNNN-DD.AAAA.J.TR.OOOO
  NNNNNNN sequencial (7) | DD dígito verificador (2) | AAAA ano (4)
  J segmento (1) | TR tribunal (2) | OOOO unidade de origem (4)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NumeroCNJ:
    sequencial: str  # 7
    digito: str      # 2
    ano: str         # 4
    segmento: str    # 1
    tribunal: str    # 2
    origem: str      # 4

    @property
    def formatado(self) -> str:
        return (f"{self.sequencial}-{self.digito}.{self.ano}."
                f"{self.segmento}.{self.tribunal}.{self.origem}")

    @property
    def limpo(self) -> str:
        return f"{self.sequencial}{self.digito}{self.ano}{self.segmento}{self.tribunal}{self.origem}"


def parse(numero: str) -> NumeroCNJ:
    """Aceita o número com ou sem máscara e devolve as partes."""
    digits = re.sub(r"\D", "", numero)
    if len(digits) != 20:
        raise ValueError(
            f"Número CNJ deve ter 20 dígitos; recebi {len(digits)} em {numero!r}"
        )
    return NumeroCNJ(
        sequencial=digits[0:7],
        digito=digits[7:9],
        ano=digits[9:13],
        segmento=digits[13:14],
        tribunal=digits[14:16],
        origem=digits[16:20],
    )
