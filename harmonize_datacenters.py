"""
harmonize_datacenters.py

Builds one clean "existing facilities" point layer for the GGR442 project out of:
  - datacenters.geojson / datacenters.csv   (global directory, name/company/city/address, NO MW)
  - im3_open_source_data_center_atlas.csv   (US points with sqft, NO MW)
  - databricks.csv                          (US, state-level, HAS TOT_POWER_MW + build stage)
  - high_growth_50_market_gravity.geojson   (2035 "high growth" scenario surface, projected CRS)

Outputs (written next to this script, in ./output/):
  1. harmonized_existing_dc.csv       - deduped point layer with MW estimates + confidence tags
  2. harmonized_existing_dc.geojson   - same, as GeoJSON points (WGS84)
  3. qa_state_mw_totals.csv           - your point-level MW sums vs. databricks state totals
  4. growth_2035_wgs84.geojson        - the gravity/growth layer reprojected to WGS84

ASSUMPTIONS TO REVIEW WITH YOUR TEAM (flagged again inline where used):
  - MW-from-sqft benchmark: 150 W per sq ft of total facility space (a commonly cited
    industry mid-range figure for enterprise/colo data centers). This is a placeholder —
    replace W_PER_SQFT below with whatever figure your team decides to defend in the
    methodology writeup, or better, use per-tier benchmarks if your mentor gives you one.
  - Dedup radius: 400 meters. Two points within this radius are treated as the same
    physical facility. Tune DEDUP_RADIUS_M if you find over- or under-merging.
  - Only US + Canada records are kept (project scope is North America).
"""

import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree
from pyproj import Transformer

# ----------------------------------------------------------------------------
# CONFIG - adjust paths/constants here
# ----------------------------------------------------------------------------
DATA_DIR = Path("/mnt/user-data/uploads")
OUT_DIR = Path("/home/claude/output")
OUT_DIR.mkdir(parents=True, exist_ok=True)

DATACENTERS_GEOJSON = DATA_DIR / "datacenters.geojson"
DATACENTERS_CSV = DATA_DIR / "datacenters.csv"
IM3_CSV = DATA_DIR / "im3_open_source_data_center_atlas.csv"
DATABRICKS_CSV = DATA_DIR / "databricks.csv"
GROWTH_GEOJSON = DATA_DIR / "high_growth_50_market_gravity.geojson"

W_PER_SQFT = 150.0          # <-- REVIEW: watts per sq ft used to derive MW from building sqft
DEDUP_RADIUS_M = 150.0      # <-- REVIEW: points closer than this are merged as one facility
NORTH_AMERICA_COUNTRIES = {"United States", "Canada"}

EARTH_RADIUS_M = 6_371_000.0


# ----------------------------------------------------------------------------
# 1. Load datacenters.geojson -> point records (already has coordinates)
# ----------------------------------------------------------------------------
def load_datacenters_geojson(path: Path) -> pd.DataFrame:
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)

    rows = []
    for feat in gj["features"]:
        props = feat["properties"]
        lon, lat = feat["geometry"]["coordinates"]
        rows.append({
            "source": "datacenters_geojson",
            "name": props.get("name"),
            "company": props.get("company"),
            "city": props.get("city"),
            "state": props.get("state"),
            "country": props.get("country"),
            "address": props.get("address"),
            "lat": lat,
            "lon": lon,
            "sqft": np.nan,
            "status": np.nan,       # not provided by this source
        })
    df = pd.DataFrame(rows)
    df = df[df["country"].isin(NORTH_AMERICA_COUNTRIES)].reset_index(drop=True)
    return df


