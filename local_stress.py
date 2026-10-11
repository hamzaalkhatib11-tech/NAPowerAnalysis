"""
local_stress.py

Local (sub-BA) data-centre stress on a ~25 km hexagon grid, yearly 2025-2035.

The BA index (us_time_series.py) asks "is there enough generation in the
region?". This one asks "can power be delivered *here*?" -- the transmission /
substation side of the project question.

  LOCAL_STRESS(hex, y) = DC_LOAD_MW / LOCAL_SUPPLY_MW

  DC_LOAD_MW       existing data centres (point MW scaled to Databricks Active
                   state totals -- the same scaling as the BA tier, so hexes
                   sum to the BA loads) + IM3 2035 sites x PUE x ramp(y)
  LOCAL_SUPPLY_MW  LOCAL_GEN_MW + TX_CAPACITY_MW
    LOCAL_GEN_MW   EIA-860M units located in the hex and online in year y
                   (operating/retired/committed-planned dates), plus the
                   BA's build-rate top-up placed where that BA built in
                   2021-2025 (new capacity sited like recent capacity)
    TX_CAPACITY_MW HIFLD transmission lines >= MIN_LINE_KV (115 kV) CROSSING the
                   hex boundary, each counted once and rated with the FERC-data
                   voltage fit of Athari & Wang 2017 (LINE_RATING_FIT); static.
                   Segments entirely inside a
                   hex are left out -- HIFLD splits circuits at every
                   substation, so summing internal segments counted a dense
                   metro network many times over (52 GW for one Loudoun hex).
                   Lines with unknown voltage are left out.

No local non-data-centre load is subtracted (a team decision: no population
data), so the index reads as "share of the high-voltage supply reachable in
this hex that data centres would use". It ranks hexes against each other; 1.0
is NOT a capacity gap the way it is for the BA index. A screening index, not a
power-flow study.

Hexes with at least MIN_DC_MW_FOR_HV of data-centre load but no >= 115 kV line
and no generation get NO_HV_ACCESS = 1, LOCAL_STRESS = null and
LOCAL_STRESS_CAPPED = LOCAL_STRESS_CAP. Sites under MIN_DC_MW_FOR_HV with no
>= 115 kV line are assumed to run off the distribution network and get 0.
LOCAL_STRESS_CAPPED is the field to feed Emerging Hot Spot Analysis.

Writes to output/:
  us_hex_local_stress_2025_2035.csv   one row per hex per year (all CONUS hexes)
  us_hex_grid.geojson                 hex polygons (WGS84) + static attributes
"""

import json
import zipfile

import numpy as np
import pandas as pd
import pyogrio
import shapefile
import shapely
from pyproj import Transformer
from shapely.geometry import shape as shapely_shape

from config import (
    EIA_PLANNED_STATUSES, EXCLUDED_STATES, GROWTH_PUE, HEX_CRS, HEX_WIDTH_M,
    LINE_KV_VALID, LINE_RATING_FIT, LOCAL_STRESS_CAP, MIN_DC_MW_FOR_HV, MIN_LINE_KV, OUT_DIR, TIGER_STATES_ZIP,
    TRANSMISSION_ZIP, TREND_HISTORY_YEARS, TS_YEARS,
)
from geo import join_ba, load_control_areas, read_prj_from_zip
from us_time_series import load_generators, ramp

WGS84 = "EPSG:4326"
TO_HEX = Transformer.from_crs(WGS84, HEX_CRS, always_xy=True)
TO_WGS = Transformer.from_crs(HEX_CRS, WGS84, always_xy=True)


# ----------------------------------------------------------------------------
# Grid
# ----------------------------------------------------------------------------
def conus_states():
    reader = shapefile.Reader(str(TIGER_STATES_ZIP))
    src = Transformer.from_crs(read_prj_from_zip(TIGER_STATES_ZIP), HEX_CRS, always_xy=True)
    names, geoms = [], []
    for sr in reader.iterShapeRecords():
        if sr.record["STUSPS"] in EXCLUDED_STATES:
            continue
        pts = np.asarray(sr.shape.points)
        bounds = list(sr.shape.parts) + [len(pts)]
        rings = [shapely.polygons(pts[a:b]) for a, b in zip(bounds[:-1], bounds[1:]) if b - a >= 4]
        geom = shapely.make_valid(shapely.symmetric_difference_all(rings))
        names.append(sr.record["NAME"])
        geoms.append(shapely.transform(geom, lambda xy: np.column_stack(src.transform(xy[:, 0], xy[:, 1]))))
    return names, np.array(geoms)


