"""
harmonize_datacenters.py

Builds the North America existing-facility demand layer and the U.S. BA-level
Grid Headroom Stress Index (PROJECT_HANDOFF.md §4, steps 1-10):

   1. Load the global directory (datacenters.geojson, or datacenters.kml as a
      fallback) filtered to U.S. + Canada, and the IM3 atlas. Apply the manual
      geocode fixes in config.GEOCODE_FIXES.
   2. Estimate MW = sqft x W_PER_SQFT for IM3 *building* records. IM3 "campus"
      records carry the whole site's land area, not floor area, so they get no
      MW (mw_method = campus_land_area_excluded); everything else is "unknown".
   3. Drop U.S./Canada-labelled records whose coordinates fall outside the
      North America bbox -> flagged_bad_geocodes.csv.
   4. Point-in-polygon joins: BA (HIFLD Control Areas), state (TIGER),
      province (StatCan).
   5. Spatial dedup within DEDUP_RADIUS_M (see dedupe_points): IM3 buildings
      on the same campus are merged with their MW summed; directory/campus
      records that overlap an IM3 building are dropped as cross-source dupes.
   6. QA: point MW vs databricks.csv state totals -> qa_state_mw_totals.csv.
   7. Hybrid per-state scaling against the databricks *Active* total only:
      adjusted = max(point sum, active total) -> adjusted_baseline_load_by_state.csv.
   8. BA stress index, scaling each point by ITS OWN joined state's factor
      (not the BA's single HIFLD STATE field) -> ba_level_stress_index.csv.
   9. Canada province summary and the 2035 growth layer reprojected to WGS84.

ba_level_stress_index.csv is the static HIFLD (2020-vintage) snapshot kept for
comparison with the handoff: scaled DC load / HIFLD AVAIL_CAP. The canonical
year-by-year index, with capacity rolled forward from EIA-860M and a headroom
denominator, is built by us_time_series.py.
"""

import html
import json
import re
import sys

import numpy as np
import pandas as pd
from pyproj import Transformer
from sklearn.neighbors import BallTree

from config import (
    DATABRICKS_BASELINE_STAGES, DATABRICKS_CSV, DATACENTERS_GEOJSON, DATACENTERS_KML,
    DEDUP_RADIUS_M, EARTH_RADIUS_M, GEOCODE_FIXES, GROWTH_GEOJSON, IM3_CSV,
    IM3_EXCLUDED_TYPES, NA_BBOX_LAT, NA_BBOX_LON, NORTH_AMERICA_COUNTRIES, OUT_DIR, W_PER_SQFT,
)
from geo import join_ba, join_province, join_state, load_control_areas

POINT_COLUMNS = [
    "source", "name", "company", "city", "state", "country", "address",
    "lat", "lon", "sqft", "status", "im3_type",
]


# ----------------------------------------------------------------------------
# 1. Loaders
# ----------------------------------------------------------------------------
def load_datacenters_geojson(path) -> pd.DataFrame:
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)

    rows = []
    for feat in gj["features"]:
        props = feat["properties"]
        lon, lat = feat["geometry"]["coordinates"][:2]
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
            "status": np.nan,
            "im3_type": np.nan,
        })
    df = pd.DataFrame(rows, columns=POINT_COLUMNS)
    df["im3_type"] = df["im3_type"].astype(object)
    return df[df["country"].isin(NORTH_AMERICA_COUNTRIES)].reset_index(drop=True)


def load_datacenters_kml(path) -> pd.DataFrame:
    """Fallback when datacenters.geojson is missing. The KML carries the same
    6,131 points but only name/company/coordinates -- country is assigned later
    from the state/province joins, so records mislabelled as U.S. but geocoded
    abroad simply fall outside North America instead of being flagged."""
    text = open(path, "r", encoding="utf-8").read()
    pattern = re.compile(
        r"<Placemark>\s*<name>(.*?)</name>\s*(?:<description>(.*?)</description>)?\s*"
        r"<Point><coordinates>\s*([-\d.eE]+),([-\d.eE]+)",
        re.S,
    )
    rows = []
    for name, company, lon, lat in pattern.findall(text):
        rows.append({
            "source": "datacenters_kml",
            "name": html.unescape(name),
            "company": html.unescape(company) if company else np.nan,
            "city": np.nan, "state": np.nan, "country": np.nan, "address": np.nan,
            "lat": float(lat), "lon": float(lon),
            "sqft": np.nan, "status": np.nan, "im3_type": np.nan,
        })
    df = pd.DataFrame(rows, columns=POINT_COLUMNS)
    text_cols = ["city", "state", "country", "address", "status", "im3_type"]
    df[text_cols] = df[text_cols].astype(object)
    return df


