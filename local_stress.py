"""
local_stress.py

Local (sub-BA) capacity gaps on a ~25 km hexagon grid, yearly 2025-2035, and
yearly hot spots of those gaps for the dashboard's year slider.

The BA tier says whether a region has headroom; this says whether the part of
that headroom deliverable in a given place covers the data centres planned there.

  BA_HEADROOM_SHARE_MW = BA AVAIL_CAP_MW (capacity - non-DC baseline peak, from
                         us_time_series.py) x the hex's share of the BA's grid
                         strength
  LOCAL_HEADROOM_MW    = max(BA_HEADROOM_SHARE_MW, DC_EXISTING_MW)
                         -- what runs today is served, so 2025 has no gaps
  LOCAL_STRESS         = DC_LOAD_MW / LOCAL_HEADROOM_MW      (> 1 = local gap)
  DC_GAP_MW            = max(DC_LOAD_MW - LOCAL_HEADROOM_MW, 0)

  DC_LOAD_MW       existing data centres (point MW scaled to Databricks Active
                   state totals, as in the BA tier) + IM3 2035 sites x PUE x ramp(y)
  GRID_STRENGTH_MW LOCAL_GEN_MW + TX_CAPACITY_MW -- used only as a *share*
    LOCAL_GEN_MW   EIA-860M units in the hex online in year y, plus the BA's
                   build-rate top-up placed where that BA built in 2021-2025
    TX_CAPACITY_MW HIFLD lines >= MIN_LINE_KV (115 kV) that cross into the hex AND
                   reach an OSM substation >= 115 kV inside it (within
                   SUBSTATION_SNAP_M; pass-through lines serve elsewhere),
                   each counted once, rated with the FERC-data voltage fit of
                   Athari & Wang 2017 (LINE_RATING_FIT). Internal segments are
                   left out (HIFLD splits circuits at every substation).

Hex shares add up to the BA's headroom, so the tiers agree: a BA can be fine
overall while the hexes where data centres cluster run out.

Why shares rather than absolute local supply/demand (both were tested):
  - gross line ratings as local supply count through-flows as local, so almost
    nothing shows a gap (1 hex in 2035, even around Loudoun);
  - spreading BA peak demand by Census population against grid-strength supply
    marks nearly every city as short before any data centre (154 of 225), since
    cities import and HIFLD misses dense urban networks.

LOCAL_CLASS: no_dc | distribution_served (< MIN_DC_MW_FOR_HV, no HV line) |
surplus | near_limit (>= 0.9) | gap (> 1, or new load where the BA has no
headroom) | no_capacity_data | data_artifact (BA flagged in the BA tier).

Hot spots: Getis-Ord Gi* on DC_GAP_MW each year across all CONUS hexes
(GI_DISTANCE_M, FDR-corrected), standardized against the pooled 2025-2035
mean and spread so years are comparable: GI_Z, GI_P, GI_BIN (-3..3, ArcGIS
convention), GI_PATTERN per hex per year -- time-enabled for the slider.
LOCAL_STRESS_CAPPED feeds Emerging Hot Spot Analysis (decade trend).

A screening model, not a power-flow study.

Writes to output/:
  us_hex_local_stress_2025_2035.csv   one row per hex per year (all CONUS hexes)
  us_hex_grid.geojson                 hex polygons (WGS84) + static attributes
"""

import json
import math
import zipfile

import numpy as np
import pandas as pd
import pyogrio
import shapefile
import shapely
from pyproj import Transformer
from shapely.geometry import shape as shapely_shape

from sklearn.neighbors import BallTree

