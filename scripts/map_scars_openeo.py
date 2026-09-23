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
  - Same-year seeds only (never year-1 locations for year Y burns):
    official_{Y} → effis_{Y} → Gencat event points (municipality centroid).
  - Buffer ~800 m (official/gencat) / ~1200 m (EFFIS); +50% if seed >500 ha;
    merge overlaps; skip seeds with seed-geometry area < --min-seed-ha
    (default 1.0 ha); skip tiny <~5 ha Gencat points; --max-aois 0 = no cap.
  - Temporal extents are always clipped to min(end, UTC today). Empty post
    windows after clipping are dropped; AOIs with no remaining post window
    are skipped. Undated current-year posts: ~Jun15→today (≤n slices); undated
    past years: Aug15–Nov15 seasonal slices (clipped). Dated default 2 windows:
    +7–30/+30–60d (3rd +60–90d when --post-windows 3).
  - Per AOI: pre median S2 L2A (B08,B12,SCL); multi-post max dNBR; region-grow
    (core≥0.35, grow≥0.22); optional morph on core; WorldCover forest/scrub
    mask; clip to seed⊕300 m; drop parts < min-ha (default 1.0).
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

DNBR_THRESHOLD_CORE = 0.35
DNBR_THRESHOLD_GROW = 0.22
DNBR_THRESHOLD = DNBR_THRESHOLD_CORE  # backwards-compat alias


# USGS/MTBS-style approximate classes on raw dNBR (Key & Benson /1000):
#   baixa ≈ low (0.10–0.27); moderada ≈ moderate-low (0.27–0.44);
#   alta ≈ moderate-high + high (≥0.44). Catalan labels for the app.
SEVERITY_BAIXA = "baixa"
SEVERITY_MODERADA = "moderada"
SEVERITY_ALTA = "alta"
# Proxy when polygons exist but the dNBR raster was not kept (backfill):
# region-grow requires a core ≥ DNBR_THRESHOLD_CORE (0.35) → solidly "moderada".
SEVERITY_PROXY_NO_RASTER = SEVERITY_MODERADA


def severity_from_dnbr(dnbr: float | None) -> str | None:
    """Map mean/max dNBR to Catalan severity class; None if unburned/unknown."""
    if dnbr is None:
        return None
    try:
        x = float(dnbr)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    if x < 0.10:
        return None
    if x < 0.27:
        return SEVERITY_BAIXA
    if x < 0.44:
        return SEVERITY_MODERADA
    return SEVERITY_ALTA

CDSE_URL = "https://openeo.dataspace.copernicus.eu"
BUFFER_OFFICIAL_M = 800.0
BUFFER_EFFIS_M = 1200.0
HUGE_HA = 500.0  # seed area: buffer +50%
MIN_POINT_HA = 5.0  # skip tiny point-only seeds (Gencat reported ha)
MIN_SEED_HA = 1.0  # skip polygon seeds with seed-geom area below this (ha)
MIN_PART_HA = 1.0  # drop polygonized parts smaller than this
CLIP_SEED_BUFFER_M = 300.0  # clip scars to seed ⊕ this buffer
EFFIS_OVERLAP_FRAC = 0.3  # skip EFFIS if ≥ this fraction overlaps official
CAT_PROVINCES_ES = {"Barcelona", "Girona", "Lleida", "Tarragona"}
AREA_CRS = "EPSG:25831"  # Catalonia UTM 31N
SCL_CLOUD = {3, 8, 9, 10}  # cloud shadow, cloud med/high, cirrus
WORLDCOVER_COLLECTION = "ESA_WORLDCOVER_10M_2021_V2"
WORLDCOVER_FOREST_SCRUB = {10, 20}  # tree cover, shrubland


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


