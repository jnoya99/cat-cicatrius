#!/usr/bin/env python3
"""Optional EFFIS WFS helper — burn areas for Catalonia bbox (no auth).

WFS 1.0.0 → lon,lat axis order. Layers like modis.ba.poly.{year}.
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
            # [x, y] or [x, y, z]
            if len(coords) >= 2:
                return [coords[1], coords[0], *coords[2:]]
            return coords
        return [swap_coords(c) for c in coords]

    feats = obj.get("features") or []
    if not feats:
        return obj
    # Probe first geometry exterior
    g0 = (feats[0].get("geometry") or {})
    coords = g0.get("coordinates")
    if not coords:
        return obj
    # Flatten first point
    pt = coords
    while isinstance(pt, list) and pt and isinstance(pt[0], list):
        pt = pt[0]
    if not (isinstance(pt, list) and len(pt) >= 2):
        return obj
    x, y = float(pt[0]), float(pt[1])
    # In Catalonia, lon is ~0–4 and lat ~40–43. If swapped, x looks like lat.
    if 39.0 <= x <= 44.0 and -2.0 <= y <= 5.0:
        print("  Detected lat,lon axis order — swapping to lon,lat", flush=True)
        for f in feats:
            geom = f.get("geometry")
            if geom and "coordinates" in geom:
                geom["coordinates"] = swap_coords(geom["coordinates"])
    return obj


def try_formats(layer: str) -> tuple[str, bytes] | None:
    # Prefer JSON; fall back to GML
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
        # Reject obvious exception reports
        head = data[:200].decode("utf-8", errors="ignore").lower()
        if "exception" in head and "featurecollection" not in head:
            print(f"  {fmt}: exception report", flush=True)
            continue
        print(f"  {fmt}: {len(data)} bytes OK", flush=True)
        return fmt, data
    return None


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
        layer = f"{args.layer_prefix}.{y}"
        print(f"EFFIS layer {layer}", flush=True)
        result = try_formats(layer)
        if result is None:
            print(f"  no usable response for {layer} (best-effort skip)", flush=True)
            continue
        fmt, data = result
        if "json" in fmt.lower():
            out = args.out_dir / f"effis_{y}.geojson"
            # Validate / pretty if possible
            try:
                obj = json.loads(data.decode("utf-8"))
                obj = maybe_swap_lonlat_geojson(obj)
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False)
                    f.write("\n")
            except json.JSONDecodeError:
                out.write_bytes(data)
        else:
            out = args.out_dir / f"effis_{y}.gml"
            out.write_bytes(data)
        print(f"Wrote {out}", flush=True)
        written += 1

    if written == 0:
        print("EFFIS: no layers written (non-fatal).", flush=True)
    return 0  # always soft-fail for seasonal pipeline


if __name__ == "__main__":
    raise SystemExit(main())
