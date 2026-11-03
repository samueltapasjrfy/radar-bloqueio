"""Radar de bloqueio: risco de bloqueio judicial de valores (SISBAJUD/penhora on-line) contra o
cliente réu, nos TJs e TRFs (PJe, eproc, e-SAJ, Projudi, TJRJ, TJSE)."""

from .config import Config, load_config
from .motor import Motor, NaoEncontrado, NaoSuportado, sistemas_de, tribunal_de
from .perfil import Advogado, Perfil

__all__ = ["Advogado", "Config", "Motor", "NaoEncontrado", "NaoSuportado", "Perfil", "load_config",
           "sistemas_de", "tribunal_de"]