def utc_today() -> date:
    """UTC calendar date (clip target for all temporal extents)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date()


def clip_date_range(
    start: date, end: date, today: date | None = None
) -> tuple[date, date] | None:
    """Clip [start, end] to UTC today. Return None if empty/invalid after clip."""
    today = today or utc_today()
    end_c = min(end, today)
    if start > today:
        return None
    if end_c <= start:
        return None
    return start, end_c


def clip_iso_window(
    window: tuple[str, str], today: date | None = None
) -> tuple[str, str] | None:
    a = date.fromisoformat(str(window[0])[:10])
    b = date.fromisoformat(str(window[1])[:10])
    clipped = clip_date_range(a, b, today=today)
    if clipped is None:
        return None
    return clipped[0].isoformat(), clipped[1].isoformat()


def clip_iso_windows(
    windows: list[tuple[str, str]], today: date | None = None
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for w in windows:
        c = clip_iso_window(w, today=today)
        if c is not None:
            out.append(c)
    return out


def pre_window_for_fire(
    fire_date: date | None, year: int, today: date | None = None
) -> tuple[str, str] | None:
    """Clear-sky pre-fire median window, clipped to UTC today."""
    today = today or utc_today()
    if fire_date is None:
        raw = (date(year, 4, 1), date(year, 6, 15))
    else:
        pre_end = fire_date - timedelta(days=7)
        pre_start = fire_date - timedelta(days=75)
        if pre_start > pre_end:
            pre_start = pre_end - timedelta(days=45)
        raw = (pre_start, pre_end)
    clipped = clip_date_range(raw[0], raw[1], today=today)
    if clipped is None:
        return None
    return clipped[0].isoformat(), clipped[1].isoformat()


def _slice_range(start: date, end: date, n_windows: int) -> list[tuple[str, str]]:
    """Split [start, end] into n contiguous date windows (ISO pairs, end>start)."""
    n_windows = max(1, int(n_windows))
    if end <= start:
        return []
    total_days = (end - start).days
    if total_days < 1:
        return []
    # Cap window count when the span is short (avoid empty slices)
    n_windows = min(n_windows, max(1, total_days))
    windows: list[tuple[str, str]] = []
    for i in range(n_windows):
        a = start + timedelta(days=int(round(total_days * i / n_windows)))
        if i + 1 == n_windows:
            b = end
        else:
            b = start + timedelta(days=int(round(total_days * (i + 1) / n_windows)))
        if b <= a:
            b = a + timedelta(days=1)
        if b > end:
            b = end
        if b <= a:
            continue
        windows.append((a.isoformat(), b.isoformat()))
    return windows


def post_windows_for_fire(
    fire_date: date | None, year: int, n_windows: int = 2, today: date | None = None
) -> list[tuple[str, str]]:
    """Sequential post-fire composite windows for max-dNBR compositing.

    Always clipped to UTC today; empty/invalid windows after clipping are dropped.

    With fire_date and n_windows==2 (default): +7–30d, +30–60d.
    With fire_date and n_windows==3: +7–30d, +30–60d, +60–90d.
    Undated incomplete year (year == today.year): Jun15 → today, ≤n slices.
    Undated past years: Aug15–Nov15 seasonal slices (then clipped).
    """
    today = today or utc_today()
    n_windows = max(1, int(n_windows))
    raw: list[tuple[str, str]] = []

    if fire_date is None:
        if year > today.year:
            return []
        if year == today.year:
            season_start = date(year, 6, 15)
            if season_start >= today:
                # Fire season post window has not started / nothing past today
                return []
            raw = _slice_range(season_start, today, n_windows)
        else:
            raw = _slice_range(date(year, 8, 15), date(year, 11, 15), n_windows)
    elif n_windows == 3:
        specs = [(7, 30), (30, 60), (60, 90)]
        for a_off, b_off in specs:
            a = fire_date + timedelta(days=a_off)
            b = fire_date + timedelta(days=b_off)
            if b <= a:
                b = a + timedelta(days=14)
            raw.append((a.isoformat(), b.isoformat()))
    elif n_windows == 2:
        specs = [(7, 30), (30, 60)]
        for a_off, b_off in specs:
            a = fire_date + timedelta(days=a_off)
            b = fire_date + timedelta(days=b_off)
            if b <= a:
                b = a + timedelta(days=14)
            raw.append((a.isoformat(), b.isoformat()))
    elif n_windows == 1:
        a = fire_date + timedelta(days=7)
        b = fire_date + timedelta(days=60)
        raw = [(a.isoformat(), b.isoformat())]
    else:
        raw = _slice_range(
            fire_date + timedelta(days=7),
            fire_date + timedelta(days=90),
            n_windows,
        )

    return clip_iso_windows(raw, today=today)


def windows_for_fire(
    fire_date: date | None, year: int, n_post_windows: int = 2, today: date | None = None
) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
    """Return (pre_window|None, list_of_post_windows) all clipped to UTC today."""
    today = today or utc_today()
    return (
        pre_window_for_fire(fire_date, year, today=today),
        post_windows_for_fire(fire_date, year, n_post_windows, today=today),
    )


def seasonal_windows(year: int) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """Back-compat: single pre + single post (first seasonal post slice)."""
    pre = pre_window_for_fire(None, year)
    posts = post_windows_for_fire(None, year, n_windows=1)
    return pre, (posts[0] if posts else None)


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
    if source in ("official", "aoi", "gencat"):
        base = BUFFER_OFFICIAL_M
    else:
        base = BUFFER_EFFIS_M
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



def _filter_effis_catalonia(effis_gdf):
    """Keep EFFIS features in Catalonia (ES + BCN/GIR/LLE/TAR) when attrs exist."""
    if effis_gdf is None or effis_gdf.empty:
        return effis_gdf
    cols = {c.upper(): c for c in effis_gdf.columns}
    country_c = cols.get("COUNTRY")
    province_c = cols.get("PROVINCE")
    if country_c is None or province_c is None:
        print(
            "[map_scars_openeo] EFFIS lacks COUNTRY/PROVINCE — keeping all bbox features",
            flush=True,
        )
        return effis_gdf
    country = effis_gdf[country_c].astype(str).str.upper()
    province = effis_gdf[province_c].astype(str)
    keep = (country == "ES") & (province.isin(CAT_PROVINCES_ES))
    before = len(effis_gdf)
    out = effis_gdf.loc[keep].copy()
    print(
        f"[map_scars_openeo] EFFIS Catalonia filter: {len(out)}/{before} "
        f"(ES + {sorted(CAT_PROVINCES_ES)})",
        flush=True,
    )
    return out

def _normalize_muni_code(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return ""
    # Gencat often drops the leading province zero (80898 vs 080898)
    if s.isdigit():
        return s.zfill(6)
    return s


def _icgc_municipis_path() -> Path:
    return TMP_DIR / "icgc_municipis_250000.geojson"


def fetch_icgc_municipis(force: bool = False) -> Path | None:
    """Download ICGC municipis (1:250k) GeoJSON for centroid geocoding."""
    out = _icgc_municipis_path()
    if out.exists() and out.stat().st_size > 1000 and not force:
        return out
    url = (
        "https://geoserveis.icgc.cat/servei/catalunya/divisions-administratives/wfs"
        "?service=WFS&version=2.0.0&request=GetFeature"
        "&typeNames=divisions_administratives_municipis_250000"
        "&outputFormat=GEOJSON"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request

        print(f"[map_scars_openeo] GET ICGC municipis → {out.name}", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "cat-cicatrius/1.0"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
        if b"FeatureCollection" not in data[:200] and b"features" not in data[:400]:
            print(
                f"  ICGC municipis response not GeoJSON ({len(data)} bytes); skip",
                flush=True,
            )
            return None
        out.write_bytes(data)
        print(f"  wrote {out} ({len(data)} bytes)", flush=True)
        return out
    except Exception as e:
        print(
            f"  WARNING: ICGC municipis download failed ({type(e).__name__}: {e})",
            flush=True,
        )
        return None


def load_gencat_point_seeds(year: int, min_ha: float = MIN_POINT_HA):
    """Build point seeds from Gencat events CSV + ICGC municipality centroids.

    Uses events/gencat_current_year.csv and/or gencat_historic_2011_2024.csv /
    gencat_events_all.csv. Rows without a matchable municipality code are skipped.
    """
    import geopandas as gpd
    import pandas as pd

    events_dir = ROOT / "events"
    candidates = [
        events_dir / "gencat_events_all.csv",
        events_dir / "gencat_current_year.csv",
        events_dir / "gencat_historic_2011_2024.csv",
    ]
    rows: list[dict] = []
    seen_keys: set[tuple] = set()
    for csv_path in candidates:
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path, dtype=str).fillna("")
        except Exception as e:
            print(f"  skip gencat csv {csv_path.name}: {e}", flush=True)
            continue
        for _, r in df.iterrows():
            d = str(r.get("data_incendi") or "")
            if not d.startswith(str(year)):
                continue
            key = (
                d[:10],
                str(r.get("codi_municipi") or ""),
                str(r.get("haforestal") or ""),
                str(r.get("termemunic") or ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(dict(r))
    if not rows:
        print(
            f"[map_scars_openeo] gencat seeds: 0 events for year {year}",
            flush=True,
        )
        return None

    muni_path = fetch_icgc_municipis()
    if muni_path is None:
        print(
            "[map_scars_openeo] gencat seeds: no municipality geometries; skip points",
            flush=True,
        )
        return None

    munis = gpd.read_file(muni_path)
    if munis.crs is None:
        munis = munis.set_crs("EPSG:4326")
    else:
        munis = munis.to_crs("EPSG:4326")
    code_col = None
    for c in ("CODIMUNI", "codimuni", "CODIGOINE", "CODIGO", "code"):
        if c in munis.columns:
            code_col = c
            break
    if code_col is None:
        print(
            f"[map_scars_openeo] gencat seeds: no CODIMUNI in ICGC props "
            f"{list(munis.columns)[:12]}; skip",
            flush=True,
        )
        return None
    munis["_code"] = munis[code_col].map(_normalize_muni_code)
    # centroid in projected CRS for stability
    munis_m = munis.to_crs(AREA_CRS)
    centroids = gpd.GeoSeries(munis_m.geometry.centroid, crs=AREA_CRS).to_crs("EPSG:4326")
    code_to_pt = {
        code: pt
        for code, pt in zip(munis["_code"], centroids)
        if code and pt is not None and not pt.is_empty
    }

    feats = []
    skipped_no_loc = 0
    skipped_tiny = 0
    for r in rows:
        try:
            ha = float(r.get("haforestal") or 0)
        except (TypeError, ValueError):
            ha = 0.0
        if ha < min_ha:
            skipped_tiny += 1
            continue
        code = _normalize_muni_code(r.get("codi_municipi"))
        pt = code_to_pt.get(code)
        if pt is None:
            skipped_no_loc += 1
            continue
        fire_d = parse_fire_date(
            {
                "data_incendi": r.get("data_incendi"),
                "DATE": r.get("data_incendi"),
            }
        )
        fid = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            f"gencat_{code}_{(r.get('data_incendi') or '')[:10]}",
        )[:40]
        feats.append(
            {
                "id": fid,
                "FIREDATE": fire_d.isoformat() if fire_d else "",
                "fire_date": fire_d.isoformat() if fire_d else "",
                "data_incendi": r.get("data_incendi") or "",
                "AREA_HA": ha,
                "termemunic": r.get("termemunic") or "",
                "codi_municipi": code,
                "geometry": pt,
            }
        )

    print(
        f"[map_scars_openeo] gencat seeds year={year}: kept={len(feats)} "
        f"skipped_tiny(<{min_ha}ha)={skipped_tiny} skipped_no_location={skipped_no_loc} "
        f"from {len(rows)} event rows",
        flush=True,
    )
    if not feats:
        return None
    gdf = gpd.GeoDataFrame(feats, geometry="geometry", crs="EPSG:4326")
    gdf["_seed_path"] = "gencat_events"
    gdf["_seed_year"] = year
    gdf["_source"] = "gencat"
    return gdf


def load_seed_geodataframes(
    year: int, aoi_path: Path | None, prefer_official: bool = True
):
    import geopandas as gpd

    frames = []
    paths: list[Path] = []
    n_official = 0
    n_effis = 0
    n_gencat = 0
    n_effis_skipped = 0

    if aoi_path is not None:
        paths = [aoi_path]
    else:
        # Same-year seeds ONLY — never seed year Y from Y-1 fire places.
        official_p = SCARS_DIR / f"official_{year}.geojson"
        effis_p = SCARS_DIR / f"effis_{year}.geojson"
        has_official = _seed_path_ok(official_p)
        has_effis = _seed_path_ok(effis_p)
        if has_official:
            paths.append(official_p)
        if has_effis:
            if prefer_official and has_official:
                print(
                    f"[map_scars_openeo] prefer-official: skipping {effis_p.name} "
                    f"(official_{year} present)",
                    flush=True,
                )
                n_effis_skipped += 1
            else:
                paths.append(effis_p)
        if not paths:
            print(
                f"[map_scars_openeo] No same-year official/EFFIS seed for {year} "
                f"(will try Gencat points if available). "
                f"Year-1 fallback is disabled.",
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
            if seed_year != year:
                print(
                    f"[map_scars_openeo] REFUSING cross-year seed {p.name} "
                    f"(seed_year={seed_year} != {year})",
                    flush=True,
                )
                continue
        source = (
            "official"
            if "official" in p.name.lower()
            else ("effis" if "effis" in p.name.lower() else "aoi")
        )
        gdf["_seed_path"] = p.name
        gdf["_seed_year"] = seed_year
        gdf["_source"] = source
        if source == "effis":
            gdf = _filter_effis_catalonia(gdf)
            if gdf.empty:
                print(f"  no Catalonia EFFIS features left in {p.name}", flush=True)
                continue
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

    # Secondary: Gencat tabular events → municipality centroids (same year only)
    if aoi_path is None:
        gencat = load_gencat_point_seeds(year)
        if gencat is not None and not gencat.empty:
            n_gencat = len(gencat)
            frames.append(gencat)

    print(
        f"[map_scars_openeo] seed sources: official={n_official} "
        f"effis={n_effis} gencat={n_gencat} "
        f"effis_skipped_overlap_or_prefer={n_effis_skipped} "
        f"prefer_official={prefer_official} (same-year only)",
        flush=True,
    )
    return frames

def build_aois(
    year: int,
    max_aois: int,
    aoi_path: Path | None,
    prefer_official: bool = True,
    min_seed_ha: float = MIN_SEED_HA,
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
    skipped_min_seed = 0
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
        # Points: seed geom area is ~0; use reported AREA_HA when present
        if is_point:
            reported = None
            for k in ("AREA_HA", "area_ha", "haforestal", "AREA_HA_"):
                if k in props and props[k] not in (None, ""):
                    try:
                        reported = float(props[k])
                        break
                    except (TypeError, ValueError):
                        pass
            if reported is not None:
                area_ha = reported
            if area_ha < max(min_seed_ha, MIN_POINT_HA):
                print(
                    f"  skip tiny point seed idx={idx} area_ha={area_ha:.2f}",
                    flush=True,
                )
                skipped_min_seed += 1
                continue
        else:
            # Seed polygon area (before buffer) must exceed --min-seed-ha
            if area_ha < min_seed_ha:
                skipped_min_seed += 1
                continue

        src = str(row.get("_source", "aoi"))
        buf = _buffer_m_for(src, area_ha)
        # Points: give a minimum footprint before buffer (~radius for ~5 ha disk ≈ 126 m)
        if is_point:
            geom = geom.buffer(126.0)
            # keep reported/attributed area_ha for ranking; geom now has footprint
        seed_m = geom
        buffered = geom.buffer(buf)
        fire_d = parse_fire_date(props)
        # Same-year seeds only; if a mismatched seed slipped through, drop its date
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

    if skipped_min_seed:
        print(
            f"[map_scars_openeo] skipped {skipped_min_seed} seeds with "
            f"seed area < min_seed_ha={min_seed_ha}",
            flush=True,
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


def build_dnbr_cube(
    connection,
    extent: dict,
    pre: tuple[str, str],
    post_windows: list[tuple[str, str]],
):
    """Build dNBR DataCube: median pre NBR vs multi-post max dNBR.

    For each post window, median clear-sky NBR → dNBR = NBR_pre − NBR_post.
    Per-pixel maximum across post windows recovers burns only visible in some
    clear scenes (cloud gaps). Threshold applied locally when polygonizing.
    """
    if not post_windows:
        raise ValueError("post_windows must be non-empty")

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
    dnbr_max = None
    for i, post in enumerate(post_windows):
        nbr_post = nbr_composite(post)
        dnbr_i = nbr_pre - nbr_post
        if dnbr_max is None:
            dnbr_max = dnbr_i
        else:
            # Per-pixel max: burn signal when any clear post composite shows it
            dnbr_max = dnbr_max.merge_cubes(dnbr_i, overlap_resolver="max")
        print(
            f"    post window [{i + 1}/{len(post_windows)}]: {post[0]} → {post[1]}",
            flush=True,
        )
    return dnbr_max



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


def region_grow_mask(data, threshold_core: float, threshold_grow: float, morph_core: bool = True):
    """Core (dNBR≥core) then keep connected components of (dNBR≥grow) that touch core.

    Morphological opening (1px) is applied to the core only when morph_core=True,
    before growing — light anti-speckle without killing thin burned corridors.
    """
    import numpy as np

    finite = np.isfinite(data)
    core = finite & (data >= threshold_core)
    grow = finite & (data >= threshold_grow)
    if morph_core and core.any():
        n0 = int(core.sum())
        core = morphological_opening(core, iterations=1)
        print(
            f"    morph opening 1px (core only): {n0} → {int(core.sum())}",
            flush=True,
        )
    if not core.any():
        return core
    if not grow.any():
        return core

    try:
        from scipy import ndimage

        labeled, _n = ndimage.label(grow)
        keep_labels = set(int(x) for x in np.unique(labeled[core]) if x != 0)
        out = np.isin(labeled, list(keep_labels)) if keep_labels else core.copy()
    except ImportError:
        # Iterative neighbour grow without scipy
        out = core.copy()
        changed = True
        while changed:
            dilated = _binary_dilate(out, iterations=1)
            add = dilated & grow & ~out
            changed = bool(add.any())
            out = out | add
    n_core = int(core.sum())
    n_out = int(out.sum())
    print(
        f"    region grow: core={n_core} → grown={n_out} "
        f"(core≥{threshold_core}, grow≥{threshold_grow})",
        flush=True,
    )
    return out


def download_worldcover_tiff(connection, extent: dict, out_path: Path) -> Path | None:
    """Download ESA WorldCover 2021 MAP for extent via CDSE openEO (same auth)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cube = connection.load_collection(
            WORLDCOVER_COLLECTION,
            spatial_extent=extent,
            temporal_extent=["2021-01-01", "2021-12-31"],
            bands=["MAP"],
        )
        # Single-date collection; reduce_t if present
        try:
            cube = cube.reduce_dimension(reducer="first", dimension="t")
        except Exception:
            try:
                cube = cube.max_time()
            except Exception:
                pass
        print(
            f"  Downloading {WORLDCOVER_COLLECTION} forest/scrub mask → {out_path.name} …",
            flush=True,
        )
        try:
            cube.download(str(out_path), format="GTiff")
        except TypeError:
            cube.download(str(out_path))
        except Exception:
            # Batch fallback
            work = out_path.parent / f"wc_{out_path.stem}"
            work.mkdir(parents=True, exist_ok=True)
            job = cube.execute_batch(
                outputfile=str(work / "worldcover.tif"),
                out_format="GTiff",
                title=f"cat-cicatrius WorldCover {out_path.stem}",
            )
            tiffs = list(work.glob("*.tif")) + list(work.rglob("*.tif"))
            if not tiffs:
                return None
            src = max(tiffs, key=lambda q: q.stat().st_size)
            import shutil

            shutil.copy(src, out_path)
        if out_path.exists() and out_path.stat().st_size > 100:
            return out_path
    except Exception as e:
        print(
            f"  WARNING: WorldCover download failed ({type(e).__name__}: {e}); "
            "continuing without forest/scrub mask",
            flush=True,
        )
    return None


