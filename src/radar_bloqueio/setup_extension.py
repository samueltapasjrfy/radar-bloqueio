"""Baixa e descompacta a extensão Loy Trust da Chrome Web Store.

Torna o ambiente reproduzível (local e servidor). Idempotente: se a pasta já
existir com o manifest, não baixa de novo (use --force para refazer).

Uso:
    python -m radar_bloqueio.setup_extension [--force]   (instala em $BLOQUEIO_HOME/extensions/loytrust)
"""

from __future__ import annotations

import argparse
import io
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

from .config import home

EXT_ID = "foacbeippeghacocoooplemaihefjfdk"
CRX_PATH = home() / "extensions" / "loytrust.crx"
EXT_DIR = home() / "extensions" / "loytrust"

UPDATE_URL = (
    "https://clients2.google.com/service/update2/crx"
    "?response=redirect&acceptformat=crx2,crx3&prodversion=149.0"
    f"&x=id%3D{EXT_ID}%26installsource%3Dondemand%26uc"
)


def download_crx() -> bytes:
    print(f"Baixando CRX de {EXT_ID} ...")
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if data[:4] != b"Cr24":
        raise RuntimeError("Resposta não é um CRX válido (magic 'Cr24' ausente).")
    CRX_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRX_PATH.write_bytes(data)
    print(f"  CRX salvo em {CRX_PATH} ({len(data)} bytes)")
    return data


def unpack_crx(data: bytes) -> None:
    # CRX3: 'Cr24' + version(4) + header_len(4) + header + zip
    header_len = struct.unpack("<I", data[8:12])[0]
    zip_start = 12 + header_len
    zbytes = data[zip_start:]
    if zbytes[:4] != b"PK\x03\x04":
        raise RuntimeError("Início do ZIP não encontrado no CRX.")
    EXT_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        z.extractall(EXT_DIR)
    print(f"  Extensão descompactada em {EXT_DIR}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebaixa mesmo se já existir")
    args = ap.parse_args()

    manifest = EXT_DIR / "manifest.json"
    if manifest.exists() and not args.force:
        print(f"Extensão já presente em {EXT_DIR} (use --force para refazer).")
        return 0

    data = download_crx()
    unpack_crx(data)
    if not manifest.exists():
        print("ERRO: manifest.json não encontrado após descompactar.", file=sys.stderr)
        return 1
    print("OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