from config import (
    GI_DISTANCE_M, GI_FDR, EIA_PLANNED_STATUSES, EXCLUDED_STATES, GROWTH_PUE, HEX_CRS, HEX_WIDTH_M,
    LINE_KV_VALID, LINE_RATING_FIT, SUBSTATION_SNAP_M, US_SUBSTATIONS_DIR, LOCAL_STRESS_CAP, MIN_DC_MW_FOR_HV, MIN_LINE_KV, OUT_DIR, TIGER_STATES_ZIP,
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


def hex_grid(state_names, state_geoms, to_wgs=TO_WGS) -> pd.DataFrame:
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

    lon, lat = to_wgs.transform(centers[:, 0], centers[:, 1])
    return pd.DataFrame({"HEX_ID": [f"H{i:05d}" for i in range(len(hexes))], "geometry": hexes,
                         "STATE": state, "lon": lon, "lat": lat})


def assign_to_hex(lons, lats, hexes: pd.DataFrame, to_hex=TO_HEX) -> np.ndarray:
    """Hex index for each lon/lat point, or -1."""
    x, y = to_hex.transform(np.asarray(lons, float), np.asarray(lats, float))
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


def osm_kv(voltage) -> float:
    """Highest voltage in an OSM voltage tag ("230000;115000" -> 230), in kV."""
    import re
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(voltage or ""))]
    return max(nums) / 1000 if nums else 0.0


def load_osm_substations(paths, to_hex) -> np.ndarray:
    """(x, y) in the hex CRS of OSM substations >= MIN_LINE_KV, de-duplicated by OSM id."""
    seen, xy = set(), []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                elements = json.load(f).get("elements", [])
        except (json.JSONDecodeError, OSError):
            print(f"  WARNING: {path.name} unreadable -- skipped")
            continue
        for e in elements:
            key = (e.get("type"), e.get("id"))
            if key in seen or osm_kv(e.get("tags", {}).get("voltage")) < MIN_LINE_KV:
                continue
            pt = e.get("center") or e
            if "lat" in pt:
                seen.add(key)
                xy.append(to_hex.transform(pt["lon"], pt["lat"]))
    return np.array(xy).reshape(-1, 2)


def transmission_capacity(hexes, lines, sub_xy: np.ndarray | None = None):
    """Per hex: rated MW and count of >= MIN_LINE_KV lines that deliver into it, max kV,
    and number of substations.

    A line counts for a hex when it crosses the hex boundary AND comes within
    SUBSTATION_SNAP_M of a substation inside the hex -- power is taken off a line
    only at a substation, so a line that just passes through serves somewhere
    else. Without substation data, falls back to boundary crossings alone."""
    geoms = hexes["geometry"].to_numpy()
    line_tree = shapely.STRtree(lines["geometry"].to_numpy())
    h_idx, l_idx = line_tree.query(shapely.boundary(geoms), predicate="intersects")
    n_subs = np.zeros(len(hexes))
    if sub_xy is not None and len(sub_xy):
        sub_pts = shapely.points(sub_xy)
        s_idx, s_hex = shapely.STRtree(geoms).query(sub_pts, predicate="intersects")
        hex_of_sub = pd.Series(s_hex, index=s_idx).groupby(level=0).first()
        n_subs = np.bincount(hex_of_sub.to_numpy(), minlength=len(hexes)).astype(float)
        sp_idx, sl_idx = line_tree.query(sub_pts, predicate="dwithin", distance=SUBSTATION_SNAP_M)
        keep = np.isin(sp_idx, hex_of_sub.index.to_numpy())
        delivering = set(zip(hex_of_sub.loc[sp_idx[keep]].to_numpy(), sl_idx[keep]))
        mask = np.array([(h, l) in delivering for h, l in zip(h_idx, l_idx)], dtype=bool)
        print(f"  {mask.sum():,} of {len(mask):,} line crossings reach a substation in the hex")
        h_idx, l_idx = h_idx[mask], l_idx[mask]
    else:
        print("  WARNING: no substation data -- counting every line that crosses a hex boundary")
    pairs = pd.DataFrame({"hex": h_idx, "mw": lines["rating_mw"].to_numpy()[l_idx],
                          "kv": lines["kv"].to_numpy()[l_idx]})
    g = pairs.groupby("hex")
    cap, n, kv = (np.zeros(len(hexes)) for _ in range(3))
    if len(pairs):
        cap[g["mw"].sum().index] = g["mw"].sum().to_numpy()
        n[g.size().index] = g.size().to_numpy()
        kv[g["kv"].max().index] = g["kv"].max().to_numpy()
    return cap, n, kv, n_subs


