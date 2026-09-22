"""Helpers for the Bolets Explorador app grid (WGS84 0.001° cells).

Constants live in grid/app_grid.json — do NOT invent UTM 100 m ids.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parents[1]
GRID_PATH = ROOT / "grid" / "app_grid.json"


def load_grid(path: Path | None = None) -> dict:
    p = path or GRID_PATH
    with open(p, encoding="utf-8") as f:
        return json.load(f)


_G = None


def _grid() -> dict:
    global _G
    if _G is None:
        _G = load_grid()
    return _G


def step() -> float:
    return float(_grid()["step_deg"])


def min_lon() -> float:
    return float(_grid()["min_lon"])


def min_lat() -> float:
    return float(_grid()["min_lat"])


def nlon() -> int:
    return int(_grid()["nlon"])


def round_coord(x: float, step_deg: float | None = None) -> float:
    """Round lon/lat to the app grid step (default 0.001°)."""
    s = step() if step_deg is None else step_deg
    return round(round(x / s) * s, 6)


def lonlat_to_ij(lon: float, lat: float) -> Tuple[int, int]:
    """ii, jj as used by the explorer binary."""
    s = step()
    ii = int(round((lon - min_lon()) / s))
    jj = int(round((lat - min_lat()) / s))
    return ii, jj


def ij_to_dense(ii: int, jj: int) -> int:
    return jj * nlon() + ii


def lonlat_to_dense(lon: float, lat: float) -> int:
    ii, jj = lonlat_to_ij(lon, lat)
    return ij_to_dense(ii, jj)


def dense_to_ij(dense_index: int) -> Tuple[int, int]:
    nl = nlon()
    jj = dense_index // nl
    ii = dense_index % nl
    return ii, jj


def ij_to_lonlat(ii: int, jj: int) -> Tuple[float, float]:
    s = step()
    lon = round_coord(min_lon() + ii * s)
    lat = round_coord(min_lat() + jj * s)
    return lon, lat


def dense_to_lonlat(dense_index: int) -> Tuple[float, float]:
    return ij_to_lonlat(*dense_to_ij(dense_index))


def cell_bbox(lon: float, lat: float) -> Tuple[float, float, float, float]:
    """Half-open-ish cell extent around a rounded centre (for sampling)."""
    s = step()
    lon_r = round_coord(lon)
    lat_r = round_coord(lat)
    half = s / 2.0
    return lon_r - half, lat_r - half, lon_r + half, lat_r + half


if __name__ == "__main__":
    g = load_grid()
    print("grid:", g)
    lon, lat = 2.173, 41.385
    ii, jj = lonlat_to_ij(lon, lat)
    d = lonlat_to_dense(lon, lat)
    print(f"sample ({lon},{lat}) -> ii={ii} jj={jj} dense={d} back={dense_to_lonlat(d)}")
