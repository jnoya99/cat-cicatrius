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
  - Prefer scars/official_{year}.geojson seeds over EFFIS when present
    (--prefer-official, default on); year-1 seeds when needed (e.g. 2026).
  - Buffer ~500 m (official) / ~800 m (EFFIS); +50% if seed >500 ha; merge overlaps;
    skip tiny <~5 ha points; cap --max-aois.
  - Per AOI: pre/post median S2 L2A (B08,B12,SCL), cloud-mask SCL, NBR, dNBR≥threshold
    (default 0.35). Clip scars to seed buffered 150 m; drop parts < min-ha (default 2.0).
    Morphological opening (1px erode+dilate) before polygonize to kill FP speckles.
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

DNBR_THRESHOLD = 0.35
CDSE_URL = "https://openeo.dataspace.copernicus.eu"
BUFFER_OFFICIAL_M = 500.0
BUFFER_EFFIS_M = 800.0
HUGE_HA = 500.0  # seed area: buffer +50%
MIN_POINT_HA = 5.0  # skip tiny point-only seeds
MIN_PART_HA = 2.0  # drop polygonized parts smaller than this
CLIP_SEED_BUFFER_M = 150.0  # clip scars to seed ⊕ this buffer
EFFIS_OVERLAP_FRAC = 0.3  # skip EFFIS if ≥ this fraction overlaps official
AREA_CRS = "EPSG:25831"  # Catalonia UTM 31N
SCL_CLOUD = {3, 8, 9, 10}  # cloud shadow, cloud med/high, cirrus


@dataclass
class Aoi:
    aoi_id: str
    geometry: Any  # buffered search window, EPSG:4326
    seed_geometry: Any  # unbuffered seed (union), EPSG:4326 — for clip
    area_ha: float
    fire_date: date | None
    source: str
    seed_year: int
    buffer_m: float


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


def _seed_path_ok(p: Path) -> bool:
    return p.exists() and p.stat().st_size > 20


def _buffer_m_for(source: str, area_ha: float) -> float:
    base = BUFFER_OFFICIAL_M if source == "official" else BUFFER_EFFIS_M
    if source == "aoi":
        base = BUFFER_OFFICIAL_M
    if area_ha >= HUGE_HA:
        base = base * 1.5
    return float(base)


def _filter_effis_nonoverlapping(official_gdf, effis_gdf):
    """Keep EFFIS features whose overlap with official is below EFFIS_OVERLAP_FRAC."""
    if official_gdf is None or official_gdf.empty or effis_gdf.empty:
        return effis_gdf
    off_m = official_gdf.to_crs(AREA_CRS)
    ef_m = effis_gdf.to_crs(AREA_CRS)
    off_union = off_m.unary_union
    keep_idx = []
    for idx, geom in ef_m.geometry.items():
        if geom is None or geom.is_empty:
            continue
        area = float(geom.area)
        if area <= 0:
            continue
        inter = geom.intersection(off_union)
        frac = float(inter.area) / area if inter and not inter.is_empty else 0.0
        if frac < EFFIS_OVERLAP_FRAC:
            keep_idx.append(idx)
    if not keep_idx:
        return effis_gdf.iloc[0:0].copy()
    return effis_gdf.loc[keep_idx].copy()


