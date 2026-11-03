"""Linha de comando para analisar processos sem o produto (teste e depuração).

    uv run python -m radar_bloqueio 5004214-45.2025.8.13.0515 --cliente BMG --nome "BANCO BMG" \
        --advogado "RICARDO LOPES GODOY:77167/MG" [--hoje AAAA-MM-DD] [--forcar-llm] [--json saida.json]

Lê `.env` da pasta atual (LOY_*, OPENAI_API_KEY, BLOQUEIO_HOME).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date

from .config import load_config
from .motor import Motor, NaoEncontrado, NaoSuportado
from .perfil import Advogado, Perfil


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(prog="python -m radar_bloqueio")
    ap.add_argument("cnjs", nargs="+")
    ap.add_argument("--cliente", required=True, help="rótulo curto do cliente réu (ex.: BMG)")
    ap.add_argument("--nome", action="append", default=[], help="nome do cliente como aparece nos autos")
    ap.add_argument("--cnpj", action="append", default=[])
    ap.add_argument("--advogado", action="append", default=[], help='"NOME:OAB/UF" do advogado do escritório')
    ap.add_argument("--hoje")
    ap.add_argument("--forcar-llm", action="store_true")
    ap.add_argument("--json", help="grava os resultados neste arquivo")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    perfil = Perfil(nome=args.cliente, nomes=tuple(args.nome), documentos=tuple(args.cnpj),
                    advogados=tuple(Advogado(*(a.split(":", 1) + [""])[:2]) for a in args.advogado))
    motor = Motor(load_config())
    hoje = date.fromisoformat(args.hoje) if args.hoje else date.today()
    saida = []
    for n in args.cnjs:
        try:
            r = motor.analisar(n, perfil, hoje=hoje, forcar_llm=args.forcar_llm)
        except (NaoSuportado, NaoEncontrado) as e:
            print(f"{n}: {type(e).__name__}: {e}")
            continue
        saida.append(r)
        print(f"\n{r['cnj']} ({r['sistema']}) -> {r['grau_risco']} | {r['etapa']} | {r['categoria'] or '-'}")
        for m in r["motivos"]:
            print(f"   - {m}")
        print(f"   ação: {r['acao_sugerida'] or '-'}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(saida, fh, ensure_ascii=False, indent=1, default=str)
    motor.fechar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