def load_im3_atlas(path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"id": str, "state_id": str, "county_id": str})
    return pd.DataFrame({
        "source": "im3_atlas",
        "name": df["name"],
        "company": df["operator"],
        "city": np.nan,
        "state": df["state"],
        "country": "United States",   # IM3 atlas is U.S.-only per README
        "address": np.nan,
        "lat": df["lat"],
        "lon": df["lon"],
        "sqft": df["sqft"],
        "status": "existing",
        "im3_type": df["type"],      # building | campus | point
    }, columns=POINT_COLUMNS)


def apply_geocode_fixes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for fix in GEOCODE_FIXES:
        mask = df["name"].eq(fix["name"])
        if df["country"].notna().any():
            mask &= df["country"].eq(fix["country"]) | df["country"].isna()
        if not mask.any():
            print(f"  WARNING: geocode fix target not found: {fix['name']}")
            continue
        df.loc[mask, ["lat", "lon", "country"]] = [fix["lat"], fix["lon"], fix["country"]]
        df.loc[mask, "city"] = fix["city"]
        print(f"  Fixed geocode for {fix['name']} ({mask.sum()} row): {fix['note']}")
    return df


# ----------------------------------------------------------------------------
# 2-3. MW estimate and bad-geocode filter
# ----------------------------------------------------------------------------
def estimate_mw(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["mw_estimate"] = np.nan
    df["mw_method"] = "unknown"
    excluded = df["im3_type"].isin(IM3_EXCLUDED_TYPES)
    has_sqft = df["sqft"].notna() & (df["sqft"] > 0) & ~excluded
    df.loc[has_sqft, "mw_estimate"] = df.loc[has_sqft, "sqft"] * W_PER_SQFT / 1_000_000.0
    df.loc[has_sqft, "mw_method"] = "sqft_derived"
    # campus sqft is acquired land, not floor area -- x150 W/ft2 gave e.g. 2,121 MW
    # for Meta Huntsville. Keep the point for location, drop the MW.
    df.loc[excluded, "mw_method"] = "campus_land_area_excluded"
    return df


def in_na_bbox(df: pd.DataFrame) -> pd.Series:
    return df["lat"].between(*NA_BBOX_LAT) & df["lon"].between(*NA_BBOX_LON)


def split_bad_geocodes(df: pd.DataFrame):
    bad = df["country"].isin(NORTH_AMERICA_COUNTRIES) & ~in_na_bbox(df)
    return df[~bad].reset_index(drop=True), df[bad].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 4. Point-in-polygon joins
# ----------------------------------------------------------------------------
def spatial_join(df: pd.DataFrame, control_areas) -> pd.DataFrame:
    df = df.copy()
    lats, lons = df["lat"].to_numpy(), df["lon"].to_numpy()
    df["ba_name"] = join_ba(lats, lons, control_areas)
    df["joined_state"] = join_state(lats, lons)
    df["joined_province"] = join_province(lats, lons)
    return df


def assign_country_from_joins(df: pd.DataFrame) -> pd.DataFrame:
    """KML fallback only: rows with no country label take it from the joins;
    rows that land in neither a U.S. state nor a province are dropped."""
    df = df.copy()
    unlabelled = df["country"].isna()
    df.loc[unlabelled & df["joined_state"].notna(), "country"] = "United States"
    df.loc[unlabelled & df["joined_province"].notna(), "country"] = "Canada"
    df.loc[unlabelled & df["joined_state"].notna(), "state"] = df["joined_state"]
    return df[df["country"].isin(NORTH_AMERICA_COUNTRIES)].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 5. Spatial dedup
# ----------------------------------------------------------------------------
def _greedy_clusters(coords_deg: np.ndarray, order: np.ndarray, radius_m: float) -> np.ndarray:
    """Seed-based clustering: walk points in `order`; each unvisited point seeds a
    cluster of every unvisited point within radius_m of it. Returns the seed
    index for every point. Deterministic given `order`, and a cluster never
    spans more than radius_m from its seed (no chaining across a whole campus)."""
    seed_of = np.full(len(coords_deg), -1)
    if len(coords_deg) == 0:
        return seed_of
    coords_rad = np.radians(coords_deg)
    tree = BallTree(coords_rad, metric="haversine")
    neighbors = tree.query_radius(coords_rad, r=radius_m / EARTH_RADIUS_M)
    for i in order:
        if seed_of[i] >= 0:
            continue
        group = neighbors[i]
        group = group[seed_of[group] < 0]
        seed_of[group] = i
    return seed_of


def _within_radius(coords_deg: np.ndarray, anchors_deg: np.ndarray, radius_m: float) -> np.ndarray:
    if len(anchors_deg) == 0 or len(coords_deg) == 0:
        return np.zeros(len(coords_deg), dtype=bool)
    tree = BallTree(np.radians(anchors_deg), metric="haversine")
    dist, _ = tree.query(np.radians(coords_deg), k=1)
    return dist[:, 0] * EARTH_RADIUS_M <= radius_m


def dedupe_points(df: pd.DataFrame, radius_m: float = DEDUP_RADIUS_M) -> pd.DataFrame:
    """
    1. IM3 buildings with MW: cluster within radius_m (seeded largest-first) and
       collapse each cluster to one point at the seed, with sqft and MW SUMMED --
       these are distinct buildings on one campus, not duplicates.
    2. Every other record (directory, IM3 campus/point) within radius_m of any
       IM3 building is a cross-source duplicate of it and is dropped.
    3. The remaining records are deduplicated among themselves, keeping the
       first in file order (none of them carry MW).
    `merged_records` counts how many input rows each output row represents.
    """
    df = df.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    is_bldg = (df["source"] == "im3_atlas") & (df["mw_method"] == "sqft_derived")
    bldg, rest = df[is_bldg].reset_index(drop=True), df[~is_bldg].reset_index(drop=True)

    coords = bldg[["lat", "lon"]].to_numpy()
    order = np.argsort(-bldg["mw_estimate"].to_numpy(), kind="stable")
    bldg["_seed"] = _greedy_clusters(coords, order, radius_m)
    sums = bldg.groupby("_seed").agg(sqft=("sqft", "sum"), mw_estimate=("mw_estimate", "sum"),
                                     merged_records=("name", "size"))
    merged = bldg.loc[sums.index].copy()
    merged["sqft"] = sums["sqft"].to_numpy()
    merged["mw_estimate"] = sums["mw_estimate"].to_numpy()
    merged["merged_records"] = sums["merged_records"].to_numpy()

    overlaps = _within_radius(rest[["lat", "lon"]].to_numpy(), coords, radius_m)
    rest = rest[~overlaps].reset_index(drop=True)
    rest_seed = _greedy_clusters(rest[["lat", "lon"]].to_numpy(), np.arange(len(rest)), radius_m)
    counts = pd.Series(rest_seed).value_counts().sort_index()
    rest = rest.loc[counts.index].copy()
    rest["merged_records"] = counts.to_numpy()

    out = pd.concat([merged.drop(columns="_seed"), rest], ignore_index=True)
    out["merged_records"] = out["merged_records"].astype(int)
    print(f"  IM3 buildings: {len(bldg)} -> {len(merged)} campus points (MW summed); "
          f"{int(overlaps.sum())} cross-source duplicates dropped; "
          f"{len(rest)} directory/campus-only points kept")
    return out


# ----------------------------------------------------------------------------
# 6-7. QA against databricks + hybrid per-state scaling
# ----------------------------------------------------------------------------
def databricks_state_totals() -> pd.DataFrame:
    db = pd.read_csv(DATABRICKS_CSV)
    db["STATE_NAME"] = db["STATE_NAME"].str.replace(" Of ", " of ")   # match TIGER's "District of Columbia"
    total = db.groupby("STATE_NAME")["TOT_POWER_MW"].sum(min_count=1).rename("databricks_mw_total")
    active = (db[db["DATACENTER_STAGE"].isin(DATABRICKS_BASELINE_STAGES)]
              .groupby("STATE_NAME")["TOT_POWER_MW"].sum(min_count=1)
              .rename("databricks_active_mw"))
    out = pd.concat([total, active], axis=1).reset_index().rename(columns={"STATE_NAME": "state"})
    return out


def qa_against_databricks(points: pd.DataFrame, db_totals: pd.DataFrame) -> pd.DataFrame:
    point_totals = (
        points[points["country"] == "United States"]
        .groupby("joined_state")["mw_estimate"].sum(min_count=1)
        .rename("point_layer_mw_total").reset_index().rename(columns={"joined_state": "state"})
    )
    qa = db_totals.merge(point_totals, on="state", how="left")
    qa["point_layer_mw_total"] = qa["point_layer_mw_total"].fillna(0.0)
    qa["databricks_active_mw"] = qa["databricks_active_mw"].fillna(0.0)
    qa["gap_mw"] = qa["databricks_active_mw"] - qa["point_layer_mw_total"]
    qa["pct_of_known_total_captured"] = np.where(
        qa["databricks_active_mw"] > 0,
        100.0 * qa["point_layer_mw_total"] / qa["databricks_active_mw"], np.nan)
    cols = ["state", "databricks_active_mw", "point_layer_mw_total", "gap_mw",
            "pct_of_known_total_captured", "databricks_mw_total"]
    return qa[cols].sort_values("gap_mw", ascending=False).reset_index(drop=True)


def adjusted_baseline(qa: pd.DataFrame) -> pd.DataFrame:
    adj = qa[["state", "point_layer_mw_total", "databricks_active_mw"]].copy()
    use_db = adj["databricks_active_mw"] > adj["point_layer_mw_total"]
    adj["adjusted_baseline_mw"] = np.where(use_db, adj["databricks_active_mw"], adj["point_layer_mw_total"])
    adj["source_used"] = np.where(use_db, "databricks_active_total", "point_layer_sum")
    adj["scale_factor"] = np.where(
        adj["point_layer_mw_total"] > 0,
        adj["adjusted_baseline_mw"] / adj["point_layer_mw_total"], np.nan)
    # with no point MW in the state there is nothing to scale up, so the databricks
    # total can't be placed on the map -- flag it rather than silently dropping it
    unplaceable = use_db & (adj["point_layer_mw_total"] <= 0)
    adj.loc[unplaceable, "source_used"] = "databricks_active_total_unplaceable"
    return adj.sort_values("adjusted_baseline_mw", ascending=False).reset_index(drop=True)


# ----------------------------------------------------------------------------
# 8. BA stress index
# ----------------------------------------------------------------------------
def ba_stress_index(points: pd.DataFrame, adj: pd.DataFrame, ba_recs: pd.DataFrame) -> pd.DataFrame:
    factors = adj.set_index("state")["scale_factor"]
    pts = points[points["ba_name"].notna()].copy()
    pts["scale_factor"] = pts["joined_state"].map(factors).fillna(1.0)
    pts["scaled_mw"] = pts["mw_estimate"].fillna(0.0) * pts["scale_factor"]

    per_ba = pts.groupby("ba_name").agg(
        point_mw_sum=("mw_estimate", lambda s: s.fillna(0.0).sum()),
        scaled_dc_load_mw=("scaled_mw", "sum"),
        facility_count=("name", "size"),
        unknown_mw_count=("mw_method", lambda s: (s != "sqft_derived").sum()),
        states_touched=("joined_state", "nunique"),
    ).reset_index()

    ba = ba_recs.rename(columns={
        "NAME": "ba_name", "STATE": "ba_state", "COUNTRY": "ba_country",
        "TOTAL_CAP": "total_cap_mw", "PEAK_LOAD": "peak_load_mw",
        "MIN_LOAD": "min_load_mw", "AVAIL_CAP": "avail_cap_mw",
    })[["ba_name", "ba_state", "ba_country", "total_cap_mw", "peak_load_mw",
        "min_load_mw", "avail_cap_mw"]]
    ba = ba.merge(per_ba, on="ba_name", how="left")
    for col in ["point_mw_sum", "scaled_dc_load_mw", "facility_count", "unknown_mw_count", "states_touched"]:
        ba[col] = ba[col].fillna(0)

    derived = ba["total_cap_mw"] - ba["peak_load_mw"]
    use_avail = ba["avail_cap_mw"] > 0
    use_derived = ~use_avail & (derived > 0)
    ba["headroom_mw"] = np.where(use_avail, ba["avail_cap_mw"], np.where(use_derived, derived, np.nan))
    ba["headroom_source"] = np.where(use_avail, "AVAIL_CAP",
                                     np.where(use_derived, "TOTAL_CAP-PEAK_LOAD", "none"))
    ba["stress_index"] = ba["scaled_dc_load_mw"] / ba["headroom_mw"]
    return ba.sort_values("stress_index", ascending=False, na_position="last").reset_index(drop=True)


# ----------------------------------------------------------------------------
# 9. Canada summary + growth layer reprojection
# ----------------------------------------------------------------------------
def canada_province_summary(points: pd.DataFrame) -> pd.DataFrame:
    ca = points[points["country"] == "Canada"]
    return (
        ca.groupby("joined_province", dropna=True)
        .agg(facility_count=("name", "size"),
             known_mw_total=("mw_estimate", lambda s: s.fillna(0.0).sum()),
             unknown_mw_count=("mw_method", lambda s: (s == "unknown").sum()))
        .reset_index().sort_values("facility_count", ascending=False)
    )


def reproject_growth_layer(path, out_path) -> None:
    with open(path, "r", encoding="utf-8") as f:
        gj = json.load(f)
    src_crs = gj.get("crs", {}).get("properties", {}).get("name", "ESRI:102003")
    transformer = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)

    def ring(r):
        xs, ys = transformer.transform(*zip(*[(p[0], p[1]) for p in r]))
        return [list(p) for p in zip(xs, ys)]

    for feat in gj["features"]:
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            geom["coordinates"] = [ring(r) for r in geom["coordinates"]]
        elif geom["type"] == "MultiPolygon":
            geom["coordinates"] = [[ring(r) for r in poly] for poly in geom["coordinates"]]
        elif geom["type"] == "Point":
            geom["coordinates"] = list(transformer.transform(*geom["coordinates"][:2]))
    gj["crs"] = {"type": "name", "properties": {"name": "EPSG:4326"}}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(gj, f)


