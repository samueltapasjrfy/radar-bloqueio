#!/usr/bin/env python3
"""Regressão contra o piloto (bmg-block-service): o motor generalizado classifica igual?

Para cada processo já analisado no Postgres do piloto, reconstrói capa + timeline do banco,
reaplica os sinais dos documentos lidos (cache do piloto) e roda as regras do piloto e as
deste pacote sobre o MESMO checklist do LLM (chaves _bmg -> _reu). Sem tribunal e sem LLM.

    ~/Documents/bmg-block-service/.venv/bin/python scripts/regressao_piloto.py [--limite N]

PG_DSN (padrão postgresql:///bmg_block) e PILOTO (padrão ~/Documents/bmg-block-service).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import date
from pathlib import Path

PILOTO = Path(os.getenv("PILOTO", Path.home() / "Documents" / "bmg-block-service"))
sys.path.insert(0, str(PILOTO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from bmg_block import analise as a_old, fatos as f_old, risco as r_old, timeline as t_old  # noqa: E402
from radar_bloqueio import analise as a_new, fatos as f_new, perfil, risco as r_new, timeline as t_new  # noqa: E402

CHAVES = {"condenacao_pecuniaria_bmg": "condenacao_pecuniaria_reu", "intimacao_bmg_para_pagar": "intimacao_reu_para_pagar",
          "pendencias_bmg": "pendencias_reu"}
VALORES = {"reformada_favor_bmg": "reformada_favor_reu", "reformada_contra_bmg": "reformada_contra_reu",
           "julgada_favor_bmg": "julgada_favor_reu", "julgada_contra_bmg": "julgada_contra_reu"}
BMG = perfil.Perfil(nome="BMG", nomes=("BANCO BMG",), advogados=(perfil.Advogado("RICARDO LOPES GODOY", "77167/MG"),
                                                                  perfil.Advogado("", "321781")))


def checklist_novo(c: dict) -> dict:
    return {CHAVES.get(k, k): (VALORES.get(v, v) if isinstance(v, str) else v) for k, v in c.items()}


def montar(mod, capa_json: dict, linhas) -> tuple:
    campos = {k: v for k, v in capa_json.items() if k in mod.Capa.__dataclass_fields__}
    capa = mod.Capa(**campos)
    eventos = []
    for seq, data, hora, polo, movs, docs in linhas:
        ds = [mod.Documento(**{k: v for k, v in d.items() if k in mod.Documento.__dataclass_fields__})
              for d in (docs if isinstance(docs, list) else json.loads(docs or "[]"))]
        eventos.append(mod.Evento(seq=seq, data=data, hora=hora or "", polo=polo, movimentos=list(movs or []),
                                  documentos=ds))
    return capa, eventos


def classificar(a_mod, f_mod, r_mod, t_mod, capa_json, linhas, checklist, hoje, textos, sistema=""):
    capa, eventos = montar(t_mod, capa_json, linhas)
    f = f_mod.extrair(capa, eventos)
    # como na análise ao vivo: grupo juntado ou processo no eproc = a migração já está na timeline
    if "+" in (sistema or "") or any(x.startswith("eproc") for x in (sistema or "").split("+")):
        f.migracoes = []
    for d, *_ in a_mod.selecionar(f, eventos):
        f_mod.aplicar_sinais(f, d, textos.get(d.doc_id, ""))
    return r_mod.classificar(checklist, f, hoje)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int)
    args = ap.parse_args()
    conn = psycopg.connect(os.getenv("PG_DSN", "postgresql:///bmg_block"))
    sql = """SELECT DISTINCT ON (a.cnj) a.cnj, a.data_analise, coalesce(a.checklist_llm, a.checklist), a.grau_risco,
                    p.capa, a.sistema
               FROM analises a JOIN processos p USING (cnj)
              WHERE a.erro IS NULL AND coalesce(a.checklist_llm, a.checklist) IS NOT NULL
              ORDER BY a.cnj, a.criado_em DESC"""
    linhas = conn.execute(sql).fetchall()[: args.limite or None]
    iguais, difs, erros = 0, [], collections.Counter()
    for cnj, dia, chk, _grau_gravado, capa_json, sistema in linhas:
        chk = chk if isinstance(chk, dict) else json.loads(chk)
        capa_json = capa_json if isinstance(capa_json, dict) else json.loads(capa_json or "{}")
        evs = conn.execute("SELECT seq, data, hora, polo, movimentos, documentos FROM eventos WHERE cnj=%s ORDER BY seq",
                           (cnj,)).fetchall()
        if not evs:
            erros["sem eventos"] += 1
            continue
        ids = list({d["doc_id"] for *_, docs in evs for d in (docs or [])})
        textos = dict(conn.execute("SELECT doc_id, texto FROM documentos WHERE doc_id = ANY(%s)", (ids,)).fetchall())
        hoje = dia or date.today()
        try:
            velho = classificar(a_old, f_old, r_old, t_old, capa_json, evs, chk, hoje, textos, sistema)
            with perfil.usar(BMG):
                novo = classificar(a_new, f_new, r_new, t_new, capa_json, evs, checklist_novo(chk), hoje, textos, sistema)
        except Exception as e:  # noqa: BLE001
            erros[f"{type(e).__name__}: {str(e)[:80]}"] += 1
            continue
        if (velho.grau, velho.ha_risco, velho.fase) == (novo.grau, novo.ha_risco, novo.fase) \
                and len(velho.motivos) == len(novo.motivos):
            iguais += 1
        else:
            difs.append((cnj, (velho.grau, velho.ha_risco, velho.fase, len(velho.motivos)),
                         (novo.grau, novo.ha_risco, novo.fase, len(novo.motivos)), velho.motivos, novo.motivos))
    total = iguais + len(difs)
    print(f"{iguais}/{total} iguais (grau, situação, fase e nº de motivos); {len(difs)} diferentes; erros {dict(erros)}")
    for cnj, v, n, mv, mn in difs[:15]:
        print(f"\n{cnj}\n  piloto: {v}\n  novo:   {n}")
        for x in sorted(set(mv) ^ set(mn))[:6]:
            print(f"    {'-' if x in mv else '+'} {x[:180]}")
    return 0 if not difs else 1


if __name__ == "__main__":
    raise SystemExit(main())
