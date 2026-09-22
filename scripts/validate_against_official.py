#!/usr/bin/env python3
"""Compare Sentinel dNBR scars vs official 2024 perimeters on the app 0.001° grid.

Metrics (cell-level): IoU, precision, recall, F1; area ha official vs sentinel.
Writes docs/validation_2024.json and a short docs/validation_2024.md (Catalan).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from grid_utils import (  # noqa: E402
    cell_bbox,
    ij_to_dense,
    load_grid,
    lonlat_to_ij,
    nlon,
    round_coord,
    step,
)

SCARS_DIR = ROOT / "scars"
DOCS_DIR = ROOT / "docs"
AREA_CRS = "EPSG:25831"


def polygon_to_cells(geom) -> set[int]:
    """Return dense_index set for cells intersecting geom (EPSG:4326)."""
    from shapely.geometry import box

    minx, miny, maxx, maxy = geom.bounds
    s = step()
    minx -= s
    miny -= s
    maxx += s
    maxy += s
    lon0 = round_coord(minx)
    lat0 = round_coord(miny)
    lon1 = round_coord(maxx)
    lat1 = round_coord(maxy)

    cells: set[int] = set()
    lon = lon0
    n_guard = 0
    while lon <= lon1 + 1e-12:
        lat = lat0
        while lat <= lat1 + 1e-12:
            n_guard += 1
            if n_guard > 500_000:
                return cells
            cell = box(*cell_bbox(lon, lat))
            if cell.intersects(geom):
                ii, jj = lonlat_to_ij(lon, lat)
                if ii >= 0 and jj >= 0 and ii < nlon() * 2:
                    cells.add(int(ij_to_dense(ii, jj)))
            lat = round_coord(lat + s)
        lon = round_coord(lon + s)
    return cells


def load_cells(path: Path) -> tuple[set[int], float]:
    """Return (dense_index set, area_ha) for a scar GeoJSON."""
    import geopandas as gpd

    gdf = gpd.read_file(path)
    if gdf.empty:
        return set(), 0.0
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[~gdf.geometry.isna() & ~gdf.geometry.is_empty]
    cells: set[int] = set()
    for geom in gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        parts = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
        for g in parts:
            cells |= polygon_to_cells(g)
    area_ha = float(gdf.to_crs(AREA_CRS).geometry.area.sum() / 10_000.0)
    return cells, area_ha


def metrics(official: set[int], sentinel: set[int]) -> dict:
    tp = len(official & sentinel)
    fp = len(sentinel - official)
    fn = len(official - sentinel)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    union = len(official | sentinel)
    iou = tp / union if union else 0.0
    return {
        "n_cells_official": len(official),
        "n_cells_sentinel": len(sentinel),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "iou": round(iou, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def write_md(path: Path, year: int, m: dict, area_off: float, area_sen: float) -> None:
    text = f"""# Validació Sentinel vs oficial ({year})

Comparació a la malla de l’app (**0.001°**, vegeu `grid/app_grid.json`).

## Mètriques (nivell cel·la)

| Mètrica | Valor |
|---|---|
| IoU | {m['iou']:.4f} |
| Precisió | {m['precision']:.4f} |
| Recall | {m['recall']:.4f} |
| F1 | {m['f1']:.4f} |
| Cel·les oficials | {m['n_cells_official']} |
| Cel·les Sentinel | {m['n_cells_sentinel']} |
| TP / FP / FN | {m['true_positives']} / {m['false_positives']} / {m['false_negatives']} |

## Àrees

| Font | ha (aprox.) |
|---|---|
| Oficial DARPA/ICGC | {area_off:.1f} |
| Sentinel-2 dNBR | {area_sen:.1f} |

> Nota: la validació només té sentit quan existeixen `scars/sentinel_{year}.geojson`
> i `scars/official_{year}.geojson`. El llindar dNBR per defecte és 0.12.
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--sentinel", type=Path, default=None)
    ap.add_argument("--official", type=Path, default=None)
    ap.add_argument("--out-json", type=Path, default=None)
    ap.add_argument("--out-md", type=Path, default=None)
    args = ap.parse_args()

    year = args.year
    sentinel_path = args.sentinel or (SCARS_DIR / f"sentinel_{year}.geojson")
    official_path = args.official or (SCARS_DIR / f"official_{year}.geojson")
    out_json = args.out_json or (DOCS_DIR / f"validation_{year}.json")
    out_md = args.out_md or (DOCS_DIR / f"validation_{year}.md")

    grid = load_grid()
    print(
        f"Validating year={year} on grid step={grid['step_deg']} "
        f"({official_path.name} vs {sentinel_path.name})",
        flush=True,
    )

    if not official_path.exists():
        print(f"Missing official scars: {official_path}", file=sys.stderr)
        return 1
    if not sentinel_path.exists():
        print(
            f"Missing sentinel scars: {sentinel_path} — "
            "run map_scars_openeo.py first.",
            file=sys.stderr,
        )
        # Write stub so CI can still publish something explicit
        stub = {
            "year": year,
            "status": "missing_sentinel",
            "official": str(official_path),
            "sentinel": str(sentinel_path),
            "message": "No hi ha scars Sentinel per validar.",
        }
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(stub, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        out_md.write_text(
            f"# Validació {year}\n\nSense `scars/sentinel_{year}.geojson` — no es pot calcular IoU/F1.\n",
            encoding="utf-8",
        )
        return 0

    try:
        import geopandas  # noqa: F401
    except ImportError as e:
        print(f"geopandas required: {e}", file=sys.stderr)
        return 1

    print("Rasterizing official → cells …", flush=True)
    cells_off, area_off = load_cells(official_path)
    print(f"  official cells={len(cells_off)} area_ha={area_off:.1f}", flush=True)
    print("Rasterizing sentinel → cells …", flush=True)
    cells_sen, area_sen = load_cells(sentinel_path)
    print(f"  sentinel cells={len(cells_sen)} area_ha={area_sen:.1f}", flush=True)

    m = metrics(cells_off, cells_sen)
    result = {
        "year": year,
        "status": "ok",
        "grid": {
            "step_deg": grid["step_deg"],
            "min_lon": grid["min_lon"],
            "min_lat": grid["min_lat"],
            "nlon": grid["nlon"],
        },
        "paths": {
            "official": str(official_path.relative_to(ROOT)),
            "sentinel": str(sentinel_path.relative_to(ROOT)),
        },
        "area_ha": {
            "official": round(area_off, 2),
            "sentinel": round(area_sen, 2),
        },
        "metrics": m,
    }
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_md(out_md, year, m, area_off, area_sen)
    print(f"Wrote {out_json}", flush=True)
    print(f"Wrote {out_md}", flush=True)
    print(
        f"IoU={m['iou']:.4f} P={m['precision']:.4f} R={m['recall']:.4f} F1={m['f1']:.4f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
