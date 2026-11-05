#!/usr/bin/env python3
"""Exporta os casos já processados pelo piloto (bmg-block-service) para o produto.

Para cada processo de `radar.resultado` (a carteira consolidada do piloto):
  - analisado: reconstrói capa + timeline do banco do piloto, reaplica os sinais dos documentos lidos
    e reclassifica com as regras DESTE pacote, na data da análise original (mesma reconstrução da
    regressão: 3.112/3.112 iguais). Sai no formato de `analise.analisar_processo` (o que o worker
    devolve), com a assinatura e o checklist da IA: a verificação diária reaproveita a leitura.
  - não analisado: situação (não encontrado / falhou / na fila) e o motivo que o piloto registrou.
E, à parte, o cache de texto dos documentos no formato de `CacheDocs` (para o servidor não baixar
de novo o que o piloto já leu).

    ~/Documents/bmg-block-service/.venv/bin/python scripts/exportar_piloto.py <saida.jsonl> <pasta_cache>

Contém dados de partes: gere numa pasta temporária, suba e apague.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

PILOTO = Path(os.getenv("PILOTO", Path.home() / "Documents" / "bmg-block-service"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

from radar_bloqueio import analise, fatos, perfil, risco, timeline  # noqa: E402
from radar_bloqueio.cache import CacheDocs  # noqa: E402
from regressao_piloto import BMG, checklist_novo, montar  # noqa: E402


def situacao_de(texto: str) -> str:
    t = (texto or "").upper()
    if t.startswith("AGUARDANDO"):
        return "NA_FILA"
    if "ERRO TÉCNICO" in t:
        return "FALHOU"
    return "NAO_ENCONTRADO"


def nivel_do_piloto(grau_visual: str | None) -> str | None:
    """Grau visual do piloto (BLOQUEIO JÁ REALIZADO, ALTO, MÉDIO...) -> nível do produto."""
    g = (grau_visual or "").upper()
    if g.startswith("BLOQUEIO"):
        return "BLOQUEIO"
    return {"ALTO": "ALTO", "MÉDIO": "MEDIO", "BAIXO": "BAIXO", "SEM RISCO": "SEM_RISCO",
            "INDETERMINADO": "INDETERMINADO"}.get(g)


def obj(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def main() -> int:
    saida, pasta_cache = Path(sys.argv[1]), Path(sys.argv[2])
    conn = psycopg.connect(os.getenv("PG_DSN", "postgresql:///bmg_block"))
    linhas = conn.execute(
        """SELECT r.cnj, r.pasta, r.situacao, r.analisado, r.analise_id, r.grau, r.grau_anterior, r.mudou_em,
                  a.data_analise, a.criado_em, a.sistema, a.assinatura, coalesce(a.checklist_llm, a.checklist),
                  a.fatos, a.docs_lidos, p.capa
             FROM radar.resultado r
             LEFT JOIN analises a ON a.id = r.analise_id
             LEFT JOIN processos p ON p.cnj = r.cnj
            ORDER BY r.cnj""").fetchall()
    cache = CacheDocs(pasta_cache)
    n_ok = n_sem = n_pulados = n_docs = 0
    with saida.open("w") as fh, perfil.usar(BMG):
        for (cnj, pasta, sit, analisado, analise_id, grau_vis, grau_ant, mudou_em, dia, criado_em, sistema,
             assinatura, chk, fatos_json, docs_lidos, capa_json) in linhas:
            digitos = re.sub(r"\D", "", cnj or "")
            if len(digitos) != 20 or digitos[13] not in "48":
                n_pulados += 1
                continue
            reg = {"numero": digitos, "pasta": (str(pasta).strip() or None) if pasta else None}
            evs = conn.execute("SELECT seq, data, hora, polo, movimentos, documentos FROM eventos WHERE cnj=%s ORDER BY seq",
                               (cnj,)).fetchall() if analisado else []
            if not analisado or not chk or not evs:
                reg.update({"situacao": situacao_de(sit) if not analisado else "NA_FILA",
                            "mensagem": None if (sit or "").startswith("AGUARDANDO") else (sit or "").capitalize()})
                fh.write(json.dumps(reg, ensure_ascii=False) + "\n")
                n_sem += 1
                continue
            chk_novo = checklist_novo(obj(chk))
            capa_dict = obj(capa_json)
            capa, eventos = montar(timeline, capa_dict, evs)
            ids = list({d.doc_id for e in eventos for d in e.documentos})
            textos = dict(conn.execute("SELECT doc_id, texto FROM documentos WHERE doc_id = ANY(%s)", (ids,)).fetchall())
            f = fatos.extrair(capa, eventos)
            # como na análise ao vivo: grupo juntado ou processo no eproc = a migração já está na timeline
            if "+" in (sistema or "") or any(x.startswith("eproc") for x in (sistema or "").split("+")):
                f.migracoes = []
            for d, *_ in analise.selecionar(f, eventos):
                fatos.aplicar_sinais(f, d, textos.get(d.doc_id, ""))
            r = risco.classificar(chk_novo, f, dia)
            fj = obj(fatos_json)
            reg.update({
                "situacao": "ANALISADO",
                "analisadoEm": criado_em.isoformat() if criado_em else None,
                "nivelAnterior": nivel_do_piloto(grau_ant) if grau_ant and grau_ant != grau_vis else None,
                "mudouEm": mudou_em.isoformat() if isinstance(mudou_em, (date, datetime)) else None,
                "resultado": {
                    "cnj": cnj, "sistema": sistema, "data_analise": dia.isoformat(), "assinatura": assinatura,
                    "grau_risco": r.grau, "ha_risco": r.ha_risco, "fase": r.fase, "conclusao": r.conclusao,
                    "motivos": r.motivos, "acao_sugerida": r.acao_sugerida,
                    "etapa": risco.etapa(r), "categoria": risco.categoria(r),
                    "bloqueio_realizado": r.ha_risco == "BLOQUEIO JÁ REALIZADO",
                    "checklist": r.checklist, "checklist_llm": chk_novo,
                    "fatos": {"resumo": f.resumo(), "juizado": f.juizado, "advogado_habilitado": f.advogado_habilitado,
                              "vinculos": fj.get("vinculos") or [], "apensos": fj.get("apensos") or [],
                              "citados": fj.get("citados") or []},
                    "docs_lidos": docs_lidos or [], "uso": {"reaproveitado": True, "origem": "piloto"},
                    "capa": capa_dict,
                    "eventos": [analise._evento_saida(e) for e in eventos[:analise.MAX_EVENTOS_SAIDA]],
                    "n_eventos": len(eventos),
                },
            })
            fh.write(json.dumps(reg, ensure_ascii=False, default=str) + "\n")
            n_ok += 1
            # cache: o documento pode ser de qualquer sistema do grupo ("esaj+eprocsp")
            for s in dict.fromkeys((sistema or "").split("+")):
                for doc_id, texto in textos.items():
                    if texto and not texto.startswith("[falha"):
                        cache.salvar(s, doc_id, texto=texto)
                        n_docs += 1
    print(f"{n_ok} analisados, {n_sem} sem análise (com o motivo), {n_pulados} pulados (CNJ inválido ou fora do radar); "
          f"{n_docs} textos no cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
