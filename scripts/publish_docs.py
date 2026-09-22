#!/usr/bin/env python3
"""Copy latest artifacts into docs/ and refresh manifest.json.

Publishing pattern mirrors mc-dades-acumulades: commit under docs/ for
raw.githubusercontent / jsDelivr / GitHub Pages consumption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
EVENTS = ROOT / "events"
MADRID = ZoneInfo("Europe/Madrid")

SCHEMA_COLUMNS = [
    {"name": "lon", "type": "float", "desc": "WGS84 longitude rounded to 0.001°"},
    {"name": "lat", "type": "float", "desc": "WGS84 latitude rounded to 0.001°"},
    {"name": "dense_index", "type": "int", "desc": "jj * NLON + ii (app dense key)"},
    {"name": "burn_year", "type": "int", "desc": "Calendar year of burn"},
    {"name": "year_minus_1", "type": "int 0/1", "desc": "1 if burn_year == reference_year - 1"},
    {"name": "year_minus_2", "type": "int 0/1", "desc": "1 if burn_year == reference_year - 2"},
    {"name": "frac_burned", "type": "float 0–1", "desc": "Fraction of cell intersecting scar"},
    {"name": "severity", "type": "int 0–3 or null", "desc": "Optional severity class"},
    {"name": "source", "type": "string", "desc": "official|sentinel|effis|bombers"},
    {"name": "confidence", "type": "string", "desc": "low|medium|high"},
    {"name": "fire_id", "type": "string or null", "desc": "Upstream feature id if known"},
]


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parquet_nrows(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        import pyarrow.parquet as pq

        return pq.read_metadata(path).num_rows
    except Exception:
        try:
            import pandas as pd

            return len(pd.read_parquet(path))
        except Exception:
            return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference-year", type=int, default=None)
    args = ap.parse_args()

    from datetime import date

    reference_year = args.reference_year or date.today().year
    DOCS.mkdir(parents=True, exist_ok=True)

    # Ensure placeholder geojson exists
    gj = DOCS / "burned_cells.geojson"
    if not gj.exists():
        gj.write_text(
            json.dumps({"type": "FeatureCollection", "features": []}) + "\n",
            encoding="utf-8",
        )

    # Copy event summary into docs for easy CDN access (small)
    summary_src = EVENTS / "gencat_summary.json"
    if summary_src.exists():
        shutil.copy2(summary_src, DOCS / "gencat_summary.json")

    now_utc = datetime.now(timezone.utc)
    now_madrid = now_utc.astimezone(MADRID)
    parquet = DOCS / "burned_cells.parquet"
    n_cells = parquet_nrows(parquet)

    grid_path = ROOT / "grid" / "app_grid.json"
    with open(grid_path, encoding="utf-8") as f:
        grid = json.load(f)

    manifest = {
        "version": 1,
        "product": "cat-cicatrius",
        "description": "Cicatrius d'incendis forestals alineades a la malla 0.001° de Bolets Explorador",
        "last_run_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_run_europe_madrid": now_madrid.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "reference_year": reference_year,
        "grid": {
            "crs": grid.get("crs"),
            "step_deg": grid.get("step_deg"),
            "min_lon": grid.get("min_lon"),
            "min_lat": grid.get("min_lat"),
            "nlon": grid.get("nlon"),
        },
        "files": {
            "burned_cells.parquet": {
                "path": "docs/burned_cells.parquet",
                "sha256": file_sha256(parquet),
                "n_rows": n_cells,
            },
            "burned_cells.geojson": {
                "path": "docs/burned_cells.geojson",
                "sha256": file_sha256(gj),
                "note": "Summary points (may be capped); prefer parquet for full sparse set",
            },
        },
        "schema": SCHEMA_COLUMNS,
        "attribution": [
            "Gencat / Transparència Catalunya (taules d'incendis)",
            "DARPA / ICGC (perímetres oficials incendisYY.zip)",
            "EFFIS / Copernicus EMS (WFS burn areas, opcional)",
            "CDSE / Sentinel-2 (dNBR openEO, quan hi ha credencials)",
        ],
        "consume": {
            "github_pages": "https://jnoya99.github.io/cat-cicatrius/burned_cells.parquet",
            "raw": "https://raw.githubusercontent.com/jnoya99/cat-cicatrius/main/docs/burned_cells.parquet",
            "jsdelivr": "https://cdn.jsdelivr.net/gh/jnoya99/cat-cicatrius@main/docs/burned_cells.parquet",
        },
        "schedule_note": "Cron 0 6 1 11,2 * UTC ≈ 07:00 / 08:00 Europe/Madrid el 1 Nov i 1 Feb (no diari FIRMS)",
    }

    man_path = DOCS / "manifest.json"
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Updated {man_path} (n_cells={n_cells})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
