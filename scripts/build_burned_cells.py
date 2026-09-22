#!/usr/bin/env python3
"""Rasterize / sample scar polygons onto the app 0.001° grid → parquet (+ geojson).

Sparse output: only burned / intersecting cells.
Join keys: lon/lat rounded to 0.001° and dense_index (NOT UTM 100 m).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
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
OUT_DIR = ROOT / "docs"
COLUMNS = [
    "lon",
    "lat",
    "dense_index",
    "burn_year",
    "year_minus_1",
    "year_minus_2",
    "frac_burned",
    "severity",
    "source",
    "confidence",
    "fire_id",
]


def infer_source(path: Path) -> str:
    name = path.name.lower()
    if name.startswith("official"):
        return "official"
    if name.startswith("effis"):
        return "effis"
    if "dnbr" in name or "sentinel" in name or "openeo" in name:
        return "sentinel"
    if "bombers" in name:
        return "bombers"
    return "sentinel"


def infer_year(path: Path, props: dict) -> int | None:
    for key in ("burn_year", "year", "YEAR", "any", "ANY", "fireyear", "FIREDATE"):
        if key in props and props[key] not in (None, ""):
            try:
                s = str(props[key])
                return int(s[:4])
            except ValueError:
                pass
    # filename …_2023.geojson / official_2024.geojson
    for part in path.stem.replace("-", "_").split("_"):
        if part.isdigit() and len(part) == 4:
            return int(part)
    return None


def confidence_for(source: str) -> str:
    return {
        "official": "high",
        "bombers": "high",
        "sentinel": "medium",
        "effis": "medium",
    }.get(source, "low")


def load_scar_frames(scar_paths: list[Path]):
    import geopandas as gpd

    frames = []
    for p in scar_paths:
        print(f"Reading {p}", flush=True)
        gdf = gpd.read_file(p)
        if gdf.empty:
            continue
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        else:
            gdf = gdf.to_crs("EPSG:4326")
        gdf = gdf[~gdf.geometry.isna() & ~gdf.geometry.is_empty]
        gdf["_source"] = infer_source(p)
        gdf["_path"] = str(p.name)
        # burn year column
        years = []
        for _, row in gdf.iterrows():
            props = {k: row[k] for k in gdf.columns if k not in ("geometry", "_source", "_path")}
            y = infer_year(p, props)
            years.append(y)
        gdf["_burn_year"] = years
        frames.append(gdf)
    return frames


def sample_polygon_to_cells(geom, burn_year: int | None, source: str, fire_id: str | None, reference_year: int):
    """Cover polygon exterior bbox with 0.001° cells; keep intersecting ones."""
    from shapely.geometry import box

    minx, miny, maxx, maxy = geom.bounds
    s = step()
    # Expand slightly so edge cells are included
    minx -= s
    miny -= s
    maxx += s
    maxy += s

    # Iterate candidate cell centres
    lon0 = round_coord(minx)
    lat0 = round_coord(miny)
    lon1 = round_coord(maxx)
    lat1 = round_coord(maxy)

    rows = []
    lon = lon0
    # Guard against infinite loops on bad geometry
    n_guard = 0
    max_cells = 500_000
    while lon <= lon1 + 1e-12:
        lat = lat0
        while lat <= lat1 + 1e-12:
            n_guard += 1
            if n_guard > max_cells:
                print("  warning: cell cap hit for one polygon", flush=True)
                return rows
            cell = box(*cell_bbox(lon, lat))
            if cell.intersects(geom):
                inter = cell.intersection(geom)
                frac = float(inter.area / cell.area) if cell.area > 0 else 0.0
                if frac <= 0:
                    lat = round_coord(lat + s)
                    continue
                ii, jj = lonlat_to_ij(lon, lat)
                # Skip out-of-grid negatives / absurd indices
                if ii < 0 or jj < 0 or ii >= nlon() * 2:
                    lat = round_coord(lat + s)
                    continue
                lon_r = round_coord(lon)
                lat_r = round_coord(lat)
                by = int(burn_year) if burn_year else reference_year
                rows.append(
                    {
                        "lon": lon_r,
                        "lat": lat_r,
                        "dense_index": int(ij_to_dense(ii, jj)),
                        "burn_year": by,
                        "year_minus_1": 1 if by == reference_year - 1 else 0,
                        "year_minus_2": 1 if by == reference_year - 2 else 0,
                        "frac_burned": round(min(1.0, max(0.0, frac)), 4),
                        "severity": None,
                        "source": source,
                        "confidence": confidence_for(source),
                        "fire_id": fire_id,
                    }
                )
            lat = round_coord(lat + s)
        lon = round_coord(lon + s)
    return rows


def empty_parquet(path: Path) -> None:
    import pandas as pd

    df = pd.DataFrame({c: [] for c in COLUMNS})
    # dtypes
    df = df.astype(
        {
            "lon": "float64",
            "lat": "float64",
            "dense_index": "int64",
            "burn_year": "int64",
            "year_minus_1": "int64",
            "year_minus_2": "int64",
            "frac_burned": "float64",
            "severity": "float64",
            "source": "object",
            "confidence": "object",
            "fire_id": "object",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    print(f"Wrote empty schema parquet → {path}")


def empty_geojson(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": []}, f)
        f.write("\n")


def merge_rows(rows: list[dict]) -> list[dict]:
    """Dedupe by dense_index+burn_year; keep max frac and prefer official source."""
    rank = {"official": 3, "bombers": 3, "sentinel": 2, "effis": 1}
    best: dict[tuple[int, int], dict] = {}
    for r in rows:
        key = (r["dense_index"], r["burn_year"])
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        if rank.get(r["source"], 0) > rank.get(cur["source"], 0):
            r = {**r, "frac_burned": max(r["frac_burned"], cur["frac_burned"])}
            best[key] = r
        else:
            cur["frac_burned"] = max(cur["frac_burned"], r["frac_burned"])
    return list(best.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scars-dir", type=Path, default=SCARS_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--reference-year", type=int, default=None)
    ap.add_argument("--geojson-limit", type=int, default=5000, help="Max points in summary GeoJSON")
    args = ap.parse_args()

    reference_year = args.reference_year or date.today().year
    grid = load_grid()
    print(f"App grid step={grid['step_deg']} min_lon={grid['min_lon']} min_lat={grid['min_lat']} nlon={grid['nlon']}")
    print(f"reference_year={reference_year}")

    scar_paths = sorted(args.scars_dir.glob("*.geojson"))
    out_parquet = args.out_dir / "burned_cells.parquet"
    out_geojson = args.out_dir / "burned_cells.geojson"

    if not scar_paths:
        print("No scars/*.geojson — writing empty outputs with correct schema.")
        empty_parquet(out_parquet)
        empty_geojson(out_geojson)
        return 0

    try:
        import geopandas  # noqa: F401
        import pandas as pd
        from shapely.geometry import mapping
    except ImportError as e:
        print(f"Missing geometry deps ({e}); writing empty schema.", file=sys.stderr)
        empty_parquet(out_parquet)
        empty_geojson(out_geojson)
        return 0

    frames = load_scar_frames(scar_paths)
    all_rows: list[dict] = []
    for gdf in frames:
        for idx, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            # Multipart
            geoms = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
            by = row.get("_burn_year")
            source = row.get("_source", "sentinel")
            fire_id = None
            for k in ("fire_id", "id", "OBJECTID", "fid", "COD"):
                if k in row.index and row[k] not in (None, ""):
                    fire_id = str(row[k])
                    break
            if fire_id is None:
                fire_id = f"{Path(row['_path']).stem}_{idx}"
            for g in geoms:
                all_rows.extend(
                    sample_polygon_to_cells(g, by, source, fire_id, reference_year)
                )

    merged = merge_rows(all_rows)
    df = pd.DataFrame(merged, columns=COLUMNS) if merged else pd.DataFrame(columns=COLUMNS)
    if not df.empty:
        # Recompute year_minus flags with reference_year
        df["year_minus_1"] = (df["burn_year"] == reference_year - 1).astype(int)
        df["year_minus_2"] = (df["burn_year"] == reference_year - 2).astype(int)
        df = df.sort_values(["burn_year", "dense_index"]).reset_index(drop=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_parquet, index=False)
    print(f"Wrote {len(df)} cells → {out_parquet}")

    # Summary GeoJSON (points)
    features = []
    for _, r in df.head(args.geojson_limit).iterrows():
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(r["lon"]), float(r["lat"])]},
                "properties": {
                    k: (None if (isinstance(r[k], float) and r[k] != r[k]) else r[k])
                    for k in COLUMNS
                    if k not in ("lon", "lat")
                },
            }
        )
    with open(out_geojson, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
        f.write("\n")
    print(f"Wrote {len(features)} summary points → {out_geojson}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
