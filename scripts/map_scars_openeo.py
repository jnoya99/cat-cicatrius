#!/usr/bin/env python3
"""CDSE openEO Sentinel-2 dNBR scars for Catalonia fire AOIs.

Requires OAuth client secrets (do NOT invent credentials; never print them):
  CDSE_CLIENT_ID, CDSE_CLIENT_SECRET — create them in the Sentinel Hub
  Dashboard: https://shapps.dataspace.copernicus.eu/dashboard/

CI auth (non-interactive only):
  authenticate_oidc_client_credentials(client_id, client_secret)

CDSE_USER/CDSE_PASSWORD are not used: an OAuth client is required for
openEO automation.

Pipeline:
  - Build AOIs from scars/official_{year}.geojson and/or scars/effis_{year}.geojson
    (and year-1 when needed, e.g. 2026 season still without official/EFFIS layers).
  - Buffer ~2–3 km, merge overlaps, skip tiny <~5 ha if only points; cap --max-aois.
  - Per AOI: pre/post median S2 L2A (B08,B12,SCL), cloud-mask SCL, NBR, dNBR≥0.12.
  - Batch jobs → GeoTIFF → local polygonize → scars/sentinel_{year}_{id}.geojson
    + combined scars/sentinel_{year}.geojson.

If credentials are missing: exit 0 (seasonal path continues).
Auth failure with credentials present: exit non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCARS_DIR = ROOT / "scars"
TMP_DIR = ROOT / ".tmp" / "openeo"

DNBR_THRESHOLD = 0.12
CDSE_URL = "https://openeo.dataspace.copernicus.eu"
BUFFER_M = 2500.0  # ~2.5 km
BUFFER_M_HUGE = 5000.0  # if seed area > 500 ha
HUGE_HA = 500.0
MIN_POINT_HA = 5.0  # skip tiny point-only seeds
AREA_CRS = "EPSG:25831"  # Catalonia UTM 31N
SCL_CLOUD = {3, 8, 9, 10}  # cloud shadow, cloud med/high, cirrus


@dataclass
class Aoi:
    aoi_id: str
    geometry: Any  # shapely geom in EPSG:4326
    area_ha: float
    fire_date: date | None
    source: str
    seed_year: int


def _client_credentials() -> tuple[str, str] | None:
    """Return a complete OAuth client credential pair, if configured."""
    for id_name, secret_name in (
        ("CDSE_CLIENT_ID", "CDSE_CLIENT_SECRET"),
        ("OPENEO_AUTH_CLIENT_ID", "OPENEO_AUTH_CLIENT_SECRET"),
    ):
        client_id = os.environ.get(id_name)
        client_secret = os.environ.get(secret_name)
        if client_id and client_secret:
            return client_id, client_secret
    return None


def credentials_present() -> bool:
    return _client_credentials() is not None


def skip(msg: str) -> int:
    print(f"[map_scars_openeo] SKIP: {msg}", flush=True)
    return 0


def fail_auth(msg: str) -> int:
    print(f"[map_scars_openeo] AUTH ERROR: {msg}", file=sys.stderr, flush=True)
    return 2


def parse_fire_date(props: dict) -> date | None:
    """Parse common official/EFFIS date fields."""
    candidates = []
    for key in (
        "DATA_INCEN",
        "FIREDATE",
        "FINALDATE",
        "fire_date",
        "date",
        "DATE",
        "data_incendi",
    ):
        if key in props and props[key] not in (None, ""):
            candidates.append(str(props[key]).strip())
    for raw in candidates:
        # ISO-ish
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        # DD/MM/YYYY
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                pass
    return None


def seasonal_windows(year: int) -> tuple[tuple[str, str], tuple[str, str]]:
    """Default summer-fire windows when fire date unknown."""
    pre = (f"{year}-04-01", f"{year}-06-15")
    post = (f"{year}-08-15", f"{year}-10-15")
    return pre, post


def windows_for_fire(fire_date: date | None, year: int) -> tuple[tuple[str, str], tuple[str, str]]:
    if fire_date is None:
        return seasonal_windows(year)
    # Clamp seasonally odd fires still into year windows with sensible offsets
    pre_end = fire_date - timedelta(days=7)
    pre_start = fire_date - timedelta(days=75)
    post_start = fire_date + timedelta(days=14)
    post_end = fire_date + timedelta(days=60)
    # Ensure chronological and year-bounded soft clamps
    if pre_start > pre_end:
        pre_start = pre_end - timedelta(days=45)
    if post_start > post_end:
        post_end = post_start + timedelta(days=30)
    return (
        (pre_start.isoformat(), pre_end.isoformat()),
        (post_start.isoformat(), post_end.isoformat()),
    )


def _feature_id(props: dict, path: Path, idx: int) -> str:
    for k in ("CODI_FINAL", "id", "fire_id", "OBJECTID", "fid", "COD", "GRID_CODE"):
        if k not in props:
            continue
        v = props[k]
        if v is None or v == "":
            continue
        # pandas NaN / float nan
        try:
            import math
            if isinstance(v, float) and math.isnan(v):
                continue
        except Exception:
            pass
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none", "null"):
            continue
        # EFFIS ids sometimes arrive as floats (224814.0)
        if isinstance(v, float) and v == int(v):
            s = str(int(v))
        return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:40]
    return f"{path.stem}_{idx}"


def load_seed_geodataframes(year: int, aoi_path: Path | None):
    import geopandas as gpd

    frames = []
    paths: list[Path] = []
    if aoi_path is not None:
        paths = [aoi_path]
    else:
        for y in (year, year - 1):
            for prefix in ("official", "effis"):
                p = SCARS_DIR / f"{prefix}_{y}.geojson"
                if p.exists() and p.stat().st_size > 20:
                    # Prefer same-year; still collect year-1 if same-year empty later
                    paths.append(p)
        # Deduplicate while preserving order
        seen = set()
        uniq = []
        for p in paths:
            if p.resolve() not in seen:
                seen.add(p.resolve())
                uniq.append(p)
        paths = uniq

    same_year = [p for p in paths if f"_{year}." in p.name or f"_{year}_" in p.name]
    if same_year:
        # Prefer same-year seeds when available
        paths = same_year
    elif year >= 2026:
        # Seed from year-1 (and any explicit --aoi already in paths)
        print(
            f"[map_scars_openeo] No same-year seed for {year}; "
            f"using year-1 / available layers as geographic seeds "
            f"with {year} seasonal windows.",
            flush=True,
        )

    for p in paths:
        if not p.exists():
            print(f"  missing AOI file: {p}", flush=True)
            continue
        print(f"Reading seed AOIs: {p}", flush=True)
        gdf = gpd.read_file(p)
        if gdf.empty:
            continue
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        else:
            gdf = gdf.to_crs("EPSG:4326")
        gdf = gdf[~gdf.geometry.isna() & ~gdf.geometry.is_empty].copy()
        seed_year = year
        m = re.search(r"(official|effis)_(\d{4})", p.name)
        if m:
            seed_year = int(m.group(2))
        gdf["_seed_path"] = p.name
        gdf["_seed_year"] = seed_year
        gdf["_source"] = "official" if "official" in p.name.lower() else (
            "effis" if "effis" in p.name.lower() else "aoi"
        )
        frames.append(gdf)
    return frames


def build_aois(year: int, max_aois: int, aoi_path: Path | None) -> list[Aoi]:
    import geopandas as gpd
    from shapely.ops import unary_union

    frames = load_seed_geodataframes(year, aoi_path)
    if not frames:
        return []

    import pandas as pd

    gdf = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        crs="EPSG:4326",
    )
    # Metric CRS for area / buffer
    metric = gdf.to_crs(AREA_CRS)
    metric["area_ha"] = metric.geometry.area / 10_000.0

    aois_raw: list[dict] = []
    for idx, row in metric.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        props = {
            k: row[k]
            for k in gdf.columns
            if k not in ("geometry", "_seed_path", "_seed_year", "_source")
        }
        # Also pull from original index-aligned frame
        for k in ("DATA_INCEN", "FIREDATE", "FINALDATE", "CODI_FINAL", "id", "AREA_HA"):
            if k in row.index:
                props[k] = row[k]

        is_point = geom.geom_type in ("Point", "MultiPoint")
        area_ha = float(row["area_ha"])
        if is_point and area_ha < MIN_POINT_HA:
            # points have ~0 area — skip unless we treat them specially
            print(f"  skip tiny point seed idx={idx}", flush=True)
            continue
        if (not is_point) and area_ha < 0.01:
            continue

        buf = BUFFER_M_HUGE if area_ha >= HUGE_HA else BUFFER_M
        # Points: give a minimum footprint before buffer (~radius for ~5 ha disk ≈ 126 m)
        if is_point:
            geom = geom.buffer(126.0)
            area_ha = float(geom.area / 10_000.0)
            if area_ha < MIN_POINT_HA:
                continue
        buffered = geom.buffer(buf)
        fire_d = parse_fire_date(props)
        # If seed is from prior year (2026 case), ignore old fire dates → seasonal defaults
        seed_year = int(row.get("_seed_year", year))
        if seed_year != year:
            fire_d = None
        src = str(row.get("_source", "aoi"))
        fid = _feature_id(props, Path(str(row.get("_seed_path", "aoi"))), int(idx))
        aois_raw.append(
            {
                "id": fid,
                "geom_m": buffered,
                "area_ha_seed": area_ha,
                "fire_date": fire_d,
                "source": src,
                "seed_year": seed_year,
            }
        )

    if not aois_raw:
        return []

    # Merge overlapping buffers
    geoms = [a["geom_m"] for a in aois_raw]
    merged = unary_union(geoms)
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]

    # Assign metadata from largest overlapping seed
    import geopandas as gpd2

    seed_gdf = gpd2.GeoDataFrame(
        [
            {
                "id": a["id"],
                "area_ha_seed": a["area_ha_seed"],
                "fire_date": a["fire_date"],
                "source": a["source"],
                "seed_year": a["seed_year"],
                "geometry": a["geom_m"],
            }
            for a in aois_raw
        ],
        crs=AREA_CRS,
    )

    result: list[Aoi] = []
    for i, part in enumerate(parts):
        if part is None or part.is_empty:
            continue
        area_ha = float(part.area / 10_000.0)
        # Find best overlapping seed for metadata
        overlaps = seed_gdf[seed_gdf.intersects(part)].copy()
        if overlaps.empty:
            fire_d = None
            src = "merged"
            fid = f"merged_{i}"
            seed_year = year
        else:
            overlaps = overlaps.sort_values("area_ha_seed", ascending=False)
            top = overlaps.iloc[0]
            fire_d = top["fire_date"]
            src = str(top["source"])
            fid = str(top["id"])
            seed_year = int(top["seed_year"])
        # Back to WGS84
        g_wgs = gpd2.GeoSeries([part], crs=AREA_CRS).to_crs("EPSG:4326").iloc[0]
        result.append(
            Aoi(
                aoi_id=fid,
                geometry=g_wgs,
                area_ha=area_ha,
                fire_date=fire_d,
                source=src,
                seed_year=seed_year,
            )
        )

    result.sort(key=lambda a: a.area_ha, reverse=True)
    if max_aois > 0 and len(result) > max_aois:
        print(
            f"[map_scars_openeo] Capping AOIs {len(result)} → {max_aois} (largest first)",
            flush=True,
        )
        result = result[:max_aois]
    return result


def spatial_extent(geom) -> dict:
    minx, miny, maxx, maxy = geom.bounds
    # Small pad in degrees (~200 m)
    pad = 0.002
    return {
        "west": float(minx - pad),
        "south": float(miny - pad),
        "east": float(maxx + pad),
        "north": float(maxy + pad),
    }


def connect_cdse():
    """Authenticate to CDSE with OAuth client credentials."""
    import openeo

    credentials = _client_credentials()
    if credentials is None:
        raise RuntimeError(
            "CDSE_CLIENT_ID and CDSE_CLIENT_SECRET must both be set for auth"
        )
    client_id, client_secret = credentials
    print(f"Connecting to {CDSE_URL} with OAuth client credentials …", flush=True)

    connection = openeo.connect(CDSE_URL)
    try:
        connection.authenticate_oidc_client_credentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        print("Authenticated via authenticate_oidc_client_credentials", flush=True)
        return connection
    except Exception as e:
        raise RuntimeError(
            "CDSE authentication failed (non-interactive): "
            f"{type(e).__name__}: {e} — check OAuth client status and "
            "CDSE_CLIENT_ID/CDSE_CLIENT_SECRET secrets."
        ) from e


def build_dnbr_cube(connection, extent: dict, pre: tuple[str, str], post: tuple[str, str]):
    """Build dNBR DataCube: median pre/post NBR with SCL cloud mask."""

    def nbr_composite(temporal_extent: tuple[str, str]):
        cube = connection.load_collection(
            "SENTINEL2_L2A",
            spatial_extent=extent,
            temporal_extent=list(temporal_extent),
            bands=["B08", "B12", "SCL"],
            max_cloud_cover=90,
        )
        scl = cube.band("SCL")
        # Mask cloudy / shadow / cirrus
        mask = (scl == 3) | (scl == 8) | (scl == 9) | (scl == 10)
        b08 = cube.band("B08")
        b12 = cube.band("B12")
        nbr = (b08 - b12) / (b08 + b12)
        nbr_masked = nbr.mask(mask)
        return nbr_masked.reduce_dimension(reducer="median", dimension="t")

    nbr_pre = nbr_composite(pre)
    nbr_post = nbr_composite(post)
    # Float dNBR; threshold applied locally when polygonizing
    return nbr_pre - nbr_post


def polygonize_tiff(tiff_path: Path, threshold: float = DNBR_THRESHOLD) -> list[dict]:
    """Polygonize burned pixels (dNBR >= threshold) → GeoJSON-like features."""
    import numpy as np
    import rasterio
    from rasterio import features as rio_features
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    feats: list[dict] = []
    with rasterio.open(tiff_path) as src:
        data = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata
        mask = np.isfinite(data) & (data >= threshold)
        if nodata is not None:
            mask &= data != nodata
        if not mask.any():
            return []
        shapes_gen = rio_features.shapes(
            data.astype("float32"),
            mask=mask.astype("uint8"),
            transform=transform,
        )
        geoms = []
        for geom, val in shapes_gen:
            if val is None:
                continue
            g = shape(geom)
            if g.is_empty or g.area <= 0:
                continue
            geoms.append(g)
        if not geoms:
            return []
        merged = unary_union(geoms)
        parts = list(merged.geoms) if merged.geom_type.startswith("Multi") else [merged]
        # Reproject to WGS84 if needed
        import geopandas as gpd

        gs = gpd.GeoSeries(parts, crs=crs)
        if crs is None:
            gs = gs.set_crs("EPSG:4326")
        gs = gs.to_crs("EPSG:4326")
        metric = gs.to_crs(AREA_CRS)
        for i, (g_wgs, g_m) in enumerate(zip(gs, metric)):
            area_ha = float(g_m.area / 10_000.0)
            if area_ha < 0.1:
                continue
            feats.append(
                {
                    "type": "Feature",
                    "geometry": mapping(g_wgs),
                    "properties": {
                        "area_ha": round(area_ha, 3),
                        "dnbr_threshold": threshold,
                        "part": i,
                    },
                }
            )
    return feats


def run_aoi_job(connection, aoi: Aoi, year: int, out_dir: Path, dry_run: bool) -> Path | None:
    pre, post = windows_for_fire(aoi.fire_date, year)
    extent = spatial_extent(aoi.geometry)
    print(
        f"  AOI {aoi.aoi_id}: area_buf≈{aoi.area_ha:.1f} ha source={aoi.source} "
        f"fire_date={aoi.fire_date} pre={pre} post={post} "
        f"extent={extent}",
        flush=True,
    )
    if dry_run:
        return None

    cube = build_dnbr_cube(connection, extent, pre, post)
    title = f"cat-cicatrius dNBR {year} {aoi.aoi_id}"
    job = None
    job_id = None
    try:
        work = out_dir / f"job_{aoi.aoi_id}"
        work.mkdir(parents=True, exist_ok=True)
        out_tif = work / "dnbr.tif"
        print(f"  Starting batch job for {aoi.aoi_id} …", flush=True)
        try:
            job = cube.execute_batch(
                outputfile=str(out_tif),
                out_format="GTiff",
                title=title,
                description=f"Sentinel-2 dNBR {year} AOI {aoi.aoi_id}",
            )
            job_id = getattr(job, "job_id", None)
        except TypeError:
            # Older client signature fallback
            job = cube.create_job(out_format="GTiff", title=title)
            job_id = getattr(job, "job_id", None) or str(job)
            print(f"  Job id={job_id} start_and_wait …", flush=True)
            job.start_and_wait()
            results = job.get_results()
            results.download_files(str(work))
        if job_id:
            print(f"  Job finished id={job_id}", flush=True)
        tiffs = list(work.glob("*.tif")) + list(work.glob("*.tiff"))
        if not tiffs:
            # sometimes nested
            tiffs = list(work.rglob("*.tif")) + list(work.rglob("*.tiff"))
        if not tiffs:
            print(f"  WARNING: no GeoTIFF for AOI {aoi.aoi_id}", flush=True)
            return None
        tiff = max(tiffs, key=lambda p: p.stat().st_size)
        print(f"  Polygonizing {tiff.name} ({tiff.stat().st_size} bytes) …", flush=True)
        feats = polygonize_tiff(tiff, DNBR_THRESHOLD)
        for f in feats:
            f["properties"]["fire_id"] = aoi.aoi_id
            f["properties"]["burn_year"] = year
            f["properties"]["source"] = "sentinel"
            f["properties"]["aoi_source"] = aoi.source
            f["properties"]["dnbr_threshold"] = DNBR_THRESHOLD
            if aoi.fire_date:
                f["properties"]["fire_date"] = aoi.fire_date.isoformat()
        out = SCARS_DIR / f"sentinel_{year}_{aoi.aoi_id}.geojson"
        SCARS_DIR.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": feats}, fh)
            fh.write("\n")
        print(f"  Wrote {len(feats)} features → {out}", flush=True)
        return out
    except Exception as e:
        print(
            f"  AOI {aoi.aoi_id} failed: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        if job_id:
            print(f"  (job id was {job_id})", flush=True)
        return None


def write_combined(year: int, paths: list[Path]) -> Path:
    features = []
    for p in paths:
        if p is None or not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        features.extend(obj.get("features") or [])
    out = SCARS_DIR / f"sentinel_{year}.geojson"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
        f.write("\n")
    print(f"Combined {len(features)} features → {out}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=date.today().year, help="Burn year (e.g. 2024|2026)")
    ap.add_argument("--max-aois", type=int, default=15, help="Cap AOIs (largest first); CI default 15")
    ap.add_argument("--aoi", type=Path, default=None, help="Optional GeoJSON AOI override")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-skip", action="store_true", help="Always skip (CI smoke)")
    args = ap.parse_args()

    if args.force_skip:
        return skip("--force-skip")

    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError as e:
        return skip(f"geometry deps missing: {e}")

    print(
        f"[map_scars_openeo] year={args.year} max_aois={args.max_aois} "
        f"threshold={DNBR_THRESHOLD} dry_run={args.dry_run}",
        flush=True,
    )

    aois = build_aois(args.year, args.max_aois, args.aoi)
    if not aois:
        # Last resort: do NOT process all of Catalonia as one cube
        return skip(
            "no usable AOIs from official/effis (or --aoi). "
            "Refusing full-Catalonia cube to save credits."
        )

    print(f"[map_scars_openeo] {len(aois)} AOIs queued:", flush=True)
    for a in aois:
        print(
            f"  - {a.aoi_id}: ~{a.area_ha:.0f} ha buf, fire={a.fire_date}, src={a.source}",
            flush=True,
        )

    if args.dry_run:
        print("dry-run: would submit batch dNBR jobs for the AOIs above", flush=True)
        return 0

    if not credentials_present():
        return skip(
            "CDSE_CLIENT_ID / CDSE_CLIENT_SECRET not set. "
            "Add both as GitHub Actions secrets (OAuth client) to enable "
            "Sentinel dNBR. Pipeline continues with official/EFFIS geometry only."
        )

    try:
        import openeo  # noqa: F401
    except ImportError:
        return skip("package 'openeo' not installed (see requirements.txt)")

    try:
        connection = connect_cdse()
    except Exception as e:
        return fail_auth(str(e))

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, aoi in enumerate(aois, 1):
        print(f"[{i}/{len(aois)}] Processing AOI {aoi.aoi_id} …", flush=True)
        out = run_aoi_job(connection, aoi, args.year, TMP_DIR, dry_run=False)
        if out is not None:
            written.append(out)

    write_combined(args.year, written)
    print(
        f"[map_scars_openeo] Done: {len(written)}/{len(aois)} AOIs produced scars.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