# ----------------------------------------------------------------------------
# Local headroom
# ----------------------------------------------------------------------------
def allocate_ba_value(hexes, strength: np.ndarray, ba_value: pd.Series, year: int) -> np.ndarray:
    """A BA-level MW value for the year, shared across the BA's hexes by grid strength.

    Gross line ratings say where power can be delivered, not how much is spare
    (through-flows are counted as if local), so they set the *share* only; the
    amount comes from the BA tier, so hex values add up to the BA's. NaN where the
    BA has no capacity data or the hex is outside every BA."""
    ba = hexes["BA_NAME"].to_numpy()
    df = pd.DataFrame({"ba": ba, "w": strength})
    total_w = df.groupby("ba")["w"].transform("sum").to_numpy()
    cap = np.array([ba_value.get((b, year), np.nan) if isinstance(b, str) else np.nan for b in ba])
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(total_w > 0, cap * strength / total_w, np.nan)


# ----------------------------------------------------------------------------
# Hot spots (Getis-Ord Gi*)
# ----------------------------------------------------------------------------
class GiStar:
    """Binary fixed-distance Gi* (self included), as in ArcGIS Hot Spot Analysis."""

    def __init__(self, coords: np.ndarray, distance: float):
        self.neighbors = BallTree(coords).query_radius(coords, r=distance)
        self.wsum = np.array([len(nb) for nb in self.neighbors], dtype=float)
        self.n = len(coords)

    def __call__(self, x: np.ndarray, xbar: float, sd: float):
        """xbar/sd are pooled over all years, so z-scores (and the hot spot extent)
        are comparable from year to year rather than relative to each year alone."""
        n = self.n
        if sd == 0:
            return np.zeros(n), np.ones(n), np.zeros(n, dtype=int)
        lag = np.array([x[nb].sum() for nb in self.neighbors])
        z = (lag - xbar * self.wsum) / (sd * np.sqrt((n * self.wsum - self.wsum ** 2) / (n - 1)))
        p = np.array([math.erfc(abs(v) / math.sqrt(2)) for v in z])
        bins = np.zeros(n, dtype=int)
        for level, alpha in ((1, 0.10), (2, 0.05), (3, 0.01)):
            cut = self.fdr_cutoff(p, alpha) if GI_FDR else alpha
            bins[(p <= cut)] = level
        return z, p, np.sign(z).astype(int) * bins

    @staticmethod
    def fdr_cutoff(p: np.ndarray, alpha: float) -> float:
        ranked = np.sort(p)
        ok = ranked <= alpha * np.arange(1, len(p) + 1) / len(p)
        return ranked[np.nonzero(ok)[0].max()] if ok.any() else -1.0


