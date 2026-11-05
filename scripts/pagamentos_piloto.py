#!/usr/bin/env python3
"""Pagamento nos autos para os casos do piloto (carga do sinal sem reprocessar no tribunal).

Reconstrói fatos + sinais de cada processo analisado no Postgres do piloto (como exportar_piloto.py)
e grava uma linha por processo: {"numero", "pagamento": {...} | null}.

    ~/Documents/bmg-block-service/.venv/bin/python scripts/pagamentos_piloto.py <saida.jsonl>
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

from radar_bloqueio import analise, fatos, perfil, risco, timeline  # noqa: E402
from regressao_piloto import BMG, checklist_novo, montar  # noqa: E402


def main() -> int:
    conn = psycopg.connect(os.getenv("PG_DSN", "postgresql:///bmg_block"))
    linhas = conn.execute(
        """SELECT r.cnj, r.grau, a.sistema, p.capa, coalesce(a.checklist_llm, a.checklist), a.data_analise FROM radar.resultado r
             JOIN analises a ON a.id = r.analise_id JOIN processos p ON p.cnj = r.cnj
            WHERE r.analisado ORDER BY r.cnj""").fetchall()
    conta = collections.Counter()
    with open(sys.argv[1], "w") as fh, perfil.usar(BMG):
        for cnj, grau, sistema, capa_json, chk, dia in linhas:
            evs = conn.execute("SELECT seq, data, hora, polo, movimentos, documentos FROM eventos WHERE cnj=%s ORDER BY seq",
                               (cnj,)).fetchall()
            if not evs:
                continue
            capa, eventos = montar(timeline, capa_json if isinstance(capa_json, dict) else json.loads(capa_json or "{}"), evs)
            ids = list({d.doc_id for e in eventos for d in e.documentos})
            textos = dict(conn.execute("SELECT doc_id, texto FROM documentos WHERE doc_id = ANY(%s)", (ids,)).fetchall())
            f = fatos.extrair(capa, eventos)
            if "+" in (sistema or "") or any(x.startswith("eproc") for x in (sistema or "").split("+")):
                f.migracoes = []
            for d, *_ in analise.selecionar(f, eventos):
                fatos.aplicar_sinais(f, d, textos.get(d.doc_id, ""))
            chk = chk if isinstance(chk, dict) else json.loads(chk or "{}")
            r = risco.classificar(checklist_novo(chk), f, dia)
            p = fatos.pagamento_nos_autos(f, r.checklist, textos)
            conta[(grau, bool(p))] += 1
            fh.write(json.dumps({"numero": re.sub(r"\D", "", cnj), "pagamento": p}, ensure_ascii=False) + "\n")
    for g in ("BLOQUEIO JÁ REALIZADO", "ALTO", "MÉDIO", "BAIXO", "SEM RISCO", "INDETERMINADO"):
        com, sem = conta[(g, True)], conta[(g, False)]
        if com + sem:
            print(f"{g:22} {com:5} com pagamento nos autos de {com + sem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