def apply_forest_scrub_mask(burn_mask, dnbr_transform, dnbr_crs, dnbr_shape, wc_path: Path):
    """Keep burned pixels only on WorldCover tree (10) + shrubland (20)."""
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    with rasterio.open(wc_path) as wc:
        dest = np.zeros(dnbr_shape, dtype=np.uint8)
        reproject(
            source=rasterio.band(wc, 1),
            destination=dest,
            src_transform=wc.transform,
            src_crs=wc.crs,
            dst_transform=dnbr_transform,
            dst_crs=dnbr_crs,
            resampling=Resampling.nearest,
        )
    forest = np.isin(dest, list(WORLDCOVER_FOREST_SCRUB))
    n_before = int(burn_mask.sum())
    out = burn_mask & forest
    n_after = int(out.sum())
    print(
        f"    forest/scrub mask (WorldCover 10+20): burned pixels {n_before} → {n_after} "
        f"(removed {n_before - n_after})",
        flush=True,
    )
    return out


def polygonize_tiff(
    tiff_path: Path,
    threshold_core: float = DNBR_THRESHOLD_CORE,
    threshold_grow: float = DNBR_THRESHOLD_GROW,
    min_ha: float = MIN_PART_HA,
    clip_geom_wgs84=None,
    worldcover_path: Path | None = None,
    morph_core: bool = True,
) -> list[dict]:
    """Polygonize burned pixels via region growing → GeoJSON-like features.

    Core: dNBR ≥ threshold_core; grow contiguous ≥ threshold_grow.
    Optional WorldCover forest/scrub mask. Optional morph opening on core only.
    Drops parts < min_ha. Optionally clips to seed ⊕ CLIP_SEED_BUFFER_M.
    Persists per-part dnbr_mean / dnbr_max and Catalan severity (baixa|moderada|alta).
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
        if nodata is not None:
            data = np.where(data == nodata, np.nan, data.astype("float64"))
        else:
            data = data.astype("float64")

        mask = region_grow_mask(
            data, threshold_core, threshold_grow, morph_core=morph_core
        )
        if not mask.any():
            return []

        if worldcover_path is not None and worldcover_path.exists():
            try:
                mask = apply_forest_scrub_mask(
                    mask, transform, crs, data.shape, worldcover_path
                )
            except Exception as e:
                print(
                    f"    WARNING: forest mask apply failed ({type(e).__name__}: {e})",
                    flush=True,
                )
        if not mask.any():
            return []

        # Connected components as vector parts; stats from dNBR under each part.
        shapes_gen = rio_features.shapes(
            np.ones(mask.shape, dtype=np.uint8),
            mask=mask.astype("uint8"),
            transform=transform,
        )
        geoms = []
        stats = []  # parallel (mean, max)
        for geom, _val in shapes_gen:
            if geom is None:
                continue
            g = shape(geom)
            if g.is_empty or g.area <= 0:
                continue
            inside = ~rio_features.geometry_mask(
                [geom], out_shape=data.shape, transform=transform, invert=False
            )
            vals = data[inside]
            vals = vals[np.isfinite(vals)]
            if vals.size == 0:
                continue
            geoms.append(g)
            stats.append((float(vals.mean()), float(vals.max())))
        if not geoms:
            return []
        # Dissolve touching parts then re-attach max severity among contributors
        # via spatial overlap with original components (keeps mean/max honest).
        merged = unary_union(geoms)
        parts = list(merged.geoms) if merged.geom_type.startswith("Multi") else [merged]
        import geopandas as gpd

        gs = gpd.GeoSeries(parts, crs=crs)
        if crs is None:
            gs = gs.set_crs("EPSG:4326")
        gs = gs.to_crs("EPSG:4326")
        # Original component geoms in WGS84 for stat transfer
        comps = gpd.GeoSeries(geoms, crs=crs)
        if crs is None:
            comps = comps.set_crs("EPSG:4326")
        comps = comps.to_crs("EPSG:4326")

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
            # Aggregate dNBR from overlapping original components (area-weighted mean)
            w_sum = 0.0
            mean_acc = 0.0
            max_acc = None
            for comp, (mn, mx) in zip(comps, stats):
                try:
                    inter = g_wgs.intersection(comp)
                except Exception:
                    continue
                if inter is None or inter.is_empty:
                    continue
                w = float(inter.area)
                if w <= 0:
                    continue
                mean_acc += mn * w
                w_sum += w
                max_acc = mx if max_acc is None else max(max_acc, mx)
            if w_sum > 0:
                dnbr_mean = mean_acc / w_sum
                dnbr_max = float(max_acc) if max_acc is not None else dnbr_mean
            else:
                dnbr_mean = None
                dnbr_max = None
            sev = severity_from_dnbr(dnbr_mean)
            feats.append(
                {
                    "type": "Feature",
                    "geometry": mapping(g_wgs),
                    "properties": {
                        "area_ha": round(area_ha, 3),
                        "dnbr_threshold_core": threshold_core,
                        "dnbr_threshold_grow": threshold_grow,
                        "dnbr_mean": None if dnbr_mean is None else round(float(dnbr_mean), 4),
                        "dnbr_max": None if dnbr_max is None else round(float(dnbr_max), 4),
                        "severity": sev,
                        "severity_method": "dnbr_mean" if sev else None,
                        "forest_mask": "esa_worldcover_2021_10_20"
                        if worldcover_path
                        else None,
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
    threshold_core: float = DNBR_THRESHOLD_CORE,
    threshold_grow: float = DNBR_THRESHOLD_GROW,
    min_ha: float = MIN_PART_HA,
    forest_mask: bool = True,
    morph_core: bool = True,
    post_windows: int = 2,
) -> Path | None:
    pre, posts = windows_for_fire(aoi.fire_date, year, n_post_windows=post_windows)
    today = utc_today()
    if not posts:
        print(
            f"  SKIP AOI {aoi.aoi_id}: no valid post window after clipping to "
            f"UTC today={today.isoformat()} (fire_date={aoi.fire_date})",
            flush=True,
        )
        return None
    if pre is None:
        print(
            f"  SKIP AOI {aoi.aoi_id}: no valid pre window after clipping to "
            f"UTC today={today.isoformat()} (fire_date={aoi.fire_date})",
            flush=True,
        )
        return None
    extent = spatial_extent(aoi.geometry)
    posts_str = "; ".join(f"{a}→{b}" for a, b in posts)
    print(
        f"  AOI {aoi.aoi_id}: area_buf≈{aoi.area_ha:.1f} ha source={aoi.source} "
        f"buffer_m={aoi.buffer_m:.0f} fire_date={aoi.fire_date} pre={pre} "
        f"post_windows({len(posts)})=[{posts_str}] today_utc={today.isoformat()} "
        f"extent={extent}",
        flush=True,
    )
    if dry_run:
        return None

    cube = build_dnbr_cube(connection, extent, pre, posts)
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
        tiffs = [
            q
            for q in (list(work.glob("*.tif")) + list(work.glob("*.tiff"))
                      + list(work.rglob("*.tif")) + list(work.rglob("*.tiff")))
            if "worldcover" not in q.name.lower()
        ]
        # de-dupe paths
        seen = set()
        uniq = []
        for q in tiffs:
            r = q.resolve()
            if r not in seen:
                seen.add(r)
                uniq.append(q)
        tiffs = uniq
        if not tiffs:
            print(f"  WARNING: no GeoTIFF for AOI {aoi.aoi_id}", flush=True)
            return None
        preferred = [q for q in tiffs if q.name.lower().startswith("dnbr")]
        tiff = max(preferred or tiffs, key=lambda q: q.stat().st_size)
        wc_path = None
        if forest_mask:
            wc_path = download_worldcover_tiff(
                connection, extent, work / "worldcover.tif"
            )
        print(
            f"  Polygonizing {tiff.name} ({tiff.stat().st_size} bytes) "
            f"core={threshold_core} grow={threshold_grow} min_ha={min_ha} "
            f"clip_seed+{CLIP_SEED_BUFFER_M:.0f}m "
            f"forest_mask={'yes' if wc_path else 'no'} …",
            flush=True,
        )
        feats = polygonize_tiff(
            tiff,
            threshold_core=threshold_core,
            threshold_grow=threshold_grow,
            min_ha=min_ha,
            clip_geom_wgs84=aoi.seed_geometry,
            worldcover_path=wc_path,
            morph_core=morph_core,
        )
        for f in feats:
            # One fire_id per disconnected polygon part (Explorer groups by fire_id)
            part_i = f["properties"].get("part")
            if part_i is None:
                part_i = 0
            f["properties"]["aoi_id"] = aoi.aoi_id
            f["properties"]["seed_id"] = aoi.aoi_id
            f["properties"]["fire_id"] = f"{aoi.aoi_id}_{part_i}"
            f["properties"]["burn_year"] = year
            f["properties"]["source"] = "sentinel"
            f["properties"]["aoi_source"] = aoi.source
            f["properties"]["dnbr_threshold_core"] = threshold_core
            f["properties"]["dnbr_threshold_grow"] = threshold_grow
            f["properties"]["dnbr_threshold"] = threshold_core  # compat
            f["properties"]["buffer_m"] = aoi.buffer_m
            f["properties"]["post_windows"] = len(posts)
            f["properties"]["post_window_dates"] = [
                {"start": a, "end": b} for a, b in posts
            ]
            f["properties"]["pre_window"] = {"start": pre[0], "end": pre[1]}
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
    ap.add_argument("--year", type=int, default=date.today().year, help="Burn year (e.g. 2024|2025|2026)")
    ap.add_argument(
        "--max-aois",
        type=int,
        default=0,
        help="Cap AOIs (largest first); 0 = no cap / process all that pass min-seed-ha",
    )
    ap.add_argument("--aoi", type=Path, default=None, help="Optional GeoJSON AOI override")
    ap.add_argument(
        "--threshold-core",
        type=float,
        default=None,
        help=f"dNBR core threshold for region growing (default {DNBR_THRESHOLD_CORE})",
    )
    ap.add_argument(
        "--threshold-grow",
        type=float,
        default=None,
        help=f"dNBR grow threshold contiguous to core (default {DNBR_THRESHOLD_GROW})",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Alias: sets both --threshold-core and --threshold-grow to this value "
        f"(legacy single-threshold mode). Default core={DNBR_THRESHOLD_CORE} grow={DNBR_THRESHOLD_GROW}",
    )
    ap.add_argument(
        "--min-ha",
        type=float,
        default=MIN_PART_HA,
        help=f"Drop polygonized parts smaller than this (default {MIN_PART_HA})",
    )
    ap.add_argument(
        "--min-seed-ha",
        type=float,
        default=MIN_SEED_HA,
        help=f"Skip fire seeds whose seed-geometry area (ha, before buffer) "
        f"is below this (default {MIN_SEED_HA})",
    )
    ap.add_argument(
        "--prefer-official",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer official seeds; skip same-year EFFIS when official exists (default: true)",
    )
    ap.add_argument(
        "--forest-mask",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mask burned pixels to ESA WorldCover tree+shrub (10,20) via CDSE openEO (default: true)",
    )
    ap.add_argument(
        "--morph-core",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Light 1px morphological opening on core before grow (default: true)",
    )
    ap.add_argument(
        "--post-windows",
        type=int,
        default=2,
        help="Number of sequential post-fire composites; per-pixel max dNBR "
        "(default 2: +7–30/+30–60d after fire_date, or seasonal slices; "
        "use 3 for +60–90d too)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-skip", action="store_true", help="Always skip (CI smoke)")
    args = ap.parse_args()

    # Resolve thresholds: --threshold sets both; else core/grow defaults
    if args.threshold is not None:
        threshold_core = float(args.threshold)
        threshold_grow = float(args.threshold)
    else:
        threshold_core = (
            float(args.threshold_core)
            if args.threshold_core is not None
            else DNBR_THRESHOLD_CORE
        )
        threshold_grow = (
            float(args.threshold_grow)
            if args.threshold_grow is not None
            else DNBR_THRESHOLD_GROW
        )
    if threshold_grow > threshold_core:
        print(
            f"[map_scars_openeo] WARNING: grow ({threshold_grow}) > core ({threshold_core}); "
            "swapping",
            flush=True,
        )
        threshold_core, threshold_grow = threshold_grow, threshold_core

    if args.force_skip:
        return skip("--force-skip")

    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError as e:
        return skip(f"geometry deps missing: {e}")

    n_post = max(1, int(args.post_windows))
    print(
        f"[map_scars_openeo] year={args.year} max_aois={args.max_aois} "
        f"(0=unlimited) threshold_core={threshold_core} "
        f"threshold_grow={threshold_grow} min_ha={args.min_ha} "
        f"min_seed_ha={args.min_seed_ha} prefer_official={args.prefer_official} "
        f"forest_mask={args.forest_mask} morph_core={args.morph_core} "
        f"post_windows={n_post} "
        f"buffer_official_m={BUFFER_OFFICIAL_M} buffer_effis_m={BUFFER_EFFIS_M} "
        f"clip_seed_buffer_m={CLIP_SEED_BUFFER_M} dry_run={args.dry_run}",
        flush=True,
    )
    print(
        "[map_scars_openeo] region-grow + forest + multi-post settings: "
        f"core≥{threshold_core} grow≥{threshold_grow}, "
        f"clip_seed_buffer_m={CLIP_SEED_BUFFER_M}, "
        f"search_buffer_official_m={BUFFER_OFFICIAL_M}, "
        f"search_buffer_effis_m={BUFFER_EFFIS_M}, "
        f"min_ha={args.min_ha}, morph_core={args.morph_core}, "
        f"post_windows={n_post} (per-pixel max dNBR across post composites), "
        f"forest_mask={args.forest_mask} ({WORLDCOVER_COLLECTION} classes "
        f"{sorted(WORLDCOVER_FOREST_SCRUB)})",
        flush=True,
    )

    aois = build_aois(
        args.year,
        args.max_aois,
        args.aoi,
        prefer_official=args.prefer_official,
        min_seed_ha=float(args.min_seed_ha),
    )
    if not aois:
        # Last resort: do NOT process all of Catalonia as one cube
        return skip(
            "no usable AOIs from official/effis (or --aoi). "
            "Refusing full-Catalonia cube to save credits."
        )

    today = utc_today()
    print(
        f"[map_scars_openeo] UTC today={today.isoformat()} "
        f"(all temporal extents clipped to today)",
        flush=True,
    )
    usable: list[Aoi] = []
    for a in aois:
        pre_w, post_w = windows_for_fire(a.fire_date, args.year, n_post_windows=n_post)
        if not post_w:
            print(
                f"  - SKIP {a.aoi_id}: no post window after clip to {today.isoformat()} "
                f"(fire={a.fire_date}, src={a.source})",
                flush=True,
            )
            continue
        if pre_w is None:
            print(
                f"  - SKIP {a.aoi_id}: no pre window after clip to {today.isoformat()} "
                f"(fire={a.fire_date}, src={a.source})",
                flush=True,
            )
            continue
        posts_str = ", ".join(f"{x}→{y}" for x, y in post_w)
        print(
            f"  - {a.aoi_id}: ~{a.area_ha:.0f} ha buf, buffer_m={a.buffer_m:.0f}, "
            f"fire={a.fire_date}, src={a.source}, pre={pre_w[0]}→{pre_w[1]}, "
            f"posts=[{posts_str}]",
            flush=True,
        )
        usable.append(a)
    aois = usable
    print(f"[map_scars_openeo] {len(aois)} AOIs queued after temporal clip", flush=True)
    if not aois:
        return skip(
            f"all AOIs lacked valid pre/post windows after clipping to UTC today="
            f"{today.isoformat()}"
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
            threshold_core=threshold_core,
            threshold_grow=threshold_grow,
            min_ha=args.min_ha,
            forest_mask=args.forest_mask,
            morph_core=args.morph_core,
            post_windows=n_post,
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
