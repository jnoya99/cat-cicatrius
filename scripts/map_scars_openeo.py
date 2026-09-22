#!/usr/bin/env python3
"""Skeleton: CDSE openEO Sentinel-2 dNBR scars per fire AOI.

Requires secrets (do NOT invent credentials):
  CDSE_USER, CDSE_PASSWORD  — https://dataspace.copernicus.eu/

Steps (when credentials present):
  1. Load fire AOIs (events + optional EFFIS/official bbox buffers)
  2. Connect to CDSE openEO
  3. For each AOI: pre-fire & post-fire median S2 L2A composites
  4. NBR = (B08 - B12) / (B08 + B12)  [or B8A & B12]
  5. dNBR = NBR_pre - NBR_post; threshold ~0.12
  6. Vectorize burned mask → scars/{fire_id}_dnbr.geojson

If credentials are missing: exit 0 with a clear skip message so the
seasonal workflow still succeeds on the Gencat+grid path.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCARS_DIR = ROOT / "scars"

# Default dNBR threshold (moderate burn onset; tune later)
DNBR_THRESHOLD = 0.12
CDSE_URL = "https://openeo.dataspace.copernicus.eu"


def credentials_present() -> bool:
    return bool(os.environ.get("CDSE_USER") and os.environ.get("CDSE_PASSWORD"))


def skip(msg: str) -> int:
    print(f"[map_scars_openeo] SKIP: {msg}", flush=True)
    return 0


def run_openeo_skeleton(aoi_geojson: Path | None, dry_run: bool) -> int:
    """Clear TODO pipeline — imports guarded so missing openeo doesn't break CI."""
    try:
        import openeo  # type: ignore
    except ImportError:
        return skip("package 'openeo' not installed (optional; see requirements.txt)")

    user = os.environ["CDSE_USER"]
    password = os.environ["CDSE_PASSWORD"]

    print(f"Connecting to {CDSE_URL} as {user!r} …", flush=True)
    # TODO: use basic auth or OIDC device flow as preferred by CDSE at runtime
    connection = openeo.connect(CDSE_URL)
    connection.authenticate_basic(user, password)

    # --- TODO 1: load AOIs -------------------------------------------------
    # Prefer scars/effis_*.geojson or official_*.geojson bbox buffers (~2 km).
    # Fall back to municipality centroids from events/gencat_events_all.csv
    # only for medium/large fires (haforestal > ~50 ha) — skip tiny buffers.
    if aoi_geojson is None or not aoi_geojson.exists():
        return skip(
            "no AOI GeoJSON provided; pass --aoi scars/effis_YYYY.geojson "
            "or drop official SHP-derived scars first"
        )

    print(f"AOI input: {aoi_geojson}", flush=True)
    if dry_run:
        print("dry-run: would process AOIs with S2 L2A pre/post dNBR", flush=True)
        return 0

    # --- TODO 2: per-feature job ------------------------------------------
    # For each fire polygon / bbox:
    #   pre_start, pre_end   = window ending ~1–4 weeks before fire date
    #   post_start, post_end = window starting ~1–3 weeks after fire date
    #   cube = connection.load_collection(
    #       "SENTINEL2_L2A",
    #       spatial_extent=...,
    #       temporal_extent=[pre_start, post_end],
    #       bands=["B08", "B8A", "B12", "SCL"],
    #   )
    #   Filter clouds via SCL; median composite for pre and post.
    #   nbr = (nir - swir) / (nir + swir)  with nir=B8A (or B08), swir=B12
    #   dnbr = nbr_pre - nbr_post
    #   mask = dnbr > DNBR_THRESHOLD  (~0.12)
    #   vectorize / download GeoTIFF → polygonize → scars/{fire_id}_dnbr.geojson

    print(
        "TODO: implement openEO process graph (median S2 L2A, NBR/dNBR, "
        f"threshold={DNBR_THRESHOLD}). Structure is ready.",
        flush=True,
    )
    SCARS_DIR.mkdir(parents=True, exist_ok=True)
    # Placeholder marker so publish knows the step was attempted
    marker = SCARS_DIR / "_openeo_pending.txt"
    marker.write_text(
        "openEO credentials present but process graph not yet implemented.\n"
        f"threshold={DNBR_THRESHOLD} collection=SENTINEL2_L2A bands=B8A,B12\n",
        encoding="utf-8",
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aoi", type=Path, default=None, help="GeoJSON AOIs / scars input")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-skip", action="store_true", help="Always skip (CI smoke)")
    args = ap.parse_args()

    if args.force_skip:
        return skip("--force-skip")

    if not credentials_present():
        return skip(
            "CDSE_USER / CDSE_PASSWORD not set. "
            "Add them as GitHub Actions secrets to enable Sentinel dNBR. "
            "Pipeline continues with official/EFFIS geometry only."
        )

    try:
        return run_openeo_skeleton(args.aoi, args.dry_run)
    except Exception as e:
        # Soft-fail: seasonal publish should still run
        print(f"[map_scars_openeo] error (non-fatal): {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
