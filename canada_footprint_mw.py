"""
canada_footprint_mw.py

Real MW estimates for Canadian data centres from Microsoft Global Building
Footprint tiles (PROJECT_HANDOFF.md §4). For each Canadian point:
  1. Compute its zoom-9 quadkey and find the matching tile.
  2. Find the nearest building centroid within FOOTPRINT_SEARCH_RADIUS_M
     (BallTree; area is computed only for the winning polygon).
  3. Geodesic area -> sqft -> MW with the same W_PER_SQFT as the U.S. layer.

Reads output/harmonized_existing_dc_with_ba.csv (from harmonize_datacenters.py).
Writes output/canada_building_footprint_tiles_needed.csv,
       output/canada_dc_with_mw.csv and a refreshed output/canada_dc_by_province.csv.
--summary-only rebuilds just canada_dc_by_province.csv from an existing
canada_dc_with_mw.csv (no tiles read).

Tiles live in data/ as part-*.csv.gz. Their filenames only carry a Spark
part number, which is NOT unique across quadkeys (part-00179 is both Calgary
and Montreal), so a tile is matched to its quadkey by reading its content.
"""

import gzip
import json
import re
import sys

import numpy as np
import pandas as pd
from pyproj import Geod
from shapely.geometry import shape as shapely_shape
from sklearn.neighbors import BallTree

from config import (
    DATA_DIR, EARTH_RADIUS_M, FOOTPRINT_LINKS_CSV, FOOTPRINT_SEARCH_RADIUS_M, FOOTPRINT_ZOOM,
    GEOCODE_FIXES, OUT_DIR, SQM_TO_SQFT, W_PER_SQFT, lat_lon_to_quadkey,
)

POINTS_CSV = OUT_DIR / "harmonized_existing_dc_with_ba.csv"
TILES_NEEDED_CSV = OUT_DIR / "canada_building_footprint_tiles_needed.csv"

geod = Geod(ellps="WGS84")


def geodesic_area_sqm(polygon) -> float:
    lon, lat = polygon.exterior.coords.xy
    area, _ = geod.polygon_area_perimeter(lon, lat)
    return abs(area)


def load_tile(path):
    """Gzipped line-delimited GeoJSON -> (BallTree over centroids, polygons)."""
    centroids, polygons = [], []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                poly = shapely_shape(json.loads(line)["geometry"])
            except Exception:
                continue
            if poly.is_empty:
                continue
            c = poly.centroid
            centroids.append((c.y, c.x))
            polygons.append(poly)
    if not centroids:
        return None, []
    return BallTree(np.radians(np.array(centroids)), metric="haversine"), polygons


def nearest_building_area(lat, lon, tree, polygons, radius_m):
    if tree is None:
        return None, None
    dist_rad, idx = tree.query(np.radians([[lat, lon]]), k=1)
    dist_m = dist_rad[0][0] * EARTH_RADIUS_M
    if dist_m > radius_m:
        return None, dist_m
    return geodesic_area_sqm(polygons[idx[0][0]]), dist_m