def hex_grid(state_names, state_geoms) -> pd.DataFrame:
    """Flat-topped hexagons covering CONUS, kept where they touch a state."""
    side = HEX_WIDTH_M / np.sqrt(3)
    xmin, ymin, xmax, ymax = shapely.total_bounds(state_geoms)
    dx, dy = 1.5 * side, HEX_WIDTH_M
    centers = []
    for i, x in enumerate(np.arange(xmin - side, xmax + side, dx)):
        offset = dy / 2 if i % 2 else 0.0
        for y in np.arange(ymin - dy, ymax + dy, dy):
            centers.append((x, y + offset))
    centers = np.array(centers)
    angles = np.radians(np.arange(0, 360, 60))
    ring = np.stack([np.cos(angles), np.sin(angles)], axis=1) * side
    hexes = shapely.polygons(centers[:, None, :] + ring[None, :, :])

    hex_idx, state_idx = shapely.STRtree(state_geoms).query(hexes, predicate="intersects")
    keep = np.unique(hex_idx)
    hexes, centers = hexes[keep], centers[keep]

    # state = the one containing the centre, else the first one touched
    cpts = shapely.points(centers)
    c_hex, c_state = shapely.STRtree(state_geoms).query(cpts, predicate="within")
    state = np.array([None] * len(hexes), dtype=object)
    state[c_hex] = np.array(state_names, dtype=object)[c_state]
    first = pd.Series(state_idx, index=hex_idx).groupby(level=0).first()
    missing = np.where(state == None)[0]  # noqa: E711
    state[missing] = np.array(state_names, dtype=object)[first.loc[keep[missing]].to_numpy()]

    lon, lat = TO_WGS.transform(centers[:, 0], centers[:, 1])
    return pd.DataFrame({"HEX_ID": [f"H{i:05d}" for i in range(len(hexes))], "geometry": hexes,
                         "STATE": state, "lon": lon, "lat": lat})


def assign_to_hex(lons, lats, hexes: pd.DataFrame) -> np.ndarray:
    """Hex index for each lon/lat point, or -1."""
    x, y = TO_HEX.transform(np.asarray(lons, float), np.asarray(lats, float))
    pts = shapely.points(x, y)
    p_idx, h_idx = shapely.STRtree(hexes["geometry"].to_numpy()).query(pts, predicate="intersects")
    out = np.full(len(pts), -1)
    out[p_idx[::-1]] = h_idx[::-1]          # first match wins on shared edges
    return out


# ----------------------------------------------------------------------------
# Demand
# ----------------------------------------------------------------------------
def existing_load(hexes) -> np.ndarray:
    pts = pd.read_csv(OUT_DIR / "harmonized_existing_dc_with_ba.csv")
    pts = pts[pts["country"] == "United States"]
    factors = pd.read_csv(OUT_DIR / "adjusted_baseline_load_by_state.csv").set_index("state")["scale_factor"]
    mw = pts["mw_estimate"].fillna(0.0) * pts["joined_state"].map(factors).fillna(1.0)
    idx = assign_to_hex(pts["lon"], pts["lat"], hexes)
    return np.bincount(idx[idx >= 0], weights=mw.to_numpy()[idx >= 0], minlength=len(hexes))


def new_load_2035(hexes) -> np.ndarray:
    with open(OUT_DIR / "growth_2035_wgs84.geojson", encoding="utf-8") as f:
        feats = json.load(f)["features"]
    cent = shapely.centroid([shapely_shape(ft["geometry"]) for ft in feats])
    mw = np.array([ft["properties"]["data_center_it_power_mw"] for ft in feats]) * GROWTH_PUE
    idx = assign_to_hex(shapely.get_x(cent), shapely.get_y(cent), hexes)
    lost = mw[idx < 0].sum()
    if lost:
        print(f"  WARNING: {lost:,.0f} MW of growth sites fall outside the hex grid")
    return np.bincount(idx[idx >= 0], weights=mw[idx >= 0], minlength=len(hexes))


