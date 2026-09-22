#!/usr/bin/env python3
"""Download official DARPA/ICGC burn-perimeter SHP zips when available.

Pattern discovered on the agricultura.gencat SHP page:
  http://www.gencat.cat/agricultura/sig/bases/incendis{YY}.zip

Example: incendis24.zip (updated ~Sep 2025). incendis25.zip may 404 until published.

Also supports a manual drop path: data/official/*.zip or unpacked SHP folders.
Large SHP/ZIP files are gitignored; convert useful scars to GeoJSON under scars/.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_DIR = ROOT / "data" / "official"
SCARS_DIR = ROOT / "scars"
BASE_URL = "http://www.gencat.cat/agricultura/sig/bases/incendis{yy}.zip"
CATALOG_PAGE = (
    "https://agricultura.gencat.cat/ca/serveis/cartografia-sig/"
    "bases-cartografiques/boscos/incendis-forestals/incendis-forestals-format-shp/"
)


def head_ok(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "cat-cicatrius/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception:
        # Some servers dislike HEAD — try a Range GET
        req2 = urllib.request.Request(
            url,
            headers={"User-Agent": "cat-cicatrius/1.0", "Range": "bytes=0-0"},
        )
        try:
            with urllib.request.urlopen(req2, timeout=30) as resp:
                return resp.status in (200, 206)
        except Exception:
            return False


def download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"GET {url} → {dest}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "cat-cicatrius/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        print(f"  skip ({e.code})", flush=True)
        return False
    dest.write_bytes(data)
    print(f"  saved {len(data)} bytes", flush=True)
    return True


def unzip_to(zip_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(out_dir)
    return out_dir


def try_to_geojson(shp_dir: Path, year: int) -> Path | None:
    """Best-effort SHP → GeoJSON via geopandas (optional dep)."""
    try:
        import geopandas as gpd
    except ImportError:
        print("geopandas not installed — leaving SHP on disk only", flush=True)
        return None

    shps = list(shp_dir.rglob("*.shp"))
    if not shps:
        print(f"No .shp under {shp_dir}", flush=True)
        return None
    # Prefer largest / first
    shp = max(shps, key=lambda p: p.stat().st_size)
    print(f"Reading {shp}", flush=True)
    gdf = gpd.read_file(shp)
    if gdf.crs is None:
        # Official layers are typically ETRS89 / UTM 31N
        gdf = gdf.set_crs("EPSG:25831", allow_override=True)
    gdf = gdf.to_crs("EPSG:4326")
    out = SCARS_DIR / f"official_{year}.geojson"
    SCARS_DIR.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out, driver="GeoJSON")
    print(f"Wrote {len(gdf)} features → {out}", flush=True)
    return out


def probe_years(years: list[int]) -> list[dict]:
    results = []
    for y in years:
        yy = f"{y % 100:02d}"
        url = BASE_URL.format(yy=yy)
        ok = head_ok(url)
        results.append({"year": y, "url": url, "available": ok})
        print(f"  {y}: {'OK' if ok else 'missing'}  {url}", flush=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--years",
        type=int,
        nargs="*",
        default=None,
        help="Years to fetch (default: last 3 calendar years from today)",
    )
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--to-geojson", action="store_true", default=True)
    ap.add_argument("--no-geojson", action="store_true")
    ap.add_argument(
        "--manual-dir",
        type=Path,
        default=OFFICIAL_DIR,
        help="Also scan this dir for already-dropped zips/SHP",
    )
    args = ap.parse_args()

    from datetime import date

    if args.years:
        years = sorted(set(args.years))
    else:
        cy = date.today().year
        years = [cy - 2, cy - 1, cy]

    print(f"Catalog page: {CATALOG_PAGE}")
    print("Probing official SHP zips…")
    probe = probe_years(years)
    meta_path = OFFICIAL_DIR / "probe.json"
    OFFICIAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"base_url_pattern": BASE_URL, "results": probe}, f, indent=2)
        f.write("\n")

    if args.probe_only:
        return 0

    do_geo = args.to_geojson and not args.no_geojson
    fetched = 0
    for item in probe:
        if not item["available"]:
            continue
        y = item["year"]
        dest = OFFICIAL_DIR / f"incendis{y % 100:02d}.zip"
        if not dest.exists():
            if not download(item["url"], dest):
                continue
        else:
            print(f"Already have {dest}", flush=True)
        extract_dir = OFFICIAL_DIR / f"incendis{y % 100:02d}"
        unzip_to(dest, extract_dir)
        if do_geo:
            try_to_geojson(extract_dir, y)
        fetched += 1

    # Manual drop path
    for zp in sorted(args.manual_dir.glob("*.zip")):
        if zp.name.startswith("incendis") and zp.stat().st_size > 0:
            print(f"Manual zip present: {zp}", flush=True)

    if fetched == 0:
        print(
            "No remote SHP downloaded. Drop zips manually under data/official/ "
            f"(see {CATALOG_PAGE})",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