def first_coord_quadkey(path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        feat = json.loads(f.readline())
    lon, lat = feat["geometry"]["coordinates"][0][0][:2]
    return lat_lon_to_quadkey(lat, lon, FOOTPRINT_ZOOM)


def tiles_needed(quadkeys) -> pd.DataFrame:
    # dtype=str: pandas would otherwise parse QuadKey as int and drop leading zeros
    links = pd.read_csv(FOOTPRINT_LINKS_CSV, dtype={"QuadKey": str})
    links = links[(links["Location"] == "Canada") & links["QuadKey"].isin(set(quadkeys))]
    return links[["QuadKey", "Url", "Size"]].sort_values("QuadKey").reset_index(drop=True)


def resolve_tile_paths(needed: pd.DataFrame) -> dict:
    """quadkey -> local tile path, resolving duplicate part numbers by content."""
    candidates = {}
    for path in DATA_DIR.glob("part-*.gz"):
        m = re.search(r"part-(\d+)-", path.name)
        if m:
            candidates.setdefault(m.group(1), []).append(path)

    resolved = {}
    for _, row in needed.iterrows():
        part = re.search(r"part-(\d+)-", row["Url"]).group(1)
        for path in candidates.get(part, []):
            if len(candidates[part]) == 1 or first_coord_quadkey(path) == row["QuadKey"]:
                resolved[row["QuadKey"]] = path
                break
    return resolved


def main():
    points = pd.read_csv(POINTS_CSV)
    ca = points[points["country"] == "Canada"].copy().reset_index(drop=True)
    ca["quadkey_z9"] = [lat_lon_to_quadkey(lat, lon, FOOTPRINT_ZOOM) for lat, lon in zip(ca["lat"], ca["lon"])]

    needed = tiles_needed(ca["quadkey_z9"])
    needed.to_csv(TILES_NEEDED_CSV, index=False)
    quadkey_to_path = resolve_tile_paths(needed)
    print(f"{len(quadkey_to_path)} / {len(needed)} needed tiles resolved to local files")
    missing = sorted(set(needed["QuadKey"]) - set(quadkey_to_path))
    for qk in missing:
        url = needed.loc[needed["QuadKey"] == qk, "Url"].iloc[0]
        print(f"  MISSING tile {qk}: download {url}")

    tile_cache = {}
    areas, dists, methods = [], [], []
    for _, row in ca.iterrows():
        qk = row["quadkey_z9"]
        path = quadkey_to_path.get(qk)
        if path is None:
            areas.append(None); dists.append(None); methods.append("no_tile_available")
            continue
        if qk not in tile_cache:
            print(f"  loading tile {qk} ({path.name}) ...")
            tile_cache[qk] = load_tile(path)
        area, dist = nearest_building_area(row["lat"], row["lon"], *tile_cache[qk], FOOTPRINT_SEARCH_RADIUS_M)
        areas.append(area); dists.append(dist)
        methods.append("footprint_matched" if area is not None else "no_building_within_radius")

    ca["nearest_building_area_sqm"] = areas
    ca["nearest_building_dist_m"] = dists
    ca["footprint_match_method"] = methods
    ca["sqft"] = pd.to_numeric(ca["nearest_building_area_sqm"]) * SQM_TO_SQFT
    ca["mw_estimate"] = ca["sqft"] * W_PER_SQFT / 1_000_000.0
    ca["mw_method"] = np.where(ca["mw_estimate"].notna(), "footprint_derived", "unknown")

    # sanity-check the manual geocode fixes landed on the expected building
    for fix in GEOCODE_FIXES:
        hit = ca[ca["name"] == fix["name"]]
        if hit.empty:
            print(f"  WARNING: {fix['name']} not among Canadian points")
        elif "expected_footprint_mw" in fix:
            mw = hit["mw_estimate"].iloc[0]
            ok = pd.notna(mw) and abs(mw - fix["expected_footprint_mw"]) < 0.05
            print(f"  {fix['name']}: {mw:.2f} MW (expected ~{fix['expected_footprint_mw']}) "
                  f"{'OK' if ok else 'MISMATCH'}")

    ca.to_csv(OUT_DIR / "canada_dc_with_mw.csv", index=False)
    matched = (ca["mw_method"] == "footprint_derived").sum()
    print(f"{matched} / {len(ca)} Canadian points got a footprint-derived MW estimate")
    write_province_summary(ca)


def write_province_summary(ca: pd.DataFrame) -> None:
    """canada_dc_by_province.csv from the footprint MW, replacing harmonize's MW-less placeholder."""
    summary = (
        ca.groupby("joined_province", dropna=True)
        .agg(facility_count=("mw_estimate", "size"),
             known_mw_total=("mw_estimate", "sum"),
             unknown_mw_count=("mw_method", lambda s: (s == "unknown").sum()))
        .reset_index().sort_values("known_mw_total", ascending=False)
    )
    summary.to_csv(OUT_DIR / "canada_dc_by_province.csv", index=False)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    if "--summary-only" in sys.argv:
        # run_pipeline.py --skip-footprint: reuse the matched points, but harmonize has
        # just rewritten the province summary without MW, so rebuild it
        write_province_summary(pd.read_csv(OUT_DIR / "canada_dc_with_mw.csv"))
    else:
        main()