# ----------------------------------------------------------------------------
# Supply
# ----------------------------------------------------------------------------
def local_generation(hexes) -> dict:
    """{year: MW per hex}, including the BA build-rate top-up sited like 2021-2025 builds."""
    sheets = load_generators()
    op, pl, rt = sheets["Operating"], sheets["Planned"], sheets["Retired"]
    pl = pl[pl["Status"].astype(str).str.startswith(EIA_PLANNED_STATUSES)]
    units = pd.concat([
        pd.DataFrame({"on": op["Operating Year"], "off": op["Planned Retirement Year"], "mw": op["mw"],
                      "lat": op["Latitude"], "lon": op["Longitude"], "ba": op["ba_name"]}),
        pd.DataFrame({"on": rt["Operating Year"], "off": rt["Retirement Year"], "mw": rt["mw"],
                      "lat": rt["Latitude"], "lon": rt["Longitude"], "ba": rt["ba_name"]}),
        pd.DataFrame({"on": pl["Planned Operation Year"], "off": np.nan, "mw": pl["mw"],
                      "lat": pl["Latitude"], "lon": pl["Longitude"], "ba": pl["ba_name"]}),
    ], ignore_index=True).dropna(subset=["lat", "lon"])
    units["hex"] = assign_to_hex(units["lon"], units["lat"], hexes)
    units = units[units["hex"] >= 0]

    # where each BA built recently -> where its top-up goes
    h0, h1 = TREND_HISTORY_YEARS
    recent = units[units["on"].between(h0, h1) & units["ba"].notna()]
    siting = recent.groupby(["ba", "hex"])["mw"].sum()
    siting = siting / siting.groupby(level=0).transform("sum")
    trend = pd.read_csv(OUT_DIR / "us_ba_capacity_changes.csv").set_index(["ba_name", "year"])["cap_trend_added_mw"]

    out = {}
    for year in TS_YEARS:
        online = units[(units["on"] <= year) & ~(units["off"] <= year)]
        gen = np.bincount(online["hex"], weights=online["mw"], minlength=len(hexes))
        topup = siting * siting.index.get_level_values(0).map(lambda b: trend.get((b, year), 0.0)).to_numpy()
        by_hex = topup.groupby(level=1).sum()
        gen[by_hex.index.to_numpy()] += by_hex.to_numpy()
        out[year] = gen
    return out


def line_rating(kv: float) -> float:
    """Average FERC line rating at this voltage (Athari & Wang 2017, Fig. 13), MW."""
    a, b = LINE_RATING_FIT
    return a * kv ** b if kv >= MIN_LINE_KV else 0.0


def load_hv_lines() -> pd.DataFrame:
    """Transmission lines >= MIN_LINE_KV, in HEX_CRS, with a MW rating."""
    name = next(n for n in zipfile.ZipFile(TRANSMISSION_ZIP).namelist() if n.endswith(".shp"))
    path = f"/vsizip/{TRANSMISSION_ZIP}/{name}"
    meta, _, geom, fields = pyogrio.raw.read(path, columns=["VOLTAGE", "VOLT_CLASS", "STATUS", "OWNER"])
    df = pd.DataFrame(dict(zip(meta["fields"], fields)))
    kv = pd.to_numeric(df["VOLTAGE"], errors="coerce")
    df["kv"] = kv.where(kv.between(*LINE_KV_VALID))          # drops the -999999 "unknown" code
    keep = (df["kv"] >= MIN_LINE_KV) & (df["STATUS"] != "INACTIVE")
    df = df[keep].copy()
    src = Transformer.from_crs(meta["crs"], HEX_CRS, always_xy=True)
    lines = shapely.from_wkb(geom[keep.to_numpy()])
    df["geometry"] = shapely.transform(lines, lambda xy: np.column_stack(src.transform(xy[:, 0], xy[:, 1])))
    df["rating_mw"] = df["kv"].map(line_rating)
    return df.reset_index(drop=True)