def load_seed_geodataframes(
    year: int, aoi_path: Path | None, prefer_official: bool = True
):
    import geopandas as gpd

    frames = []
    paths: list[Path] = []
    n_official = 0
    n_effis = 0
    n_effis_skipped = 0

    if aoi_path is not None:
        paths = [aoi_path]
    else:
        for y in (year, year - 1):
            official_p = SCARS_DIR / f"official_{y}.geojson"
            effis_p = SCARS_DIR / f"effis_{y}.geojson"
            has_official = _seed_path_ok(official_p)
            has_effis = _seed_path_ok(effis_p)
            if has_official:
                paths.append(official_p)
            if has_effis:
                # Same-year: skip EFFIS entirely when official exists and prefer_official
                if prefer_official and has_official and y == year:
                    print(
                        f"[map_scars_openeo] prefer-official: skipping {effis_p.name} "
                        f"(official_{y} present)",
                        flush=True,
                    )
                    n_effis_skipped += 1
                else:
                    paths.append(effis_p)
        seen = set()
        uniq = []
        for p in paths:
            if p.resolve() not in seen:
                seen.add(p.resolve())
                uniq.append(p)
        paths = uniq

    same_year = [p for p in paths if f"_{year}." in p.name or f"_{year}_" in p.name]
    if same_year:
        paths = same_year
    elif year >= 2026:
        print(
            f"[map_scars_openeo] No same-year seed for {year}; "
            f"using year-1 / available layers as geographic seeds "
            f"with {year} seasonal windows.",
            flush=True,
        )

    loaded_by_key: dict[tuple[str, int], Any] = {}
    pending: list[tuple[Path, Any]] = []

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
        source = (
            "official"
            if "official" in p.name.lower()
            else ("effis" if "effis" in p.name.lower() else "aoi")
        )
        gdf["_seed_path"] = p.name
        gdf["_seed_year"] = seed_year
        gdf["_source"] = source
        pending.append((p, gdf))
        loaded_by_key[(source, seed_year)] = gdf

    for p, gdf in pending:
        source = str(gdf["_source"].iloc[0])
        seed_year = int(gdf["_seed_year"].iloc[0])
        if source == "effis" and not prefer_official:
            off = loaded_by_key.get(("official", seed_year))
            before = len(gdf)
            gdf = _filter_effis_nonoverlapping(off, gdf)
            dropped = before - len(gdf)
            if dropped:
                print(
                    f"[map_scars_openeo] dropped {dropped}/{before} EFFIS features "
                    f"overlapping official_{seed_year}",
                    flush=True,
                )
                n_effis_skipped += dropped
        if gdf.empty:
            continue
        if source == "official":
            n_official += len(gdf)
        elif source == "effis":
            n_effis += len(gdf)
        frames.append(gdf)

    print(
        f"[map_scars_openeo] seed sources: official={n_official} "
        f"effis={n_effis} effis_skipped_overlap_or_prefer={n_effis_skipped} "
        f"prefer_official={prefer_official}",
        flush=True,
    )
    return frames


