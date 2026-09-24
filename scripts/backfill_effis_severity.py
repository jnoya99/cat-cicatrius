#!/usr/bin/env python3
"""Backfill real Sentinel-2 dNBR severity onto CAT EFFIS scar features.

Product policy (hard):
  - Only *compute* dNBR for EFFIS footprints in Catalonia
    (COUNTRY=ES + PROVINCE in Barcelona/Girona/Lleida/Tarragona).
  - Never drop or rewrite non-CAT EFFIS features in the GeoJSON.
  - Never invent proxy severity (no fake "moderada").
  - Leave severity null when CDSE fails or there are no valid imagery pixels.
  - Do not touch Sentinel scars; do not change Bolets Explorador HTML.

Pipeline per CAT EFFIS feature (idempotent unless --force):
  1. Reuse map_scars_openeo windows + build_dnbr_cube (same CDSE openEO path).
  2. Sample mean/max dNBR *inside the EFFIS polygon* (optional WorldCover
     forest/scrub mask). No region-grow scar extraction — values below the
     scar core threshold still map via severity_from_dnbr.
  3. Attach dnbr_mean / dnbr_max / severity / severity_method=
     dnbr_mean_effis_backfill onto that feature's properties.

Requires CDSE_CLIENT_ID + CDSE_CLIENT_SECRET (same as map_scars_openeo).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import map_scars_openeo as mso  # noqa: E402

SCARS_DIR = mso.SCARS_DIR
TMP_DIR = ROOT / ".tmp" / "effis_sev_backfill"
SEVERITY_METHOD = "dnbr_mean_effis_backfill"
REAL_DNBR_METHODS = frozenset(
    {"dnbr_mean", "dnbr_max", "dnbr_mean_effis_backfill"}
)


def _is_cat_props(props: dict) -> bool:
    country = str(props.get("COUNTRY") or props.get("country") or "").strip().upper()
    province = str(props.get("PROVINCE") or props.get("province") or "").strip()
    if not country or not province:
        return False
    return country == "ES" and province in mso.CAT_PROVINCES_ES


def _feature_id(props: dict, idx: int) -> str:
    return mso._feature_id(props, Path(f"effis_{idx}"), idx)


def _already_has_real_dnbr(props: dict) -> bool:
    method = props.get("severity_method")
    dnbr = props.get("dnbr_mean")
    if dnbr is None:
        dnbr = props.get("dnbr")
    try:
        if dnbr is not None and dnbr != "" and float(dnbr) == float(dnbr):
            if method in REAL_DNBR_METHODS or (
                isinstance(method, str) and str(method).startswith("dnbr_mean")
            ):
                return True
    except (TypeError, ValueError):
        pass
    return False


def _geom_area_ha(geom) -> float:
    import geopandas as gpd

    if geom is None or getattr(geom, "is_empty", True):
        return 0.0
    s = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(mso.AREA_CRS)
    return float(s.iloc[0].area / 10_000.0)


def sample_dnbr_in_polygon(
    tiff_path: Path,
    geom_wgs84,
    worldcover_path: Path | None = None,
) -> dict[str, Any]:
    """Mean/max dNBR of valid pixels inside geom (optional forest/scrub mask)."""
    import numpy as np
    import rasterio
    from rasterio import features as rio_features
    from rasterio.warp import Resampling, reproject
    from shapely.geometry import mapping
    import geopandas as gpd

    out: dict[str, Any] = {
        "dnbr_mean": None,
        "dnbr_max": None,
        "n_valid": 0,
        "n_forest_valid": 0,
        "used_forest_mask": False,
    }
    if geom_wgs84 is None or getattr(geom_wgs84, "is_empty", True):
        return out

    with rasterio.open(tiff_path) as src:
        data = src.read(1).astype("float64")
        transform = src.transform
        crs = src.crs
        nodata = src.nodata
        if nodata is not None:
            data = np.where(data == nodata, np.nan, data)

        g_s = gpd.GeoSeries([geom_wgs84], crs="EPSG:4326")
        if crs is not None:
            g_s = g_s.to_crs(crs)
        poly = g_s.iloc[0]
        if poly is None or poly.is_empty:
            return out
        inside = ~rio_features.geometry_mask(
            [mapping(poly)],
            out_shape=data.shape,
            transform=transform,
            invert=False,
        )
        vals_all = data[inside]
        vals_all = vals_all[np.isfinite(vals_all)]
        out["n_valid"] = int(vals_all.size)
        if vals_all.size == 0:
            return out

        vals = vals_all
        if worldcover_path is not None and worldcover_path.exists():
            try:
                with rasterio.open(worldcover_path) as wc:
                    dest = np.zeros(data.shape, dtype=np.uint8)
                    reproject(
                        source=rasterio.band(wc, 1),
                        destination=dest,
                        src_transform=wc.transform,
                        src_crs=wc.crs,
                        dst_transform=transform,
                        dst_crs=crs,
                        resampling=Resampling.nearest,
                    )
                forest = np.isin(dest, list(mso.WORLDCOVER_FOREST_SCRUB))
                vals_f = data[inside & forest]
                vals_f = vals_f[np.isfinite(vals_f)]
                out["n_forest_valid"] = int(vals_f.size)
                if vals_f.size > 0:
                    vals = vals_f
                    out["used_forest_mask"] = True
            except Exception as e:
                print(
                    f"    WARNING: forest mask sample failed "
                    f"({type(e).__name__}: {e}); using all valid pixels",
                    flush=True,
                )

        out["dnbr_mean"] = round(float(vals.mean()), 4)
        out["dnbr_max"] = round(float(vals.max()), 4)
    return out


def download_dnbr_tiff(
    connection,
    extent: dict,
    pre: tuple[str, str],
    posts: list[tuple[str, str]],
    work: Path,
    title: str,
) -> Path | None:
    work.mkdir(parents=True, exist_ok=True)
    out_tif = work / "dnbr.tif"
    if out_tif.exists() and out_tif.stat().st_size > 100:
        print(f"    reusing cached {out_tif}", flush=True)
        return out_tif

    cube = mso.build_dnbr_cube(connection, extent, pre, posts)
    try:
        try:
            job = cube.execute_batch(
                outputfile=str(out_tif),
                out_format="GTiff",
                title=title,
                description=title,
            )
            job_id = getattr(job, "job_id", None)
        except TypeError:
            job = cube.create_job(out_format="GTiff", title=title)
            job_id = getattr(job, "job_id", None) or str(job)
            print(f"    Job id={job_id} start_and_wait …", flush=True)
            job.start_and_wait()
            results = job.get_results()
            results.download_files(str(work))
        if job_id:
            print(f"    Job finished id={job_id}", flush=True)
    except Exception as e:
        print(
            f"    CDSE job failed: {type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return None

    tiffs = [
        q
        for q in (
            list(work.glob("*.tif"))
            + list(work.glob("*.tiff"))
            + list(work.rglob("*.tif"))
            + list(work.rglob("*.tiff"))
        )
        if "worldcover" not in q.name.lower()
    ]
    seen: set = set()
    uniq = []
    for q in tiffs:
        r = q.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(q)
    if not uniq:
        return None
    preferred = [q for q in uniq if q.name.lower().startswith("dnbr")]
    src = max(preferred or uniq, key=lambda q: q.stat().st_size)
    if src.resolve() != out_tif.resolve():
        import shutil

        shutil.copy(src, out_tif)
    return out_tif if out_tif.exists() and out_tif.stat().st_size > 100 else None


def backfill_year(
    year: int,
    *,
    dry_run: bool = False,
    force: bool = False,
    forest_mask: bool = True,
    post_windows: int = 2,
    max_features: int = 0,
    min_ha: float = 0.0,
) -> dict[str, Any]:
    from shapely.geometry import shape

    path = SCARS_DIR / f"effis_{year}.geojson"
    if not path.exists() or path.stat().st_size < 20:
        print(f"[backfill_effis_severity] missing {path}", flush=True)
        return {"year": year, "error": "missing_effis", "updated": 0}

    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    features = obj.get("features") or []
    print(
        f"[backfill_effis_severity] year={year} features={len(features)} "
        f"force={force} forest_mask={forest_mask} post_windows={post_windows} "
        f"min_ha={min_ha} dry_run={dry_run}",
        flush=True,
    )

    candidates: list[tuple[int, dict]] = []
    n_cat = 0
    n_skip_done = 0
    n_skip_tiny = 0
    for i, feat in enumerate(features):
        props = feat.get("properties") or {}
        if not _is_cat_props(props):
            continue
        n_cat += 1
        if not force and _already_has_real_dnbr(props):
            n_skip_done += 1
            continue
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty:
            continue
        area_ha = _geom_area_ha(g)
        if area_ha < float(min_ha):
            n_skip_tiny += 1
            continue
        candidates.append((i, feat))

    def _area_key(item):
        _, feat = item
        try:
            return _geom_area_ha(shape(feat["geometry"]))
        except Exception:
            return 0.0

    candidates.sort(key=_area_key, reverse=True)
    if max_features and max_features > 0:
        candidates = candidates[: int(max_features)]

    print(
        f"  CAT features={n_cat}; already real dNBR={n_skip_done}; "
        f"below min_ha={n_skip_tiny}; queued={len(candidates)}",
        flush=True,
    )

    stats: dict[str, Any] = {
        "year": year,
        "n_features": len(features),
        "n_cat": n_cat,
        "n_queued": len(candidates),
        "updated": 0,
        "ok_with_severity": 0,
        "ok_null_low_dnbr": 0,
        "failed_no_imagery": 0,
        "failed_no_window": 0,
        "failed_job": 0,
        "by_severity": {"baixa": 0, "moderada": 0, "alta": 0},
    }

    if not candidates:
        return stats

    if dry_run:
        for i, feat in candidates:
            props = feat.get("properties") or {}
            fid = _feature_id(props, i)
            fd = mso.parse_fire_date(props)
            pre, posts = mso.windows_for_fire(fd, year, n_post_windows=post_windows)
            print(
                f"  DRY {fid}: fire={fd} pre={pre} posts={posts} "
                f"area≈{_area_key((i, feat)):.1f} ha",
                flush=True,
            )
        return stats

    if not mso.credentials_present():
        print(
            "[backfill_effis_severity] SKIP: CDSE_CLIENT_ID/SECRET not set",
            flush=True,
        )
        stats["error"] = "no_credentials"
        return stats

    try:
        connection = mso.connect_cdse()
    except Exception as e:
        print(
            f"[backfill_effis_severity] AUTH ERROR: {e}",
            file=sys.stderr,
            flush=True,
        )
        stats["error"] = "auth_failed"
        return stats

    today = mso.utc_today()
    print(f"  UTC today={today.isoformat()}", flush=True)

    for n_done, (i, feat) in enumerate(candidates, 1):
        props = feat.setdefault("properties", {})
        fid = _feature_id(props, i)
        try:
            g = shape(feat["geometry"])
        except Exception as e:
            print(f"  [{n_done}/{len(candidates)}] {fid}: bad geom ({e})", flush=True)
            stats["failed_no_imagery"] += 1
            continue
        fd = mso.parse_fire_date(props)
        pre, posts = mso.windows_for_fire(fd, year, n_post_windows=post_windows)
        if not posts or pre is None:
            print(
                f"  [{n_done}/{len(candidates)}] SKIP {fid}: no pre/post window "
                f"(fire={fd})",
                flush=True,
            )
            stats["failed_no_window"] += 1
            continue

        extent = mso.spatial_extent(g)
        area_ha = _geom_area_ha(g)
        print(
            f"  [{n_done}/{len(candidates)}] {fid}: area≈{area_ha:.1f} ha "
            f"fire={fd} pre={pre} posts={posts} extent={extent}",
            flush=True,
        )
        work = TMP_DIR / str(year) / f"job_{fid}"
        tiff = download_dnbr_tiff(
            connection,
            extent,
            pre,
            posts,
            work,
            title=f"cat-cicatrius EFFIS sev backfill {year} {fid}",
        )
        if tiff is None:
            stats["failed_job"] += 1
            continue

        wc_path = None
        if forest_mask:
            wc_path = mso.download_worldcover_tiff(
                connection, extent, work / "worldcover.tif"
            )

        sample = sample_dnbr_in_polygon(tiff, g, worldcover_path=wc_path)
        dnbr_mean = sample["dnbr_mean"]
        dnbr_max = sample["dnbr_max"]
        sev = mso.severity_from_dnbr(dnbr_mean)

        props["dnbr_mean"] = dnbr_mean
        props["dnbr_max"] = dnbr_max
        props["severity"] = sev
        props["severity_method"] = (
            SEVERITY_METHOD if dnbr_mean is not None else None
        )
        props["dnbr_n_valid"] = sample["n_valid"]
        props["dnbr_n_forest_valid"] = sample["n_forest_valid"]
        props["dnbr_used_forest_mask"] = bool(sample["used_forest_mask"])
        props["dnbr_backfill_year"] = year
        props["pre_window"] = {"start": pre[0], "end": pre[1]}
        props["post_window_dates"] = [
            {"start": a, "end": b} for a, b in posts
        ]
        if fd:
            props["fire_date"] = fd.isoformat()

        stats["updated"] += 1
        if sev:
            stats["ok_with_severity"] += 1
            stats["by_severity"][sev] = stats["by_severity"].get(sev, 0) + 1
        elif dnbr_mean is not None:
            stats["ok_null_low_dnbr"] += 1
        else:
            stats["failed_no_imagery"] += 1

        print(
            f"    → dnbr_mean={dnbr_mean} dnbr_max={dnbr_max} severity={sev} "
            f"n_valid={sample['n_valid']} forest_valid={sample['n_forest_valid']} "
            f"forest_mask={sample['used_forest_mask']}",
            flush=True,
        )

    # Write full FeatureCollection (non-CAT untouched; CAT updated in place)
    SCARS_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.write("\n")
    print(
        f"[backfill_effis_severity] wrote {path} "
        f"(updated={stats['updated']} with_sev={stats['ok_with_severity']} "
        f"low_dnbr_null={stats['ok_null_low_dnbr']} "
        f"no_window={stats['failed_no_window']} job_fail={stats['failed_job']} "
        f"no_imagery={stats['failed_no_imagery']})",
        flush=True,
    )

    stats_path = TMP_DIR / f"stats_{year}.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
        f.write("\n")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--years",
        type=str,
        default="2025,2026",
        help="Comma-separated burn years (default 2025,2026)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Recompute even when feature already has real dNBR severity",
    )
    ap.add_argument(
        "--forest-mask",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer ESA WorldCover tree+shrub pixels for mean (default: true)",
    )
    ap.add_argument("--post-windows", type=int, default=2)
    ap.add_argument(
        "--max-features",
        type=int,
        default=0,
        help="Cap CAT features per year (largest first); 0 = all",
    )
    ap.add_argument(
        "--min-ha",
        type=float,
        default=0.0,
        help="Skip CAT features below this seed area (ha); default 0 = all",
    )
    args = ap.parse_args()

    years = []
    for part in str(args.years).split(","):
        part = part.strip()
        if part:
            years.append(int(part))
    if not years:
        print("No years given", file=sys.stderr)
        return 2

    all_stats = []
    for y in years:
        st = backfill_year(
            y,
            dry_run=args.dry_run,
            force=args.force,
            forest_mask=args.forest_mask,
            post_windows=max(1, int(args.post_windows)),
            max_features=int(args.max_features),
            min_ha=float(args.min_ha),
        )
        all_stats.append(st)

    summary_path = TMP_DIR / "stats_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(all_stats, f, indent=2)
        f.write("\n")
    print(f"[backfill_effis_severity] summary → {summary_path}", flush=True)
    for st in all_stats:
        print(json.dumps(st, ensure_ascii=False), flush=True)

    if any(st.get("error") == "auth_failed" for st in all_stats):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
