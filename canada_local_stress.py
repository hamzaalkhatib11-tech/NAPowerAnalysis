"""
canada_local_stress.py

Local capacity gaps and yearly hot spots on a ~25 km hexagon grid for the nine
provinces with a provincial stress series (ON, QC, AB, BC, MB, SK, NB, NS, NL),
2025-2035. Same method as local_stress.py (U.S.), with Canadian inputs:

  PROVINCE headroom  AVAIL_CAP_MW from canada_stress.py (CER capacity - baseline
                     peak), shared across the province's hexes by grid strength
  LOCAL_HEADROOM_MW  max(share, existing DC load) -- today's load is served
  LOCAL_STRESS       DC_LOAD_MW / LOCAL_HEADROOM_MW   (> 1 = local gap)
  DC_GAP_MW          max(DC_LOAD_MW - LOCAL_HEADROOM_MW, 0)
  Gi* hot spots      on DC_GAP_MW, pooled across years (as in the U.S. tier)

  DC_LOAD_MW         existing points (building-footprint MW, scaled up to the
                     provincial floor where one exists) + each province's new
                     load from canada_stress.py (dc_new_mw), placed at named
                     sites (Alberta's Meta and Keephills projects) and otherwise
                     by the siting model below
  GRID_STRENGTH_MW   NRCan NACEI power plants (>= 100 MW, and renewables >= 1 MW;
                     static, 2013-2021 vintage) + OpenStreetMap lines >= 115 kV
                     that cross into the hex AND reach a >= 115 kV substation
                     inside it (pass-through lines don't count), rated with the
                     same FERC-data voltage fit as the U.S. (Athari & Wang 2017)

Siting model (stand-in for IM3, which covers only the U.S.): eligible hexes
hold an OSM substation >= 115 kV; SITING_SCORE = CA_SITING_GRAVITY_WEIGHT x
market gravity (population of OSM cities/towns within 100 km) + the rest x grid
strength, each log-scaled to 0-1 within the province. A province's unsited new
load goes to its CA_SITING_TOP_K best hexes in proportion to score.

Newfoundland and Labrador is flagged as a data artifact (CER capacity includes
Churchill Falls output contracted to Hydro-Quebec).

Writes to output/:
  ca_hex_local_stress_2025_2035.csv   one row per hex per year
  ca_hex_grid.geojson                 hex polygons (WGS84) + static attributes
"""
import json
import re

import numpy as np
import pandas as pd
import shapefile
import shapely
from pyproj import Transformer

from sklearn.neighbors import BallTree

from config import (
    CA_HEX_CRS, CA_SITING_GRAVITY_RADIUS_M, CA_SITING_GRAVITY_WEIGHT, CA_SITING_TOP_K,
    CANADA_ARTIFACT_PROVINCES, CANADA_DC_EXISTING_FLOOR_MW, CANADA_DC_SITES, PROVINCE_ABBREV,
    CANADA_PEAK_LOAD_2025_MW, GI_DISTANCE_M, LOCAL_STRESS_CAP, MIN_DC_MW_FOR_HV, MIN_LINE_KV,
    NACEI_PLANT_FILES, OSM_LINES_DIR, OUT_DIR, STATCAN_PROVINCES_ZIP, TS_YEARS,
)
from geo import read_prj_from_zip
from local_stress import (
    GI_LABELS, GiStar, assign_to_hex, hex_grid, line_rating, load_osm_substations, osm_kv,
    transmission_capacity,
)

WGS84 = "EPSG:4326"
TO_HEX = Transformer.from_crs(WGS84, CA_HEX_CRS, always_xy=True)
TO_WGS = Transformer.from_crs(CA_HEX_CRS, WGS84, always_xy=True)
PROVINCES = list(CANADA_PEAK_LOAD_2025_MW)
SIMPLIFY_M = 1000.0      # province outlines only decide hex membership