# ----------------------------------------------------------------------------
# 2. Load IM3 atlas -> point records with sqft
# ----------------------------------------------------------------------------
def load_im3_atlas(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    out = pd.DataFrame({
        "source": "im3_atlas",
        "name": df.get("name"),
        "company": df.get("operator"),
        "city": np.nan,
        "state": df.get("state"),
        "country": "United States",   # IM3 atlas is US-only per README
        "address": np.nan,
        "lat": df.get("lat"),
        "lon": df.get("lon"),
        "sqft": df.get("sqft"),
        "status": "existing",
    })
    return out


# ----------------------------------------------------------------------------
# 3. Estimate MW where possible, tag the method used, leave rest as unknown
# ----------------------------------------------------------------------------
def estimate_mw(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["mw_estimate"] = np.nan
    df["mw_method"] = "unknown"

    has_sqft = df["sqft"].notna() & (df["sqft"] > 0)
    df.loc[has_sqft, "mw_estimate"] = (df.loc[has_sqft, "sqft"] * W_PER_SQFT) / 1_000_000.0
    df.loc[has_sqft, "mw_method"] = "sqft_derived"

    return df


# ----------------------------------------------------------------------------
# 4. Spatial dedup: merge points within DEDUP_RADIUS_M of each other
#    Uses a haversine BallTree; keeps the record with more complete info
#    (prefers a row that already has an mw_estimate).
# ----------------------------------------------------------------------------
def dedupe_points(df: pd.DataFrame, radius_m: float = DEDUP_RADIUS_M) -> pd.DataFrame:
    df = df.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    if len(df) == 0:
        return df

    coords_rad = np.radians(df[["lat", "lon"]].to_numpy())
    tree = BallTree(coords_rad, metric="haversine")
    radius_rad = radius_m / EARTH_RADIUS_M

    neighbors = tree.query_radius(coords_rad, r=radius_rad)

    visited = np.zeros(len(df), dtype=bool)
    keep_rows = []

    # Prefer rows with a real mw_estimate when merging duplicates
    has_mw = df["mw_method"] != "unknown"

    for i in range(len(df)):
        if visited[i]:
            continue
        group = neighbors[i]
        visited[group] = True
        if len(group) == 1:
            keep_rows.append(i)
            continue
        # pick the best row in the group: has_mw first, then non-null company/name
        group_df = df.iloc[group]
        group_has_mw = has_mw.iloc[group]
        if group_has_mw.any():
            best_idx = group[np.argmax(group_has_mw.to_numpy())]
        else:
            best_idx = group[0]
        keep_rows.append(best_idx)

    deduped = df.iloc[sorted(set(keep_rows))].reset_index(drop=True)
    return deduped


# ----------------------------------------------------------------------------
# 5. QA: compare point-level MW sums per state against databricks state totals
# ----------------------------------------------------------------------------
def qa_against_databricks(existing_df: pd.DataFrame, databricks_path: Path) -> pd.DataFrame:
    db = pd.read_csv(databricks_path)
    db_state_totals = (
        db.groupby("STATE_NAME", dropna=True)["TOT_POWER_MW"]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={"TOT_POWER_MW": "databricks_mw_total", "STATE_NAME": "state"})
    )

    # normalize state naming: im3 uses full state names already; datacenters_geojson
    # source often has state abbreviations or blanks for US rows - point-level state
    # coverage will be incomplete for that source, which is itself useful QA info.
    point_state_totals = (
        existing_df.groupby("state", dropna=True)["mw_estimate"]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={"mw_estimate": "point_layer_mw_total"})
    )

    qa = db_state_totals.merge(point_state_totals, on="state", how="left")
    qa["point_layer_mw_total"] = qa["point_layer_mw_total"].fillna(0.0)
    qa["gap_mw"] = qa["databricks_mw_total"] - qa["point_layer_mw_total"]
    qa["pct_of_known_total_captured"] = np.where(
        qa["databricks_mw_total"] > 0,
        100.0 * qa["point_layer_mw_total"] / qa["databricks_mw_total"],
        np.nan,
    )
    return qa.sort_values("gap_mw", ascending=False)


# ----------------------------------------------------------------------------
# 6. Reproject the 2035 growth-gravity layer from its native projected CRS to WGS84
# ----------------------------------------------------------------------------
def reproject_growth_layer(path: Path, out_path: Path) -> None:
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)

    src_crs = gj.get("crs", {}).get("properties", {}).get("name", "ESRI:102003")
    transformer = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)

    def reproject_ring(ring):
        return [list(transformer.transform(x, y)) for x, y in ring]

    def reproject_geometry(geom):
        if geom["type"] == "Polygon":
            geom["coordinates"] = [reproject_ring(ring) for ring in geom["coordinates"]]
        elif geom["type"] == "MultiPolygon":
            geom["coordinates"] = [
                [reproject_ring(ring) for ring in poly] for poly in geom["coordinates"]
            ]
        elif geom["type"] == "Point":
            x, y = geom["coordinates"]
            geom["coordinates"] = list(transformer.transform(x, y))
        return geom

    for feat in gj["features"]:
        feat["geometry"] = reproject_geometry(feat["geometry"])

    gj["crs"] = {"type": "name", "properties": {"name": "EPSG:4326"}}

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(gj, f)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    print("Loading datacenters.geojson ...")
    dc_geo = load_datacenters_geojson(DATACENTERS_GEOJSON)
    print(f"  -> {len(dc_geo)} North America records with coordinates")

    print("Loading IM3 open-source atlas ...")
    im3 = load_im3_atlas(IM3_CSV)
    print(f"  -> {len(im3)} US records")

    combined = pd.concat([dc_geo, im3], ignore_index=True)
    combined = estimate_mw(combined)

    print("Deduplicating points within", DEDUP_RADIUS_M, "meters ...")
    before = len(combined)
    deduped = dedupe_points(combined)
    print(f"  -> {before} records reduced to {len(deduped)} after dedup")

    existing_out_csv = OUT_DIR / "harmonized_existing_dc.csv"
    deduped.to_csv(existing_out_csv, index=False)
    print(f"Wrote {existing_out_csv}")

    # write as GeoJSON too
    features = []
    for _, row in deduped.iterrows():
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": {
                k: (None if pd.isna(v) else v)
                for k, v in row.drop(["lat", "lon"]).items()
            },
        })
    geojson_out = {"type": "FeatureCollection", "features": features}
    existing_out_geojson = OUT_DIR / "harmonized_existing_dc.geojson"
    with open(existing_out_geojson, "w", encoding="utf-8") as f:
        json.dump(geojson_out, f)
    print(f"Wrote {existing_out_geojson}")

    print("Running QA against databricks state totals ...")
    qa = qa_against_databricks(deduped, DATABRICKS_CSV)
    qa_out = OUT_DIR / "qa_state_mw_totals.csv"
    qa.to_csv(qa_out, index=False)
    print(f"Wrote {qa_out}")
    print("\nTop 10 states where the point layer is missing the most known MW:")
    print(qa.head(10).to_string(index=False))

    print("\nReprojecting 2035 growth-gravity layer to WGS84 ...")
    growth_out = OUT_DIR / "growth_2035_wgs84.geojson"
    reproject_growth_layer(GROWTH_GEOJSON, growth_out)
    print(f"Wrote {growth_out}")

    print("\nDone. Review qa_state_mw_totals.csv first -- it tells you which states")
    print("need more point-level work before Phase 3's spatial join.")


if __name__ == "__main__":
    main()
