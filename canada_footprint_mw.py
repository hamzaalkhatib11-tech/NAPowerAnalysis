"""
canada_footprint_mw.py

Extends the North America harmonization pipeline with real MW estimates for
Canadian data centers, using Microsoft Global Building Footprint tiles
(the 23 zoom-9 quadkey tiles identified as containing the 63 Canadian points
in harmonized_existing_dc_with_ba.csv).

For each Canadian point:
  1. Load its matching tile (line-delimited GeoJSON of building polygons).
  2. Find the nearest building footprint polygon to the point, within a
     search radius (SEARCH_RADIUS_M).
  3. Compute that polygon's geodesic area (accounts for latitude, unlike a
     naive planar degree-based area).
  4. Convert area -> sqft -> MW using the same W_PER_SQFT assumption used
     for the U.S. IM3-derived estimates, so the two are directly comparable.

Output: canada_dc_with_mw.csv, plus a refreshed canada_dc_by_province.csv
"""

import gzip
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Geod
from shapely.geometry import shape as shapely_shape, Point
from sklearn.neighbors import BallTree

DATA_DIR = Path("/mnt/user-data/uploads")
OUT_DIR = Path("/home/claude/output")
POINTS_CSV = OUT_DIR / "harmonized_existing_dc_with_ba.csv"
TILES_NEEDED_CSV = OUT_DIR / "canada_building_footprint_tiles_needed.csv"

W_PER_SQFT = 150.0            # same assumption as the US sqft_derived method
SEARCH_RADIUS_M = 200.0       # max distance from point to nearest building
SQM_TO_SQFT = 10.7639
EARTH_RADIUS_M = 6_371_000.0

geod = Geod(ellps="WGS84")


def geodesic_area_sqm(polygon) -> float:
    lon, lat = polygon.exterior.coords.xy
    area, _ = geod.polygon_area_perimeter(lon, lat)
    return abs(area)


def load_tile(path: Path):
    """Load a gzipped line-delimited GeoJSON tile. Returns (BallTree over
    centroids, list of polygons) -- area is computed lazily, only for
    whichever polygon actually ends up being the nearest match to a point."""
    centroids = []
    polygons = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                feat = json.loads(line)
                poly = shapely_shape(feat["geometry"])
                if poly.is_empty:
                    continue
            except Exception:
                continue
            c = poly.centroid
            centroids.append((c.y, c.x))  # lat, lon for haversine
            polygons.append(poly)

    if not centroids:
        return None, []

    coords_rad = np.radians(np.array(centroids))
    tree = BallTree(coords_rad, metric="haversine")
    return tree, polygons


def nearest_building_area(pt_lon: float, pt_lat: float, tree, polygons: list, radius_m: float):
    if tree is None:
        return None, None
    query = np.radians([[pt_lat, pt_lon]])
    dist_rad, idx = tree.query(query, k=1)
    dist_m = dist_rad[0][0] * EARTH_RADIUS_M
    if dist_m > radius_m:
        return None, dist_m
    poly = polygons[idx[0][0]]
    return geodesic_area_sqm(poly), dist_m