def province_shapes():
    reader = shapefile.Reader(str(STATCAN_PROVINCES_ZIP), encoding="latin1")
    src = Transformer.from_crs(read_prj_from_zip(STATCAN_PROVINCES_ZIP), CA_HEX_CRS, always_xy=True)
    names, geoms = [], []
    for i, rec in enumerate(reader.iterRecords(fields=["PRENAME"])):
        if rec["PRENAME"] not in PROVINCES:
            continue
        shp = reader.shape(i)
        pts = np.asarray(shp.points)
        bounds = list(shp.parts) + [len(pts)]
        rings = np.array([shapely.polygons(pts[a:b]) for a, b in zip(bounds[:-1], bounds[1:]) if b - a >= 4])
        rings = shapely.make_valid(shapely.simplify(rings, SIMPLIFY_M, preserve_topology=False))
        rings = rings[~shapely.is_empty(rings) & (shapely.area(rings) >= 1e6)]
        geom = shapely.make_valid(shapely.symmetric_difference_all(rings))
        names.append(rec["PRENAME"])
        geoms.append(shapely.transform(geom, lambda xy: np.column_stack(src.transform(xy[:, 0], xy[:, 1]))))
    return names, np.array(geoms)


# ----------------------------------------------------------------------------
# Demand
# ----------------------------------------------------------------------------
def dc_points() -> pd.DataFrame:
    """Existing Canadian points with MW scaled to the provincial floor where it's higher."""
    pts = pd.read_csv(OUT_DIR / "canada_dc_with_mw.csv")
    pts = pts[pts["joined_province"].isin(PROVINCES)].copy()
    pts["mw"] = pts["mw_estimate"].fillna(0.0)
    totals = pts.groupby("joined_province")["mw"].sum()
    for prov, floor in CANADA_DC_EXISTING_FLOOR_MW.items():
        if totals.get(prov, 0) > 0 and floor > totals[prov]:
            pts.loc[pts["joined_province"] == prov, "mw"] *= floor / totals[prov]
    return pts


def demand(hexes, pts, prov_ts: pd.DataFrame, siting_weights: dict) -> tuple[np.ndarray, dict]:
    """Existing MW per hex, and {year: new MW per hex}.

    New MW per province comes from canada_stress.py (one source of truth); named
    sites take their share, the rest goes to the province's siting-model hexes
    (or, if a province has none, to its existing data-centre points)."""
    idx = assign_to_hex(pts["lon"], pts["lat"], hexes, TO_HEX)
    ok = idx >= 0
    existing = np.bincount(idx[ok], weights=pts["mw"].to_numpy()[ok], minlength=len(hexes))

    sites = pd.DataFrame(CANADA_DC_SITES)
    site_idx = assign_to_hex(sites["lon"], sites["lat"], hexes, TO_HEX)
    new = {}
    for year in TS_YEARS:
        arr = np.zeros(len(hexes))
        for prov in PROVINCES:
            mw = float(prov_ts["dc_new_mw"].get((prov, year), 0.0))
            if mw <= 0:
                continue
            in_prov_sites = (sites["province"] == prov).to_numpy()
            placed = 0.0
            for share, h in zip(sites.loc[in_prov_sites, "share_of_new"], site_idx[in_prov_sites]):
                arr[h] += mw * share
                placed += share
            rest = mw * (1 - placed)
            if rest <= 0:
                continue
            if prov in siting_weights:
                hexes_w = siting_weights[prov]
                arr[hexes_w.index.to_numpy()] += rest * hexes_w.to_numpy()
            else:
                in_prov = ok & (pts["joined_province"] == prov).to_numpy()
                w = pts["mw"].to_numpy()[in_prov]
                if w.sum() > 0:
                    arr += np.bincount(idx[in_prov], weights=rest * w / w.sum(), minlength=len(hexes))
        new[year] = arr
    return existing, new