def transmission_capacity(hexes, lines) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    boundaries = shapely.boundary(hexes["geometry"].to_numpy())
    h_idx, l_idx = shapely.STRtree(lines["geometry"].to_numpy()).query(boundaries, predicate="intersects")
    pairs = pd.DataFrame({"hex": h_idx, "mw": lines["rating_mw"].to_numpy()[l_idx],
                          "kv": lines["kv"].to_numpy()[l_idx]})
    g = pairs.groupby("hex")
    cap, n, kv = (np.zeros(len(hexes)) for _ in range(3))
    cap[g["mw"].sum().index] = g["mw"].sum().to_numpy()
    n[g.size().index] = g.size().to_numpy()
    kv[g["kv"].max().index] = g["kv"].max().to_numpy()
    return cap, n, kv


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    print("Building hex grid ...")
    names, geoms = conus_states()
    hexes = hex_grid(names, geoms)
    hexes["BA_NAME"] = join_ba(hexes["lat"], hexes["lon"], load_control_areas())
    print(f"  {len(hexes):,} hexes ({HEX_WIDTH_M / 1000:.0f} km across)")

    print("Demand ...")
    dc_existing = existing_load(hexes)
    dc_new = new_load_2035(hexes)
    print(f"  existing {dc_existing.sum():,.0f} MW in {(dc_existing > 0).sum()} hexes; "
          f"new by 2035 {dc_new.sum():,.0f} MW in {(dc_new > 0).sum()} hexes")

    print("Supply ...")
    lines = load_hv_lines()
    tx, n_lines, max_kv = transmission_capacity(hexes, lines)
    print(f"  {len(lines):,} lines >= {MIN_LINE_KV} kV; {(tx > 0).sum():,} hexes reach one")
    gen = local_generation(hexes)

    ba_ts = pd.read_csv(OUT_DIR / "us_ba_time_series_2025_2035.csv").set_index(["BA_NAME", "YEAR"])

    frames = []
    for year in TS_YEARS:
        load = dc_existing + dc_new * ramp(year)
        supply = gen[year] + tx
        # small sites with no HV line are distribution-served: not a pressure point
        distribution = (load > 0) & (load < MIN_DC_MW_FOR_HV) & (tx <= 0)
        no_access = (load >= MIN_DC_MW_FOR_HV) & (supply <= 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            stress = np.where((load > 0) & ~distribution & ~no_access, load / supply, 0.0)
        stress[no_access] = np.nan
        capped = np.where(no_access, LOCAL_STRESS_CAP, np.minimum(np.nan_to_num(stress), LOCAL_STRESS_CAP))
        ba_stress = [ba_ts["STRESS_INDEX"].get((b, year), np.nan) if b else np.nan for b in hexes["BA_NAME"]]
        frames.append(pd.DataFrame({
            "HEX_ID": hexes["HEX_ID"], "YEAR": year, "YEAR_DATE": f"{year}-01-01",
            "STATE": hexes["STATE"], "BA_NAME": hexes["BA_NAME"],
            "DC_LOAD_MW": load, "DC_EXISTING_MW": dc_existing, "DC_NEW_MW": dc_new * ramp(year),
            "LOCAL_GEN_MW": gen[year], "TX_CAPACITY_MW": tx, "LOCAL_SUPPLY_MW": supply,
            "LOCAL_STRESS": stress, "LOCAL_STRESS_CAPPED": capped,
            "NO_HV_ACCESS": no_access.astype(int), "BA_STRESS_INDEX": ba_stress,
        }))
    ts = pd.concat(frames, ignore_index=True)
    ts.to_csv(OUT_DIR / "us_hex_local_stress_2025_2035.csv", index=False, float_format="%.4f")

    wgs = shapely.transform(hexes["geometry"].to_numpy(),
                            lambda xy: np.column_stack(TO_WGS.transform(xy[:, 0], xy[:, 1])))
    features = [{"type": "Feature",
                 "properties": {"HEX_ID": h, "STATE": s, "BA_NAME": b, "TX_CAPACITY_MW": float(t),
                                "HV_LINES": int(n), "MAX_KV": float(k)},
                 "geometry": json.loads(shapely.to_geojson(shapely.set_precision(g, 1e-5)))}
                for h, s, b, t, n, k, g in zip(hexes["HEX_ID"], hexes["STATE"], hexes["BA_NAME"],
                                               tx, n_lines, max_kv, wgs)]
    with open(OUT_DIR / "us_hex_grid.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    print(f"Wrote us_hex_local_stress_2025_2035.csv ({len(ts):,} rows) and us_hex_grid.geojson")

    for year in (TS_YEARS[0], TS_YEARS[-1]):
        y = ts[(ts["YEAR"] == year) & (ts["DC_LOAD_MW"] > 0)]
        print(f"\n{year}: {len(y)} hexes with data-centre load; {int(y['NO_HV_ACCESS'].sum())} without HV access; "
              f"{int((y['LOCAL_STRESS'] > 1).sum())} over 1.0")
        print(y.sort_values("LOCAL_STRESS_CAPPED", ascending=False).head(10)
              [["HEX_ID", "STATE", "BA_NAME", "DC_LOAD_MW", "LOCAL_SUPPLY_MW", "LOCAL_STRESS"]]
              .to_string(index=False, float_format=lambda v: f"{v:,.2f}"))


if __name__ == "__main__":
    main()