GI_LABELS = {3: "Hot Spot - 99% Confidence", 2: "Hot Spot - 95% Confidence", 1: "Hot Spot - 90% Confidence",
             0: "Not Significant", -1: "Cold Spot - 90% Confidence", -2: "Cold Spot - 95% Confidence",
             -3: "Cold Spot - 99% Confidence"}


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    print("Building hex grid ...")
    names, geoms = conus_states()
    hexes = hex_grid(names, geoms)
    control_areas = load_control_areas()
    hexes["BA_NAME"] = join_ba(hexes["lat"], hexes["lon"], control_areas)
    print(f"  {len(hexes):,} hexes ({HEX_WIDTH_M / 1000:.0f} km across)")

    print("Demand ...")
    dc_existing = existing_load(hexes)
    dc_new = new_load_2035(hexes)
    print(f"  existing {dc_existing.sum():,.0f} MW in {(dc_existing > 0).sum()} hexes; "
          f"new by 2035 {dc_new.sum():,.0f} MW in {(dc_new > 0).sum()} hexes")

    print("Supply ...")
    lines = load_hv_lines()
    sub_xy = load_osm_substations(sorted(US_SUBSTATIONS_DIR.glob("subs-US-*.json")), TO_HEX)
    print(f"  {len(sub_xy):,} OSM substations >= {MIN_LINE_KV} kV")
    tx, n_lines, max_kv, n_subs = transmission_capacity(hexes, lines, sub_xy)
    print(f"  {len(lines):,} lines >= {MIN_LINE_KV} kV; {(tx > 0).sum():,} hexes reach one")
    gen = local_generation(hexes)
    gi = GiStar(shapely.get_coordinates(shapely.centroid(hexes["geometry"].to_numpy())), GI_DISTANCE_M)

    ba_ts = pd.read_csv(OUT_DIR / "us_ba_time_series_2025_2035.csv").set_index(["BA_NAME", "YEAR"])
    ba_artifact = ba_ts["DATA_ARTIFACT_FLAG"].groupby(level=0).first()

    frames = []
    for year in TS_YEARS:
        load = dc_existing + dc_new * ramp(year)
        strength = gen[year] + tx
        share_mw = allocate_ba_value(hexes, strength, ba_ts["AVAIL_CAP_MW"], year)
        no_cap = np.isnan(share_mw)
        # what runs today is served (the lights are on), so a hex's headroom is at
        # least its existing data-centre load
        headroom = np.where(no_cap, np.nan, np.maximum(np.nan_to_num(share_mw), dc_existing))
        has_dc = load > 0
        distribution = has_dc & (load < MIN_DC_MW_FOR_HV) & (tx <= 0)
        assessed = has_dc & ~distribution & ~no_cap
        with np.errstate(divide="ignore", invalid="ignore"):
            stress = np.where(assessed & (headroom > 0), load / headroom, 0.0)
        stress[assessed & (headroom <= 0)] = np.nan            # new load where the BA has no headroom
        gap = np.where(assessed, np.maximum(load - np.nan_to_num(headroom), 0.0), 0.0)
        capped = np.where(np.isnan(stress), LOCAL_STRESS_CAP, np.minimum(stress, LOCAL_STRESS_CAP))
        artifact = hexes["BA_NAME"].map(ba_artifact).fillna(False).to_numpy(dtype=bool)
        cls = np.select(
            [~has_dc, distribution, no_cap, artifact, np.isnan(stress) | (stress > 1), stress >= 0.9],
            ["no_dc", "distribution_served", "no_capacity_data", "data_artifact", "gap", "near_limit"], "surplus")
        gap[artifact] = 0.0
        stress = np.where(artifact, np.nan, stress)
        capped[artifact | ~assessed] = 0.0
        ba_stress = [ba_ts["STRESS_INDEX"].get((b, year), np.nan) if b else np.nan for b in hexes["BA_NAME"]]
        frames.append(pd.DataFrame({
            "HEX_ID": hexes["HEX_ID"], "YEAR": year, "YEAR_DATE": f"{year}-01-01",
            "STATE": hexes["STATE"], "BA_NAME": hexes["BA_NAME"],
            "DC_LOAD_MW": load, "LOCAL_HEADROOM_MW": headroom, "LOCAL_STRESS": stress, "DC_GAP_MW": gap,
            "LOCAL_CLASS": cls,
            "LOCAL_STRESS_CAPPED": capped, "DATA_ARTIFACT_FLAG": artifact.astype(int),
            "DC_EXISTING_MW": dc_existing, "DC_NEW_MW": dc_new * ramp(year),
            "GRID_STRENGTH_MW": strength, "LOCAL_GEN_MW": gen[year], "TX_CAPACITY_MW": tx,
            "HV_LINES_DELIVERING": n_lines, "HV_SUBSTATIONS": n_subs,
            "BA_HEADROOM_SHARE_MW": share_mw, "BA_STRESS_INDEX": ba_stress,
        }))
    ts = pd.concat(frames, ignore_index=True)

    print("Hot spots of capacity gaps (Gi*, pooled across years) ...")
    pooled = ts["DC_GAP_MW"].to_numpy()
    xbar, sd = pooled.mean(), pooled.std()
    gi_cols = {"GI_Z": [], "GI_P": [], "GI_BIN": [], "GI_PATTERN": []}
    for year in TS_YEARS:
        z, p, gi_bin = gi(ts.loc[ts["YEAR"] == year, "DC_GAP_MW"].to_numpy(), xbar, sd)
        gi_cols["GI_Z"].append(z), gi_cols["GI_P"].append(p), gi_cols["GI_BIN"].append(gi_bin)
        gi_cols["GI_PATTERN"].append(np.array([GI_LABELS[b] for b in gi_bin], dtype=object))
    for col, parts in gi_cols.items():
        ts[col] = np.concatenate(parts)          # frames were built year by year, in TS_YEARS order
    order = ["HEX_ID", "YEAR", "YEAR_DATE", "STATE", "BA_NAME", "DC_LOAD_MW", "LOCAL_HEADROOM_MW",
             "LOCAL_STRESS", "DC_GAP_MW", "LOCAL_CLASS", "GI_Z", "GI_P", "GI_BIN", "GI_PATTERN"]
    ts = ts[order + [c for c in ts.columns if c not in order]]
    ts.to_csv(OUT_DIR / "us_hex_local_stress_2025_2035.csv", index=False, float_format="%.4f")

    wgs = shapely.transform(hexes["geometry"].to_numpy(),
                            lambda xy: np.column_stack(TO_WGS.transform(xy[:, 0], xy[:, 1])))
    features = [{"type": "Feature",
                 "properties": {"HEX_ID": h, "STATE": s, "BA_NAME": b, "TX_CAPACITY_MW": float(t),
                                "HV_LINES": int(n), "MAX_KV": float(k), "HV_SUBSTATIONS": int(ns)},
                 "geometry": json.loads(shapely.to_geojson(shapely.set_precision(g, 1e-5)))}
                for h, s, b, t, n, k, ns, g in zip(hexes["HEX_ID"], hexes["STATE"], hexes["BA_NAME"],
                                                   tx, n_lines, max_kv, n_subs, wgs)]
    with open(OUT_DIR / "us_hex_grid.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    print(f"Wrote us_hex_local_stress_2025_2035.csv ({len(ts):,} rows) and us_hex_grid.geojson")

    for year in (TS_YEARS[0], TS_YEARS[-1]):
        y = ts[ts["YEAR"] == year]
        dc = y[y["DC_LOAD_MW"] > 0]
        print(f"\n{year}: classes of hexes with data-centre load: {dc['LOCAL_CLASS'].value_counts().to_dict()}")
        print(f"  DC gap {y['DC_GAP_MW'].sum():,.0f} MW in {(y['DC_GAP_MW'] > 0).sum()} hexes; "
              f"Gi* hot spot hexes (99/95/90%): {[(y['GI_BIN'] == b).sum() for b in (3, 2, 1)]}")
        hot = y[y["GI_BIN"] > 0]
        if len(hot):
            print("  hot spot hexes by state:", hot["STATE"].value_counts().head(8).to_dict())
        print(dc.sort_values("DC_GAP_MW", ascending=False).head(8)
              [["HEX_ID", "STATE", "BA_NAME", "DC_EXISTING_MW", "DC_LOAD_MW", "LOCAL_HEADROOM_MW",
                "DC_GAP_MW", "LOCAL_CLASS"]].to_string(index=False, float_format=lambda v: f"{v:,.0f}"))


if __name__ == "__main__":
    main()