def main():
    points = pd.read_csv(POINTS_CSV)
    ca_points = points[points["country"] == "Canada"].copy().reset_index(drop=True)

    tiles_needed = pd.read_csv(TILES_NEEDED_CSV, dtype={"QuadKey": str})
    tiles_needed["part_num"] = tiles_needed["Url"].str.extract(r"part-(\d+)-")

    # map each quadkey -> local file path. Filenames only carry a Spark
    # "part-NNNNN" number, which is NOT globally unique across quadkeys
    # (two different quadkey folders can both produce a part-00179, and the
    # browser disambiguates re-downloads with a "__1__" suffix that carries
    # no quadkey information). So: for any part number claimed by more than
    # one needed quadkey, resolve by content -- read the first coordinate of
    # each candidate file and check which needed quadkey's tile it actually
    # falls inside, rather than trusting the filename alone.
    def first_coord_quadkey(path: Path, zoom: int = 9) -> str:
        import math
        with gzip.open(path, "rt", encoding="utf-8") as f:
            line = f.readline()
        feat = json.loads(line)
        lon, lat = feat["geometry"]["coordinates"][0][0]
        lat_rad = math.radians(lat)
        x = (lon + 180.0) / 360.0
        y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0
        n = 2 ** zoom
        tile_x = int(x * n)
        tile_y = int(y * n)
        quadkey = ""
        for i in range(zoom, 0, -1):
            digit = 0
            mask = 1 << (i - 1)
            if tile_x & mask:
                digit += 1
            if tile_y & mask:
                digit += 2
            quadkey += str(digit)
        return quadkey

    part_num_to_candidates = {}
    local_files = {f.name: f for f in DATA_DIR.glob("part-*.gz")}
    for fname, fpath in local_files.items():
        m = re.search(r"part-(\d+)-", fname)
        if m:
            part_num_to_candidates.setdefault(m.group(1), []).append(fpath)

    quadkey_to_path = {}
    for _, row in tiles_needed.iterrows():
        candidates = part_num_to_candidates.get(row["part_num"], [])
        if len(candidates) == 1:
            quadkey_to_path[row["QuadKey"]] = candidates[0]
        elif len(candidates) > 1:
            for cand in candidates:
                if first_coord_quadkey(cand) == row["QuadKey"]:
                    quadkey_to_path[row["QuadKey"]] = cand
                    break

    print(f"{len(quadkey_to_path)} / {len(tiles_needed)} needed tiles resolved to local files")

    # recompute each point's quadkey the same way as before (zoom 9)
    import math

    def lat_lon_to_quadkey(lat, lon, zoom):
        lat_rad = math.radians(lat)
        x = (lon + 180.0) / 360.0
        y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0
        n = 2 ** zoom
        tile_x = int(x * n)
        tile_y = int(y * n)
        quadkey = ""
        for i in range(zoom, 0, -1):
            digit = 0
            mask = 1 << (i - 1)
            if tile_x & mask:
                digit += 1
            if tile_y & mask:
                digit += 2
            quadkey += str(digit)
        return quadkey

    ca_points["quadkey_z9"] = ca_points.apply(
        lambda r: lat_lon_to_quadkey(r["lat"], r["lon"], 9), axis=1
    )

    tile_cache = {}
    areas, dists, methods = [], [], []
    for _, row in ca_points.iterrows():
        qk = row["quadkey_z9"]
        path = quadkey_to_path.get(qk)
        if path is None:
            areas.append(None)
            dists.append(None)
            methods.append("no_tile_available")
            continue
        if qk not in tile_cache:
            print(f"Loading tile {qk} ({path.name}) ...")
            tile_cache[qk] = load_tile(path)
        tree, polygons = tile_cache[qk]
        area, dist = nearest_building_area(row["lon"], row["lat"], tree, polygons, SEARCH_RADIUS_M)
        areas.append(area)
        dists.append(dist)
        methods.append("footprint_matched" if area is not None else "no_building_within_radius")

    ca_points["nearest_building_area_sqm"] = areas
    ca_points["nearest_building_dist_m"] = dists
    ca_points["footprint_match_method"] = methods
    ca_points["sqft"] = ca_points["nearest_building_area_sqm"] * SQM_TO_SQFT
    ca_points["mw_estimate"] = np.where(
        ca_points["sqft"].notna(),
        (ca_points["sqft"] * W_PER_SQFT) / 1_000_000.0,
        np.nan,
    )
    ca_points["mw_method"] = np.where(
        ca_points["mw_estimate"].notna(), "footprint_derived", "unknown"
    )

    out_path = OUT_DIR / "canada_dc_with_mw.csv"
    ca_points.to_csv(out_path, index=False)
    print(f"Wrote {out_path}")

    matched = ca_points["mw_method"].eq("footprint_derived").sum()
    print(f"\n{matched} / {len(ca_points)} Canadian points got a footprint-derived MW estimate")

    summary = (
        ca_points.groupby("joined_province", dropna=True)
        .agg(
            facility_count=("mw_estimate", "size"),
            known_mw_total=("mw_estimate", "sum"),
            unknown_mw_count=("mw_method", lambda s: (s == "unknown").sum()),
        )
        .reset_index()
        .sort_values("known_mw_total", ascending=False)
    )
    summary_out = OUT_DIR / "canada_dc_by_province.csv"
    summary.to_csv(summary_out, index=False)
    print(f"Wrote {summary_out}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
