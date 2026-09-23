#!/usr/bin/env python3
"""Rasterize / sample scar polygons onto the app 0.001° grid → parquet (+ geojson).

Sparse output: only burned / intersecting cells.
Join keys: lon/lat rounded to 0.001° and dense_index (NOT UTM 100 m).

Geometry priority per burn year:
  1. official (DARPA/ICGC) when scars/official_{year}.geojson exists
  2. sentinel (dNBR) only fills years/areas without official coverage
  3. effis lowest priority; skipped for years that already have official
Official cells are never overwritten by sentinel/effis for the same year.
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
    "severity",  # baixa|moderada|alta|None (Catalan; Sentinel dNBR)
    "dnbr",  # mean dNBR float or None
    "source",
    "confidence",
    "fire_id",
]

# USGS-style approximate classes on raw dNBR (same as map_scars_openeo)
_SEVERITY_RANK = {"alta": 3, "moderada": 2, "baixa": 1}


def severity_from_dnbr(dnbr) -> str | None:
    if dnbr is None:
        return None
    try:
        x = float(dnbr)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    if x < 0.10:
        return None
    if x < 0.27:
        return "baixa"
    if x < 0.44:
        return "moderada"
    return "alta"


def normalize_severity(val) -> str | None:
    """Accept Catalan labels, legacy 0–3 ints, or English synonyms."""
    if val is None:
        return None
    if isinstance(val, float) and val != val:
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        # legacy numeric 0–3
        try:
            i = int(val)
        except (TypeError, ValueError):
            return None
        return {1: "baixa", 2: "moderada", 3: "alta"}.get(i)
    s = str(val).strip().lower()
    if s in ("", "none", "null", "nan", "unknown", "desconeguda", "desconegut"):
        return None
    aliases = {
        "baixa": "baixa",
        "low": "baixa",
        "moderada": "moderada",
        "moderate": "moderada",
        "moderat": "moderada",
        "alta": "alta",
        "high": "alta",
        "high severity": "alta",
    }
    return aliases.get(s)


def severity_from_feature(props: dict, source: str) -> tuple[str | None, float | None]:
    """Return (severity, dnbr) for a scar feature.

    Official/EFFIS: leave null (no dNBR).
    Sentinel: prefer explicit severity / dnbr_mean; else threshold_proxy → moderada
    when the feature is a region-grow scar (core threshold present).
    """
    dnbr = None
    for k in ("dnbr", "dnbr_mean", "mean_dnbr", "dNBR", "DNBR"):
        if k in props and props[k] not in (None, ""):
            try:
                dnbr = float(props[k])
                if dnbr != dnbr:
                    dnbr = None
                else:
                    break
            except (TypeError, ValueError):
                pass
    sev = normalize_severity(props.get("severity"))
    if sev is None and dnbr is not None:
        sev = severity_from_dnbr(dnbr)
    if source == "sentinel" and sev is None:
        # Local backfill without CDSE: scars require core ≥ ~0.35 → moderada band
        if props.get("severity_method") == "threshold_proxy" or props.get(
            "dnbr_threshold_core"
        ) is not None:
            sev = "moderada"
    if source in ("official", "effis", "bombers"):
        # Never invent severity for non-Sentinel sources
        if props.get("severity_method") not in ("dnbr_mean", "dnbr_max") and dnbr is None:
            return None, None
    return sev, dnbr


def better_severity(a: str | None, b: str | None) -> str | None:
    ra = _SEVERITY_RANK.get(a or "", 0)
    rb = _SEVERITY_RANK.get(b or "", 0)
    if ra >= rb:
        return a if ra else b
    return b



def split_disconnected_sentinel_fire_ids(df):
    """Within each (burn_year, fire_id) sentinel group, give each 4-connected
    component on the 0.001° dense grid a distinct fire_id `{old}_{comp}`.

    Official/EFFIS rows are left unchanged. Singleton components keep a `_0`
    suffix only when the group had >1 component (so a lone patch stays bare
    id unless siblings exist — actually always suffix when splitting a group
    that has multiple components; single-component groups keep original id).
    """
    import pandas as pd

    if df is None or df.empty or "fire_id" not in df.columns:
        return df
    nl = nlon()
    out_ids = df["fire_id"].astype(object).copy()
    mask = df["source"].astype(str) == "sentinel"
    if not mask.any():
        return df
    n_groups_split = 0
    n_components = 0
    # Group by year + original fire_id
    grouped = df.loc[mask].groupby(["burn_year", "fire_id"], sort=False)
    for (by, fid), g in grouped:
        if fid is None or (isinstance(fid, float) and fid != fid):
            continue
        idxs = list(g.index)
        dens = g["dense_index"].astype(int).tolist()
        if len(dens) <= 1:
            n_components += 1
            continue
        present = set(dens)
        # BFS 4-neighbour components
        visited: set[int] = set()
        comps: list[list[int]] = []  # lists of dense_index
        for d0 in dens:
            if d0 in visited:
                continue
            stack = [d0]
            visited.add(d0)
            comp = [d0]
            while stack:
                d = stack.pop()
                for nb in (d - 1, d + 1, d - nl, d + nl):
                    if nb in present and nb not in visited:
                        visited.add(nb)
                        stack.append(nb)
                        comp.append(nb)
            comps.append(comp)
        n_components += len(comps)
        if len(comps) <= 1:
            continue
        n_groups_split += 1
        # Map dense -> component ordinal (stable: largest first, then min dense)
        comps_sorted = sorted(comps, key=lambda c: (-len(c), min(c)))
        dense_to_comp = {}
        for ci, comp in enumerate(comps_sorted):
            for d in comp:
                dense_to_comp[d] = ci
        fid_s = str(fid)
        for ix, d in zip(idxs, dens):
            out_ids.at[ix] = f"{fid_s}_{dense_to_comp[int(d)]}"
    df = df.copy()
    df["fire_id"] = out_ids
    print(
        f"Sentinel fire_id CC split: {n_groups_split} multi-component groups → "
        f"{n_components} components (among sentinel cells)",
        flush=True,
    )
    return df


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


def sample_polygon_to_cells(geom, burn_year: int | None, source: str, fire_id: str | None, reference_year: int, severity: str | None = None, dnbr: float | None = None):
    """Cover polygon exterior bbox with 0.001° cells; keep intersecting ones."""
    from shapely.geometry import box
    from shapely.validation import make_valid

    if geom is None or geom.is_empty:
        return []
    if not geom.is_valid:
        try:
            geom = make_valid(geom)
        except Exception:
            try:
                geom = geom.buffer(0)
            except Exception:
                return []
    if geom is None or geom.is_empty:
        return []

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
            try:
                intersects = cell.intersects(geom)
            except Exception:
                intersects = False
            if intersects:
                try:
                    inter = cell.intersection(geom)
                    frac = float(inter.area / cell.area) if cell.area > 0 else 0.0
                except Exception:
                    lat = round_coord(lat + s)
                    continue
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
                        "severity": severity,
                        "dnbr": None if dnbr is None else round(float(dnbr), 4),
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
            "severity": "object",
            "dnbr": "float64",
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


def years_with_official(scar_paths: list[Path]) -> set[int]:
    """Years that have a non-empty official_{year}.geojson."""
    years: set[int] = set()
    for p in scar_paths:
        if not p.name.lower().startswith("official"):
            continue
        if p.stat().st_size <= 20:
            continue
        for part in p.stem.replace("-", "_").split("_"):
            if part.isdigit() and len(part) == 4:
                years.add(int(part))
                break
    return years


def merge_rows(rows: list[dict], official_years: set[int] | None = None) -> list[dict]:
    """Dedupe by dense_index+burn_year; official wins; sentinel gap-fills.

    For years with official coverage:
      - keep all official cells
      - keep sentinel only where dense_index not already official (gap fill)
      - drop effis entirely (noisy when DARPA/ICGC exists)
    """
    official_years = official_years or set()
    rank = {"official": 3, "bombers": 3, "sentinel": 2, "effis": 1}

    # First pass: collect official cell keys per year
    official_keys: set[tuple[int, int]] = set()
    for r in rows:
        if r["source"] == "official":
            official_keys.add((r["dense_index"], r["burn_year"]))

    best: dict[tuple[int, int], dict] = {}
    for r in rows:
        src = r["source"]
        by = r["burn_year"]
        key = (r["dense_index"], by)

        if by in official_years:
            if src == "effis":
                continue  # official year → no EFFIS
            if src == "sentinel" and key in official_keys:
                continue  # do not overwrite / duplicate official cells
            if src == "sentinel" and key not in official_keys:
                pass  # gap fill OK

        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        if rank.get(src, 0) > rank.get(cur["source"], 0):
            r = {**r, "frac_burned": max(r["frac_burned"], cur["frac_burned"])}
            # Keep stronger severity / higher dnbr from the displaced row when useful
            r["severity"] = better_severity(r.get("severity"), cur.get("severity"))
            rd, cd = r.get("dnbr"), cur.get("dnbr")
            if rd is None:
                r["dnbr"] = cd
            elif cd is not None:
                r["dnbr"] = max(float(rd), float(cd))
            best[key] = r
        else:
            cur["frac_burned"] = max(cur["frac_burned"], r["frac_burned"])
            cur["severity"] = better_severity(cur.get("severity"), r.get("severity"))
            rd, cd = r.get("dnbr"), cur.get("dnbr")
            if cd is None:
                cur["dnbr"] = rd
            elif rd is not None:
                cur["dnbr"] = max(float(rd), float(cd))
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
    # Prefer combined sentinel_{year}.geojson over per-AOI sentinel_{year}_*.geojson
    combined_sentinel_years = set()
    for pth in scar_paths:
        parts = pth.stem.split("_")
        if len(parts) == 2 and parts[0] == "sentinel" and parts[1].isdigit():
            combined_sentinel_years.add(parts[1])
    if combined_sentinel_years:
        before = len(scar_paths)
        scar_paths = [
            pth
            for pth in scar_paths
            if not (
                pth.stem.startswith("sentinel_")
                and pth.stem.count("_") >= 2
                and pth.stem.split("_")[1] in combined_sentinel_years
            )
        ]
        print(
            f"Skipping per-AOI sentinel files when combined exists "
            f"({before} → {len(scar_paths)} scar files)",
            flush=True,
        )
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
            part_prop = None
            if "part" in row.index and row["part"] not in (None, ""):
                try:
                    part_prop = int(row["part"])
                except (TypeError, ValueError):
                    part_prop = None
            props = {
                k: row[k]
                for k in gdf.columns
                if k not in ("geometry", "_source", "_path", "_burn_year")
            }
            sev, dnbr_v = severity_from_feature(props, source)

            def _already_part_suffixed(fid: str) -> bool:
                """True if fid looks like '{seed}_{part}' (seed may contain underscores)."""
                if "_" not in fid:
                    return False
                tail = fid.rsplit("_", 1)[-1]
                return tail.isdigit()

            for gi, g in enumerate(geoms):
                fid_i = fire_id
                if source == "sentinel":
                    if not _already_part_suffixed(fire_id):
                        if part_prop is not None and len(geoms) == 1:
                            fid_i = f"{fire_id}_{part_prop}"
                        elif part_prop is not None and len(geoms) > 1:
                            fid_i = f"{fire_id}_{part_prop}_{gi}"
                        elif len(geoms) > 1:
                            fid_i = f"{fire_id}_{gi}"
                    elif len(geoms) > 1:
                        fid_i = f"{fire_id}_{gi}"
                all_rows.extend(
                    sample_polygon_to_cells(
                        g, by, source, fid_i, reference_year, severity=sev, dnbr=dnbr_v
                    )
                )

    off_years = years_with_official(scar_paths)
    print(f"Official coverage years (priority): {sorted(off_years) or 'none'}")
    merged = merge_rows(all_rows, official_years=off_years)
    df = pd.DataFrame(merged, columns=COLUMNS) if merged else pd.DataFrame(columns=COLUMNS)
    if not df.empty:
        src_counts = df.groupby(["burn_year", "source"]).size()
        print("Cells by burn_year × source:")
        print(src_counts.to_string())
    if not df.empty:
        # Split disconnected sentinel patches that still share one fire_id
        df = split_disconnected_sentinel_fire_ids(df)
        # Recompute year_minus flags with reference_year
        df["year_minus_1"] = (df["burn_year"] == reference_year - 1).astype(int)
        df["year_minus_2"] = (df["burn_year"] == reference_year - 2).astype(int)
        df = df.sort_values(["burn_year", "dense_index"]).reset_index(drop=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_parquet, index=False)
    print(f"Wrote {len(df)} cells → {out_parquet}")
    if not df.empty:
        sent = df[df["source"] == "sentinel"]
        n_sev = int(sent["severity"].notna().sum()) if len(sent) else 0
        print(
            f"Sentinel cells with severity: {n_sev}/{len(sent)} "
            f"({(100.0 * n_sev / len(sent)) if len(sent) else 0:.1f}%)",
            flush=True,
        )
        if n_sev:
            print(sent["severity"].value_counts(dropna=False).to_string(), flush=True)

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