def build_aois(
    year: int,
    max_aois: int,
    aoi_path: Path | None,
    prefer_official: bool = True,
) -> list[Aoi]:
    import geopandas as gpd
    from shapely.ops import unary_union

    frames = load_seed_geodataframes(year, aoi_path, prefer_official=prefer_official)
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
        for k in ("DATA_INCEN", "FIREDATE", "FINALDATE", "CODI_FINAL", "id", "AREA_HA"):
            if k in row.index:
                props[k] = row[k]

        is_point = geom.geom_type in ("Point", "MultiPoint")
        area_ha = float(row["area_ha"])
        if is_point and area_ha < MIN_POINT_HA:
            print(f"  skip tiny point seed idx={idx}", flush=True)
            continue
        if (not is_point) and area_ha < 0.01:
            continue

        src = str(row.get("_source", "aoi"))
        buf = _buffer_m_for(src, area_ha)
        # Points: give a minimum footprint before buffer (~radius for ~5 ha disk ≈ 126 m)
        if is_point:
            geom = geom.buffer(126.0)
            area_ha = float(geom.area / 10_000.0)
            if area_ha < MIN_POINT_HA:
                continue
        seed_m = geom
        buffered = geom.buffer(buf)
        fire_d = parse_fire_date(props)
        # If seed is from prior year (2026 case), ignore old fire dates → seasonal defaults
        seed_year = int(row.get("_seed_year", year))
        if seed_year != year:
            fire_d = None
        fid = _feature_id(props, Path(str(row.get("_seed_path", "aoi"))), int(idx))
        aois_raw.append(
            {
                "id": fid,
                "geom_m": buffered,
                "seed_m": seed_m,
                "area_ha_seed": area_ha,
                "fire_date": fire_d,
                "source": src,
                "seed_year": seed_year,
                "buffer_m": buf,
            }
        )

    if not aois_raw:
        return []

    # Merge overlapping buffers
    geoms = [a["geom_m"] for a in aois_raw]
    merged = unary_union(geoms)
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]

    seed_gdf = gpd.GeoDataFrame(
        [
            {
                "id": a["id"],
                "area_ha_seed": a["area_ha_seed"],
                "fire_date": a["fire_date"],
                "source": a["source"],
                "seed_year": a["seed_year"],
                "buffer_m": a["buffer_m"],
                "seed_m": a["seed_m"],
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
        overlaps = seed_gdf[seed_gdf.intersects(part)].copy()
        if overlaps.empty:
            fire_d = None
            src = "merged"
            fid = f"merged_{i}"
            seed_year = year
            buf_m = BUFFER_OFFICIAL_M
            seed_union_m = part  # fallback
        else:
            overlaps = overlaps.sort_values("area_ha_seed", ascending=False)
            top = overlaps.iloc[0]
            fire_d = top["fire_date"]
            src = str(top["source"])
            fid = str(top["id"])
            seed_year = int(top["seed_year"])
            buf_m = float(top["buffer_m"])
            seed_union_m = unary_union(list(overlaps["seed_m"]))
        g_wgs = gpd.GeoSeries([part], crs=AREA_CRS).to_crs("EPSG:4326").iloc[0]
        seed_wgs = (
            gpd.GeoSeries([seed_union_m], crs=AREA_CRS).to_crs("EPSG:4326").iloc[0]
        )
        result.append(
            Aoi(
                aoi_id=fid,
                geometry=g_wgs,
                seed_geometry=seed_wgs,
                area_ha=area_ha,
                fire_date=fire_d,
                source=src,
                seed_year=seed_year,
                buffer_m=buf_m,
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



def _binary_erode(mask, iterations: int = 1):
    """3x3 binary erosion (all-neighbors). Prefer scipy; else numpy."""
    import numpy as np

    try:
        from scipy import ndimage

        return ndimage.binary_erosion(mask, iterations=iterations)
    except ImportError:
        out = mask.astype(bool)
        for _ in range(iterations):
            padded = np.pad(out, 1, mode="constant", constant_values=False)
            neigh = True
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    neigh = neigh & padded[1 + di : 1 + di + out.shape[0], 1 + dj : 1 + dj + out.shape[1]]
            out = neigh
        return out


def _binary_dilate(mask, iterations: int = 1):
    """3x3 binary dilation (any-neighbor). Prefer scipy; else numpy."""
    import numpy as np

    try:
        from scipy import ndimage

        return ndimage.binary_dilation(mask, iterations=iterations)
    except ImportError:
        out = mask.astype(bool)
        for _ in range(iterations):
            padded = np.pad(out, 1, mode="constant", constant_values=False)
            neigh = False
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    neigh = neigh | padded[1 + di : 1 + di + out.shape[0], 1 + dj : 1 + dj + out.shape[1]]
            out = neigh
        return out


def morphological_opening(mask, iterations: int = 1):
    """Erode then dilate to remove isolated FP speckles."""
    return _binary_dilate(_binary_erode(mask, iterations), iterations)


def polygonize_tiff(
    tiff_path: Path,
    threshold: float = DNBR_THRESHOLD,
    min_ha: float = MIN_PART_HA,
    clip_geom_wgs84=None,
) -> list[dict]:
    """Polygonize burned pixels (dNBR >= threshold) → GeoJSON-like features.

    Applies 1px morphological opening before polygonize (anti-FP speckles).
    Drops parts < min_ha. Optionally clips to seed geometry buffered CLIP_SEED_BUFFER_M.
    """
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
        n_before = int(mask.sum())
        mask = morphological_opening(mask, iterations=1)
        n_after = int(mask.sum())
        print(
            f"    morph opening 1px: burned pixels {n_before} → {n_after} "
            f"(removed {n_before - n_after})",
            flush=True,
        )
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
        import geopandas as gpd

        gs = gpd.GeoSeries(parts, crs=crs)
        if crs is None:
            gs = gs.set_crs("EPSG:4326")
        gs = gs.to_crs("EPSG:4326")

        if clip_geom_wgs84 is not None and not getattr(clip_geom_wgs84, "is_empty", True):
            seed_m = (
                gpd.GeoSeries([clip_geom_wgs84], crs="EPSG:4326")
                .to_crs(AREA_CRS)
                .iloc[0]
            )
            clip_m = seed_m.buffer(CLIP_SEED_BUFFER_M)
            clip_wgs = (
                gpd.GeoSeries([clip_m], crs=AREA_CRS).to_crs("EPSG:4326").iloc[0]
            )
            gs = gs.intersection(clip_wgs)
            gs = gs[~gs.is_empty & gs.is_valid]

        if gs.empty:
            return []

        metric = gs.to_crs(AREA_CRS)
        for i, (g_wgs, g_m) in enumerate(zip(gs, metric)):
            if g_wgs is None or g_wgs.is_empty:
                continue
            area_ha = float(g_m.area / 10_000.0)
            if area_ha < min_ha:
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


def run_aoi_job(
    connection,
    aoi: Aoi,
    year: int,
    out_dir: Path,
    dry_run: bool,
    threshold: float = DNBR_THRESHOLD,
    min_ha: float = MIN_PART_HA,
) -> Path | None:
    pre, post = windows_for_fire(aoi.fire_date, year)
    extent = spatial_extent(aoi.geometry)
    print(
        f"  AOI {aoi.aoi_id}: area_buf≈{aoi.area_ha:.1f} ha source={aoi.source} "
        f"buffer_m={aoi.buffer_m:.0f} fire_date={aoi.fire_date} pre={pre} post={post} "
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
        print(
            f"  Polygonizing {tiff.name} ({tiff.stat().st_size} bytes) "
            f"threshold={threshold} min_ha={min_ha} clip_seed+{CLIP_SEED_BUFFER_M:.0f}m …",
            flush=True,
        )
        feats = polygonize_tiff(
            tiff,
            threshold=threshold,
            min_ha=min_ha,
            clip_geom_wgs84=aoi.seed_geometry,
        )
        for f in feats:
            f["properties"]["fire_id"] = aoi.aoi_id
            f["properties"]["burn_year"] = year
            f["properties"]["source"] = "sentinel"
            f["properties"]["aoi_source"] = aoi.source
            f["properties"]["dnbr_threshold"] = threshold
            f["properties"]["buffer_m"] = aoi.buffer_m
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
    ap.add_argument(
        "--threshold",
        type=float,
        default=DNBR_THRESHOLD,
        help=f"dNBR threshold (default {DNBR_THRESHOLD})",
    )
    ap.add_argument(
        "--min-ha",
        type=float,
        default=MIN_PART_HA,
        help=f"Drop polygonized parts smaller than this (default {MIN_PART_HA})",
    )
    ap.add_argument(
        "--prefer-official",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer official seeds; skip same-year EFFIS when official exists (default: true)",
    )
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
        f"threshold={args.threshold} min_ha={args.min_ha} "
        f"prefer_official={args.prefer_official} "
        f"buffer_official_m={BUFFER_OFFICIAL_M} buffer_effis_m={BUFFER_EFFIS_M} "
        f"clip_seed_buffer_m={CLIP_SEED_BUFFER_M} dry_run={args.dry_run}",
        flush=True,
    )
    print(
        "[map_scars_openeo] anti-FP settings: "
        f"dnbr_threshold={args.threshold} (default {DNBR_THRESHOLD}), "
        f"clip_seed_buffer_m={CLIP_SEED_BUFFER_M}, "
        f"search_buffer_official_m={BUFFER_OFFICIAL_M}, "
        f"search_buffer_effis_m={BUFFER_EFFIS_M}, "
        f"min_ha={args.min_ha} (default {MIN_PART_HA}), "
        "morph_opening=1px erode+dilate",
        flush=True,
    )

    aois = build_aois(
        args.year, args.max_aois, args.aoi, prefer_official=args.prefer_official
    )
    if not aois:
        # Last resort: do NOT process all of Catalonia as one cube
        return skip(
            "no usable AOIs from official/effis (or --aoi). "
            "Refusing full-Catalonia cube to save credits."
        )

    print(f"[map_scars_openeo] {len(aois)} AOIs queued:", flush=True)
    for a in aois:
        print(
            f"  - {a.aoi_id}: ~{a.area_ha:.0f} ha buf, buffer_m={a.buffer_m:.0f}, "
            f"fire={a.fire_date}, src={a.source}",
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
        out = run_aoi_job(
            connection,
            aoi,
            args.year,
            TMP_DIR,
            dry_run=False,
            threshold=args.threshold,
            min_ha=args.min_ha,
        )
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