# ----------------------------------------------------------------------------
# Siting model (stand-in for IM3)
# ----------------------------------------------------------------------------
def market_gravity(hexes) -> np.ndarray:
    """Population of OSM cities/towns within CA_SITING_GRAVITY_RADIUS_M of each hex centre."""
    with open(OSM_LINES_DIR / "places-CA.json", encoding="utf-8") as f:
        places = json.load(f).get("elements", [])
    pop = []
    for e in places:
        digits = re.sub(r"[^0-9]", "", str(e.get("tags", {}).get("population", "")))
        if digits and "lat" in e:
            pop.append((e["lon"], e["lat"], float(digits)))
    pop = np.array(pop)
    x, y = TO_HEX.transform(pop[:, 0], pop[:, 1])
    tree = BallTree(np.column_stack([x, y]))
    centres = shapely.get_coordinates(shapely.centroid(hexes["geometry"].to_numpy()))
    neighbours = tree.query_radius(centres, r=CA_SITING_GRAVITY_RADIUS_M)
    print(f"  {len(pop):,} OSM cities/towns with population ({pop[:, 2].sum() / 1e6:.1f} M people)")
    return np.array([pop[nb, 2].sum() for nb in neighbours])


def siting_model(hexes, strength, substations, gravity) -> tuple[np.ndarray, dict]:
    """SITING_SCORE per hex (0 where ineligible) and {province: weights over its top-K hexes}."""
    score = np.zeros(len(hexes))
    weights = {}
    prov = hexes["PROVINCE"].to_numpy()

    def scaled(v):
        v = np.log1p(v)
        span = v.max() - v.min()
        return (v - v.min()) / span if span > 0 else np.zeros_like(v)

    for p in PROVINCES:
        eligible = np.where((prov == p) & (substations > 0))[0]
        if len(eligible) == 0:
            continue
        sc = (CA_SITING_GRAVITY_WEIGHT * scaled(gravity[eligible])
              + (1 - CA_SITING_GRAVITY_WEIGHT) * scaled(strength[eligible]))
        score[eligible] = sc
        top = eligible[np.argsort(-sc)[:CA_SITING_TOP_K]]
        w = score[top] / score[top].sum() if score[top].sum() > 0 else np.full(len(top), 1 / len(top))
        weights[p] = pd.Series(w, index=top)
    return score, weights


# ----------------------------------------------------------------------------
# Supply
# ----------------------------------------------------------------------------
def plant_capacity(hexes) -> np.ndarray:
    frames = []
    for path in NACEI_PLANT_FILES:
        with open(path, encoding="utf-8") as f:
            frames.append(pd.DataFrame([ft["properties"] for ft in json.load(f)["features"]]))
    plants = pd.concat(frames, ignore_index=True)
    # the renewables layer repeats >= 100 MW renewable plants from the large-plant layer
    plants["key"] = plants["Latitude"].round(3).astype(str) + plants["Longitude"].round(3).astype(str)
    plants = plants.sort_values("Total_MW", ascending=False).drop_duplicates("key")
    idx = assign_to_hex(plants["Longitude"], plants["Latitude"], hexes, TO_HEX)
    ok = idx >= 0
    print(f"  {len(plants)} NACEI plants, {plants.loc[ok, 'Total_MW'].sum():,.0f} MW inside the grid")
    return np.bincount(idx[ok], weights=plants["Total_MW"].to_numpy()[ok], minlength=len(hexes))


