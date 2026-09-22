#!/usr/bin/env python3
"""Optional EFFIS WFS helper — burn areas for Catalonia bbox (no auth).

WFS 1.0.0 → lon,lat axis order. Prefer year layer `modis.ba.poly.{year}`;
if missing (common for the unfinished current season), fall back to the
rolling `modis.ba.poly` layer and filter features by FIREDATE year.

Service: https://maps.effis.emergency.copernicus.eu/effis
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCARS_DIR = ROOT / "scars"
WFS_BASE = "https://maps.effis.emergency.copernicus.eu/effis"
# Catalonia approximate bbox (lon,lat for WFS 1.0.0)
BBOX = (0.15, 40.5, 3.35, 42.9)


def wfs_url(layer: str, bbox: tuple[float, float, float, float], output_format: str) -> str:
    minx, miny, maxx, maxy = bbox
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": layer,
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "outputFormat": output_format,
    }
    return f"{WFS_BASE}?{urllib.parse.urlencode(params)}"


def fetch(url: str) -> bytes:
    print(f"GET {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "cat-cicatrius/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def maybe_swap_lonlat_geojson(obj: dict) -> dict:
    """EFFIS sometimes returns GeoJSON with (lat, lon) despite WFS 1.0.0 bbox.

    Detect Catalonia-like inverted bounds (x≈40–43, y≈0–4) and swap rings.
    """

    def swap_coords(coords):
        if not coords:
            return coords
        if isinstance(coords[0], (int, float)):
            if len(coords) >= 2:
                return [coords[1], coords[0], *coords[2:]]
            return coords
        return [swap_coords(c) for c in coords]

    feats = obj.get("features") or []
    if not feats:
        return obj
    g0 = feats[0].get("geometry") or {}
    coords = g0.get("coordinates")
    if not coords:
        return obj
    pt = coords
    while isinstance(pt, list) and pt and isinstance(pt[0], list):
        pt = pt[0]
    if not (isinstance(pt, list) and len(pt) >= 2):
        return obj
    x, y = float(pt[0]), float(pt[1])
    if 39.0 <= x <= 44.0 and -2.0 <= y <= 5.0:
        print("  Detected lat,lon axis order — swapping to lon,lat", flush=True)
        for f in feats:
            geom = f.get("geometry")
            if geom and "coordinates" in geom:
                geom["coordinates"] = swap_coords(geom["coordinates"])
    return obj


def try_formats(layer: str) -> tuple[str, bytes] | None:
    for fmt in ("GEOJSON", "application/json", "json", "GML2", "GML3"):
        url = wfs_url(layer, BBOX, fmt)
        try:
            data = fetch(url)
        except urllib.error.HTTPError as e:
            print(f"  {fmt}: HTTP {e.code}", flush=True)
            continue
        except Exception as e:
            print(f"  {fmt}: {e}", flush=True)
            continue
        if not data or len(data) < 20:
            print(f"  {fmt}: empty", flush=True)
            continue
        head = data[:200].decode("utf-8", errors="ignore").lower()
        if "exception" in head and "featurecollection" not in head:
            print(f"  {fmt}: exception report", flush=True)
            continue
        print(f"  {fmt}: {len(data)} bytes OK", flush=True)
        return fmt, data
    return None


def filter_geojson_by_year(obj: dict, year: int) -> dict:
    year_s = str(year)
    kept = []
    for f in obj.get("features") or []:
        props = f.get("properties") or {}
        raw = (
            props.get("FIREDATE")
            or props.get("FINALDATE")
            or props.get("fire_date")
            or props.get("DATE")
            or ""
        )
        if str(raw).startswith(year_s):
            kept.append(f)
    out = dict(obj)
    out["features"] = kept
    out["name"] = f"modis.ba.poly.{year}"
    return out


def write_geojson(out: Path, obj: dict) -> None:
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.write("\n")


def fetch_year_layer(year: int, layer_prefix: str, out_dir: Path) -> bool:
    """Try year-specific layer, then rolling modis.ba.poly filtered by year."""
    candidates = [
        f"{layer_prefix}.{year}",
    ]
    alternate_layers = [
        # Tried only if year layer + rolling fallback both fail
        f"viirs.ba.poly.{year}",
        f"mw.ba.poly.{year}",
    ]
    for layer in candidates:
        print(f"EFFIS layer {layer}", flush=True)
        result = try_formats(layer)
        if result is None:
            print(f"  no usable response for {layer}", flush=True)
            continue
        fmt, data = result
        if "json" not in fmt.lower():
            out = out_dir / f"effis_{year}.gml"
            out.write_bytes(data)
            print(f"Wrote {out} (from {layer})", flush=True)
            return True
        try:
            obj = json.loads(data.decode("utf-8"))
            obj = maybe_swap_lonlat_geojson(obj)
        except json.JSONDecodeError:
            out = out_dir / f"effis_{year}.geojson"
            out.write_bytes(data)
            print(f"Wrote {out} (raw, from {layer})", flush=True)
            return True
        n = len(obj.get("features") or [])
        out = out_dir / f"effis_{year}.geojson"
        write_geojson(out, obj)
        print(
            f"Wrote {out} ({n} features) via year layer '{layer}'",
            flush=True,
        )
        return True

    # Fallback: rolling all-years layer filtered by FIREDATE year
    rolling = "modis.ba.poly"
    print(
        f"EFFIS fallback: layer {rolling} filtered to FIREDATE year={year}",
        flush=True,
    )
    result = try_formats(rolling)
    if result is None:
        print(f"  no usable response for {rolling}", flush=True)
        for layer in alternate_layers:
            print(f"EFFIS alternate layer {layer}", flush=True)
            alt = try_formats(layer)
            if alt is None:
                continue
            fmt, data = alt
            if "json" not in fmt.lower():
                continue
            try:
                obj = json.loads(data.decode("utf-8"))
                obj = maybe_swap_lonlat_geojson(obj)
            except json.JSONDecodeError:
                continue
            out = out_dir / f"effis_{year}.geojson"
            write_geojson(out, obj)
            n = len(obj.get("features") or [])
            print(f"Wrote {out} ({n} features) via alternate '{layer}'", flush=True)
            return True
        return False
    fmt, data = result
    if "json" not in fmt.lower():
        print("  rolling layer returned non-JSON; cannot filter by year", flush=True)
        return False
    try:
        obj = json.loads(data.decode("utf-8"))
        obj = maybe_swap_lonlat_geojson(obj)
    except json.JSONDecodeError as e:
        print(f"  JSON decode failed: {e}", flush=True)
        return False
    n_all = len(obj.get("features") or [])
    filtered = filter_geojson_by_year(obj, year)
    n = len(filtered.get("features") or [])
    if n == 0:
        print(
            f"  rolling layer had {n_all} features in bbox but 0 for year {year}",
            flush=True,
        )
        return False
    out = out_dir / f"effis_{year}.geojson"
    write_geojson(out, filtered)
    print(
        f"Wrote {out} ({n}/{n_all} features) via rolling '{rolling}' "
        f"filtered to {year}",
        flush=True,
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, nargs="*", default=None)
    ap.add_argument("--layer-prefix", default="modis.ba.poly")
    ap.add_argument("--out-dir", type=Path, default=SCARS_DIR)
    args = ap.parse_args()

    from datetime import date

    years = args.years or [date.today().year - 1, date.today().year]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for y in years:
        if fetch_year_layer(y, args.layer_prefix, args.out_dir):
            written += 1
        else:
            print(f"  no usable EFFIS data for {y} (best-effort skip)", flush=True)

    if written == 0:
        print("EFFIS: no layers written (non-fatal).", flush=True)
    else:
        print(f"EFFIS: wrote {written} year file(s).", flush=True)
    return 0  # always soft-fail for seasonal pipeline


if __name__ == "__main__":
    raise SystemExit(main())
