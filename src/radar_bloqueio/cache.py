"""Cache do texto dos documentos, em disco.

Documento juntado não muda: o texto extraído fica guardado e nunca é baixado de novo. A chave é
(sistema, id do documento): os IDs são numéricos por tribunal e se repetem entre sistemas.
Arquivo por documento, escrita atômica: várias threads do worker usam o mesmo cache sem trava.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


def _sem_nulos(s: str) -> str:
    return (s or "").replace("\x00", "")


class CacheDocs:
    def __init__(self, pasta: Path):
        self.pasta = pasta
        self.pasta.mkdir(parents=True, exist_ok=True)

    def _arquivo(self, sistema: str, doc_id: str) -> Path:
        fam = re.sub(r"[^a-z0-9_]", "", (sistema or "x").lower().split("+")[0]) or "x"
        h = hashlib.sha1(f"{fam}:{doc_id}".encode()).hexdigest()
        return self.pasta / fam / h[:2] / f"{h}.json"

    def obter(self, sistema: str, doc_id: str) -> dict | None:
        try:
            return json.loads(self._arquivo(sistema, doc_id).read_text())
        except (OSError, ValueError):
            return None

    def varios(self, sistema: str, ids: list[str]) -> dict[str, dict]:
        out = {}
        for i in ids:
            c = self.obter(sistema, i)
            if c is not None:
                out[i] = c
        return out

    def salvar(self, sistema: str, doc_id: str, *, texto: str, juntado_por: str = "", paginas: int = 0,
               tipo: str = "", titulo: str = "", content_type: str = "") -> None:
        arq = self._arquivo(sistema, doc_id)
        arq.parent.mkdir(parents=True, exist_ok=True)
        tmp = arq.with_suffix(f".{os.getpid()}.{id(arq)}.tmp")
        tmp.write_text(json.dumps({"doc_id": doc_id, "texto": _sem_nulos(texto), "juntado_por": juntado_por or "",
                                   "paginas": paginas or 0, "tipo": tipo, "titulo": titulo,
                                   "content_type": content_type}, ensure_ascii=False))
        os.replace(tmp, arq)