def write_points_geojson(df: pd.DataFrame, path) -> None:
    features = []
    for _, row in df.iterrows():
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": {k: (None if pd.isna(v) else v) for k, v in row.drop(["lat", "lon"]).items()},
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if DATACENTERS_GEOJSON.exists():
        print("Loading datacenters.geojson ...")
        directory = load_datacenters_geojson(DATACENTERS_GEOJSON)
    else:
        print("WARNING: datacenters.geojson not found -- falling back to datacenters.kml.")
        print("         Country is inferred from the state/province joins, city/address are blank,")
        print("         and flagged_bad_geocodes.csv only lists what the fixes/bbox can detect.")
        directory = load_datacenters_kml(DATACENTERS_KML)
    print(f"  -> {len(directory)} directory records")
    directory = apply_geocode_fixes(directory)

    im3 = load_im3_atlas(IM3_CSV)
    print(f"  -> {len(im3)} IM3 atlas records")

    combined = estimate_mw(pd.concat([directory, im3], ignore_index=True))

    combined, flagged = split_bad_geocodes(combined)
    flagged.to_csv(OUT_DIR / "flagged_bad_geocodes.csv", index=False)
    print(f"Bad-geocode filter: {len(flagged)} labelled U.S./Canada records outside the NA bbox")
    if combined["country"].isna().any():
        before = len(combined)
        combined = combined[combined["country"].notna() | in_na_bbox(combined)].reset_index(drop=True)
        print(f"  (KML fallback) dropped {before - len(combined)} unlabelled points outside the NA bbox")

    print("Point-in-polygon joins (BA, state, province) ...")
    control_areas = load_control_areas()
    combined = spatial_join(combined, control_areas)
    if combined["country"].isna().any():
        before = len(combined)
        combined = assign_country_from_joins(combined)
        print(f"  (KML fallback) {before - len(combined)} points outside U.S. states/provinces dropped")

    print(f"Deduplicating within {DEDUP_RADIUS_M:.0f} m ...")
    before = len(combined)
    points = dedupe_points(combined)
    print(f"  -> {before} records reduced to {len(points)}")

    base_cols = POINT_COLUMNS + ["mw_estimate", "mw_method", "merged_records"]
    points[base_cols].to_csv(OUT_DIR / "harmonized_existing_dc.csv", index=False)
    write_points_geojson(points[base_cols], OUT_DIR / "harmonized_existing_dc.geojson")
    points.to_csv(OUT_DIR / "harmonized_existing_dc_with_ba.csv", index=False)
    unmatched = points["ba_name"].isna()
    print(f"  {unmatched.sum()} points outside every BA polygon "
          f"({(unmatched & points['country'].eq('Canada')).sum()} Canadian)")

    print("QA against databricks state totals ...")
    qa = qa_against_databricks(points, databricks_state_totals())
    qa.to_csv(OUT_DIR / "qa_state_mw_totals.csv", index=False)
    adj = adjusted_baseline(qa)
    adj.to_csv(OUT_DIR / "adjusted_baseline_load_by_state.csv", index=False)

    print("BA stress index (per-point state scaling) ...")
    ba = ba_stress_index(points, adj, control_areas[0])
    ba.to_csv(OUT_DIR / "ba_level_stress_index.csv", index=False)
    print(ba[["ba_name", "scaled_dc_load_mw", "headroom_mw", "stress_index"]].head(10).to_string(index=False))

    canada_province_summary(points).to_csv(OUT_DIR / "canada_dc_by_province.csv", index=False)

    print("Reprojecting 2035 growth layer to WGS84 ...")
    reproject_growth_layer(GROWTH_GEOJSON, OUT_DIR / "growth_2035_wgs84.geojson")
    print("harmonize_datacenters.py done.")


if __name__ == "__main__":
    sys.exit(main())