def load_osm_lines() -> pd.DataFrame:
    rows = []
    for path in sorted(OSM_LINES_DIR.glob("CA-*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                ways = json.load(f).get("elements", [])
        except (json.JSONDecodeError, OSError):
            print(f"  WARNING: {path.name} unreadable -- skipped")
            continue
        for w in ways:
            kv = osm_kv(w.get("tags", {}).get("voltage"))
            if kv < MIN_LINE_KV or len(w.get("geometry", [])) < 2:
                continue
            lon = np.array([p["lon"] for p in w["geometry"]])
            lat = np.array([p["lat"] for p in w["geometry"]])
            x, y = TO_HEX.transform(lon, lat)
            rows.append({"osm_id": w["id"], "kv": kv, "rating_mw": line_rating(kv),
                         "geometry": shapely.linestrings(np.column_stack([x, y]))})
    lines = pd.DataFrame(rows).drop_duplicates("osm_id")   # ways on a provincial border appear twice
    return lines.reset_index(drop=True)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    print("Building Canadian hex grid ...")
    names, geoms = province_shapes()
    hexes = hex_grid(names, geoms, TO_WGS).rename(columns={"STATE": "PROVINCE"})
    hexes["HEX_ID"] = hexes["HEX_ID"].str.replace("H", "C", n=1)
    print(f"  {len(hexes):,} hexes across {len(names)} provinces")

    print("Supply ...")
    plants = plant_capacity(hexes)
    lines = load_osm_lines()
    sub_xy = load_osm_substations(sorted(OSM_LINES_DIR.glob("subs-CA-*.json")), TO_HEX)
    print(f"  {len(sub_xy):,} OSM substations >= {MIN_LINE_KV} kV")
    tx, _, _, substations = transmission_capacity(hexes, lines, sub_xy)
    print(f"  {len(lines):,} OSM lines >= {MIN_LINE_KV} kV; {(tx > 0).sum():,} hexes reach one")
    strength = plants + tx

    print("Siting model ...")
    gravity = market_gravity(hexes)
    siting_score, siting_weights = siting_model(hexes, strength, substations, gravity)
    for p, w in siting_weights.items():
        print(f"  {p}: top sites {hexes.loc[w.index[:3], 'HEX_ID'].tolist()}")

    print("Demand ...")
    prov_ts = pd.read_csv(OUT_DIR / "canada_stress_index_timeseries.csv").set_index(["province", "year"])
    pts = dc_points()
    existing, new = demand(hexes, pts, prov_ts, siting_weights)
    print(f"  existing {existing.sum():,.0f} MW; new by 2035 {new[TS_YEARS[-1]].sum():,.0f} MW")
    with_lines = {p for p in PROVINCES if (OSM_LINES_DIR / f"CA-{PROVINCE_ABBREV[p]}.json").exists()}
    prov = hexes["PROVINCE"].to_numpy()
    total_w = pd.Series(strength).groupby(prov).transform("sum").to_numpy()
    share = np.where(total_w > 0, strength / total_w, 0.0)
    gi = GiStar(shapely.get_coordinates(shapely.centroid(hexes["geometry"].to_numpy())), GI_DISTANCE_M)

    frames = []
    for year in TS_YEARS:
        load = existing + new[year]
        avail = np.array([prov_ts["avail_cap_mw"].get((p, year), np.nan) for p in prov])
        share_mw = avail * share
        headroom = np.maximum(np.nan_to_num(share_mw), existing)
        has_dc = load > 0
        distribution = has_dc & (load < MIN_DC_MW_FOR_HV) & (tx <= 0)
        assessed = has_dc & ~distribution
        with np.errstate(divide="ignore", invalid="ignore"):
            stress = np.where(assessed & (headroom > 0), load / headroom, 0.0)
        stress[assessed & (headroom <= 0)] = np.nan
        gap = np.where(assessed, np.maximum(load - headroom, 0.0), 0.0)
        capped = np.where(np.isnan(stress), LOCAL_STRESS_CAP, np.minimum(stress, LOCAL_STRESS_CAP))
        capped[~assessed] = 0.0
        artifact = np.isin(prov, list(CANADA_ARTIFACT_PROVINCES))
        cls = np.select([~has_dc, distribution, artifact, np.isnan(stress) | (stress > 1), stress >= 0.9],
                        ["no_dc", "distribution_served", "data_artifact", "gap", "near_limit"], "surplus")
        gap[artifact] = 0.0
        capped[artifact] = 0.0
        prov_stress = np.array([prov_ts["stress_index"].get((p, year), np.nan) for p in prov])
        frames.append(pd.DataFrame({
            "HEX_ID": hexes["HEX_ID"], "YEAR": year, "YEAR_DATE": f"{year}-01-01", "PROVINCE": prov,
            "DC_LOAD_MW": load, "LOCAL_HEADROOM_MW": headroom, "LOCAL_STRESS": stress, "DC_GAP_MW": gap,
            "LOCAL_CLASS": cls, "LOCAL_STRESS_CAPPED": capped,
            "DC_EXISTING_MW": existing, "DC_NEW_MW": new[year],
            "GRID_STRENGTH_MW": strength, "PLANT_MW": plants, "TX_CAPACITY_MW": tx,
            "PROVINCE_HEADROOM_SHARE_MW": share_mw, "PROVINCE_STRESS_INDEX": prov_stress,
            "OSM_LINES": np.isin(prov, list(with_lines)).astype(int),
            "DATA_ARTIFACT_FLAG": artifact.astype(int),
            "SITING_SCORE": siting_score, "HV_SUBSTATIONS": substations, "POP_100KM": gravity,
        }))
    ts = pd.concat(frames, ignore_index=True)

    pooled = ts["DC_GAP_MW"].to_numpy()
    xbar, sd = pooled.mean(), pooled.std()
    parts = {"GI_Z": [], "GI_P": [], "GI_BIN": [], "GI_PATTERN": []}
    for year in TS_YEARS:
        z, p, b = gi(ts.loc[ts["YEAR"] == year, "DC_GAP_MW"].to_numpy(), xbar, sd)
        parts["GI_Z"].append(z), parts["GI_P"].append(p), parts["GI_BIN"].append(b)
        parts["GI_PATTERN"].append(np.array([GI_LABELS[v] for v in b], dtype=object))
    for col, chunks in parts.items():
        ts[col] = np.concatenate(chunks)
    order = ["HEX_ID", "YEAR", "YEAR_DATE", "PROVINCE", "DC_LOAD_MW", "LOCAL_HEADROOM_MW", "LOCAL_STRESS",
             "DC_GAP_MW", "LOCAL_CLASS", "GI_Z", "GI_P", "GI_BIN", "GI_PATTERN"]
    ts = ts[order + [c for c in ts.columns if c not in order]]
    ts.to_csv(OUT_DIR / "ca_hex_local_stress_2025_2035.csv", index=False, float_format="%.4f")

    wgs = shapely.transform(hexes["geometry"].to_numpy(),
                            lambda xy: np.column_stack(TO_WGS.transform(xy[:, 0], xy[:, 1])))
    features = [{"type": "Feature",
                 "properties": {"HEX_ID": h, "PROVINCE": p, "PLANT_MW": float(pl), "TX_CAPACITY_MW": float(t)},
                 "geometry": json.loads(shapely.to_geojson(shapely.set_precision(g, 1e-5)))}
                for h, p, pl, t, g in zip(hexes["HEX_ID"], hexes["PROVINCE"], plants, tx, wgs)]
    with open(OUT_DIR / "ca_hex_grid.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    print(f"Wrote ca_hex_local_stress_2025_2035.csv ({len(ts):,} rows) and ca_hex_grid.geojson")

    for year in (TS_YEARS[0], 2030, TS_YEARS[-1]):
        y = ts[ts["YEAR"] == year]
        dc = y[y["DC_LOAD_MW"] > 0]
        print(f"\n{year}: {dc['LOCAL_CLASS'].value_counts().to_dict()}; gap {y['DC_GAP_MW'].sum():,.0f} MW; "
              f"hot spot hexes {(y['GI_BIN'] > 0).sum()} {y.loc[y['GI_BIN'] > 0, 'PROVINCE'].value_counts().to_dict()}")
        print(dc.sort_values("DC_GAP_MW", ascending=False).head(6)
              [["HEX_ID", "PROVINCE", "DC_EXISTING_MW", "DC_LOAD_MW", "LOCAL_HEADROOM_MW", "DC_GAP_MW", "LOCAL_CLASS"]]
              .to_string(index=False, float_format=lambda v: f"{v:,.0f}"))


if __name__ == "__main__":
    main()
