#!/usr/bin/env python3
"""Build docs/recent_burns.bin.gz — lightweight index for fireFac / fireSuppress.

RB01 little-endian:
  magic 4s 'RB01'
  uint16 yref
  uint32 n
  n × (uint32 dense_index, uint16 burn_year, uint8 severity)
severity: 0=unknown, 1=baixa, 2=moderada, 3=alta

Keeps last 2 calendar years (yref-1 … yref), newest burn_year per dense_index.
Older cicatrices stay in burned_cells.parquet (Incendis layer only).
Applies the same source policy as the explorer (official ≤2024; sentinel/effis/official ≥2025).
"""
from __future__ import annotations

import argparse
import gzip
import struct
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SEV_CODE = {None: 0, "": 0, "baixa": 1, "moderada": 2, "alta": 3}


def keep_row(year: int, source: str) -> bool:
    s = str(source or "").lower()
    if int(year) <= 2024:
        return s in ("official", "oficial")
    return s in ("effis", "sentinel", "official", "oficial")


def build_recent_burns(
    parquet_path: Path,
    out_path: Path,
    reference_year: int | None = None,
    window_years: int = 2,
) -> dict:
    import pandas as pd

    yref = int(reference_year or date.today().year)
    ymin = yref - (window_years - 1)
    cols = ["dense_index", "burn_year", "severity", "source"]
    df = pd.read_parquet(parquet_path, columns=cols)
    if df.empty:
        records: list[tuple[int, int, int]] = []
    else:
        mask = df.apply(
            lambda r: keep_row(int(r["burn_year"]), r["source"])
            and int(r["burn_year"]) >= ymin
            and int(r["burn_year"]) <= yref,
            axis=1,
        )
        sub = df.loc[mask].copy()
        if sub.empty:
            records = []
        else:
            sub["dense_index"] = sub["dense_index"].astype("int64")
            sub["burn_year"] = sub["burn_year"].astype("int64")
            # newest year wins; within same year prefer higher severity code
            sub["_sev"] = (
                sub["severity"]
                .map(lambda v: SEV_CODE.get(None if v is None or (isinstance(v, float) and v != v) else str(v).strip().lower(), 0))
                .astype("int64")
            )
            sub = sub.sort_values(
                ["dense_index", "burn_year", "_sev"],
                ascending=[True, False, False],
            )
            best = sub.groupby("dense_index", as_index=False).first()
            records = [
                (int(di), int(by), int(sev))
                for di, by, sev in zip(
                    best["dense_index"].tolist(),
                    best["burn_year"].tolist(),
                    best["_sev"].tolist(),
                )
            ]

    raw = bytearray()
    raw += b"RB01"
    raw += struct.pack("<HI", yref & 0xFFFF, len(records))
    for di, by, sev in records:
        raw += struct.pack("<IHB", di & 0xFFFFFFFF, by & 0xFFFF, sev & 0xFF)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wb", compresslevel=9) as gz:
        gz.write(raw)

    return {
        "yref": yref,
        "n": len(records),
        "raw_bytes": len(raw),
        "gz_bytes": out_path.stat().st_size,
        "path": str(out_path),
        "ymin": ymin,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parquet", type=Path, default=DOCS / "burned_cells.parquet")
    ap.add_argument("--out", type=Path, default=DOCS / "recent_burns.bin.gz")
    ap.add_argument("--reference-year", type=int, default=None)
    ap.add_argument("--window-years", type=int, default=2)
    args = ap.parse_args()
    if not args.parquet.exists():
        raise SystemExit(f"missing parquet: {args.parquet}")
    info = build_recent_burns(
        args.parquet, args.out, args.reference_year, args.window_years
    )
    print(
        f"Wrote RB01 n={info['n']} yref={info['yref']} "
        f"window={info['ymin']}..{info['yref']} "
        f"raw={info['raw_bytes']} gz={info['gz_bytes']} → {info['path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
