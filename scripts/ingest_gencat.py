#!/usr/bin/env python3
"""Download Gencat Socrata fire tables → events CSV.

Sources (no auth):
  - Current year: https://analisi.transparenciacatalunya.cat/resource/9r29-e8ha.json
  - Historic 2011–2024: https://analisi.transparenciacatalunya.cat/resource/bks7-dkfd.json

These tables are tabular (municipality + ha), not perimeters. They feed the
event calendar; geometry comes from official SHP / EFFIS / Sentinel.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENTS_DIR = ROOT / "events"

CURRENT_URL = "https://analisi.transparenciacatalunya.cat/resource/9r29-e8ha.json"
HISTORIC_URL = "https://analisi.transparenciacatalunya.cat/resource/bks7-dkfd.json"

FIELDS = [
    "data_incendi",
    "codi_comarca",
    "comarca",
    "codi_municipi",
    "termemunic",
    "haarbrades",
    "hanoarbrad",
    "hanoforest",
    "haforestal",
    "source_table",
]


def fetch_all(url: str, page_size: int = 50000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        qs = urllib.parse.urlencode({"$limit": page_size, "$offset": offset})
        full = f"{url}?{qs}"
        print(f"GET {full}", flush=True)
        req = urllib.request.Request(full, headers={"User-Agent": "cat-cicatrius/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"HTTP error {e.code} for {full}", file=sys.stderr)
            raise
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def normalize(rows: list[dict], source_table: str) -> list[dict]:
    out = []
    for r in rows:
        item = {k: r.get(k, "") for k in FIELDS if k != "source_table"}
        item["source_table"] = source_table
        # coerce ha fields to float strings for CSV stability
        for ha in ("haarbrades", "hanoarbrad", "hanoforest", "haforestal"):
            v = item.get(ha, "")
            try:
                item[ha] = "" if v in (None, "") else f"{float(v):.6f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                item[ha] = str(v) if v is not None else ""
        out.append(item)
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows → {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=EVENTS_DIR)
    ap.add_argument("--skip-historic", action="store_true")
    ap.add_argument("--skip-current", action="store_true")
    args = ap.parse_args()

    all_rows: list[dict] = []
    if not args.skip_historic:
        hist = normalize(fetch_all(HISTORIC_URL), "bks7-dkfd")
        write_csv(args.out_dir / "gencat_historic_2011_2024.csv", hist)
        all_rows.extend(hist)
    if not args.skip_current:
        cur = normalize(fetch_all(CURRENT_URL), "9r29-e8ha")
        write_csv(args.out_dir / "gencat_current_year.csv", cur)
        all_rows.extend(cur)

    # Combined event list (sorted by date)
    all_rows.sort(key=lambda r: r.get("data_incendi") or "")
    write_csv(args.out_dir / "gencat_events_all.csv", all_rows)

    # Lightweight JSON summary for the workflow / manifest
    years: dict[str, int] = {}
    for r in all_rows:
        d = (r.get("data_incendi") or "")[:4]
        if d.isdigit():
            years[d] = years.get(d, 0) + 1
    summary = {
        "n_events": len(all_rows),
        "years": dict(sorted(years.items())),
        "sources": {
            "current": CURRENT_URL,
            "historic": HISTORIC_URL,
        },
    }
    summary_path = args.out_dir / "gencat_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Summary → {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
