"""
canada_local_stress.py

Local capacity gaps and yearly hot spots on a ~25 km hexagon grid for the six
provinces with a provincial stress series (ON, QC, AB, BC, NB, NS), 2025-2035.
Same method as local_stress.py (U.S.), with Canadian inputs:

  PROVINCE headroom  AVAIL_CAP_MW from canada_stress.py (CER capacity - baseline
                     peak), shared across the province's hexes by grid strength
  LOCAL_HEADROOM_MW  max(share, existing DC load) -- today's load is served
  LOCAL_STRESS       DC_LOAD_MW / LOCAL_HEADROOM_MW   (> 1 = local gap)
  DC_GAP_MW          max(DC_LOAD_MW - LOCAL_HEADROOM_MW, 0)
  Gi* hot spots      on DC_GAP_MW, pooled across years (as in the U.S. tier)

  DC_LOAD_MW         existing points (building-footprint MW, scaled up to the
                     provincial floor where one exists) + new load from
                     canada_stress.py's reference path, placed at named sites
                     (Alberta's Meta and Keephills projects) or, otherwise, at
                     the province's existing data-centre points in proportion
                     to their MW
  GRID_STRENGTH_MW   NRCan NACEI power plants (>= 100 MW, and renewables >= 1 MW;
                     static, 2013-2021 vintage) + OpenStreetMap lines >= 115 kV
                     crossing the hex boundary, rated with the same FERC-data
                     voltage fit as the U.S. (Athari & Wang 2017)

Coverage notes: Nova Scotia has no OSM line data (the Overpass server refused
the download), so its grid strength is plants only (OSM_LINES = 0). No Canadian
equivalent of IM3 exists, so new load follows announced sites or today's
clusters rather than modelled new sites.

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

from config import (
    CA_HEX_CRS, CANADA_DC_EXISTING_FLOOR_MW, CANADA_DC_NEW_REF_MW, CANADA_DC_SITES,
    CANADA_PEAK_LOAD_2025_MW, GI_DISTANCE_M, LOCAL_STRESS_CAP, MIN_DC_MW_FOR_HV, MIN_LINE_KV,
    NACEI_PLANT_FILES, OSM_LINES_DIR, OUT_DIR, STATCAN_PROVINCES_ZIP, TS_YEARS,
)
from geo import read_prj_from_zip
from local_stress import GI_LABELS, GiStar, assign_to_hex, hex_grid, line_rating

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


def demand(hexes, pts) -> tuple[np.ndarray, dict]:
    """Existing MW per hex, and {year: new MW per hex}."""
    idx = assign_to_hex(pts["lon"], pts["lat"], hexes, TO_HEX)
    ok = idx >= 0
    existing = np.bincount(idx[ok], weights=pts["mw"].to_numpy()[ok], minlength=len(hexes))

    sites = pd.DataFrame(CANADA_DC_SITES)
    site_idx = assign_to_hex(sites["lon"], sites["lat"], hexes, TO_HEX)
    new = {}
    for year in TS_YEARS:
        arr = np.zeros(len(hexes))
        for prov, table in CANADA_DC_NEW_REF_MW.items():
            mw = float(table.get(year, 0.0))
            if mw <= 0:
                continue
            placed = 0.0
            for (_, site), h in zip(sites[sites["province"] == prov].iterrows(),
                                    site_idx[(sites["province"] == prov).to_numpy()]):
                arr[h] += mw * site["share_of_new"]
                placed += site["share_of_new"]
            rest = mw * (1 - placed)
            in_prov = ok & (pts["joined_province"] == prov).to_numpy()
            w = pts["mw"].to_numpy()[in_prov]
            if rest > 0 and w.sum() > 0:
                arr += np.bincount(idx[in_prov], weights=rest * w / w.sum(), minlength=len(hexes))
        new[year] = arr
    return existing, new


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


def osm_kv(voltage: str | None) -> float:
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", voltage or "")]
    return max(nums) / 1000 if nums else 0.0


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


def transmission_capacity(hexes, lines) -> np.ndarray:
    boundaries = shapely.boundary(hexes["geometry"].to_numpy())
    h_idx, l_idx = shapely.STRtree(lines["geometry"].to_numpy()).query(boundaries, predicate="intersects")
    return np.bincount(h_idx, weights=lines["rating_mw"].to_numpy()[l_idx], minlength=len(hexes))


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    print("Building Canadian hex grid ...")
    names, geoms = province_shapes()
    hexes = hex_grid(names, geoms, TO_WGS).rename(columns={"STATE": "PROVINCE"})
    hexes["HEX_ID"] = hexes["HEX_ID"].str.replace("H", "C", n=1)
    print(f"  {len(hexes):,} hexes across {len(names)} provinces")

    print("Demand ...")
    pts = dc_points()
    existing, new = demand(hexes, pts)
    print(f"  existing {existing.sum():,.0f} MW; new by 2035 {new[TS_YEARS[-1]].sum():,.0f} MW")

    print("Supply ...")
    plants = plant_capacity(hexes)
    lines = load_osm_lines()
    tx = transmission_capacity(hexes, lines)
    print(f"  {len(lines):,} OSM lines >= {MIN_LINE_KV} kV; {(tx > 0).sum():,} hexes reach one")
    strength = plants + tx

    prov_ts = pd.read_csv(OUT_DIR / "canada_stress_index_timeseries.csv").set_index(["province", "year"])
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
        cls = np.select([~has_dc, distribution, np.isnan(stress) | (stress > 1), stress >= 0.9],
                        ["no_dc", "distribution_served", "gap", "near_limit"], "surplus")
        prov_stress = np.array([prov_ts["stress_index"].get((p, year), np.nan) for p in prov])
        frames.append(pd.DataFrame({
            "HEX_ID": hexes["HEX_ID"], "YEAR": year, "YEAR_DATE": f"{year}-01-01", "PROVINCE": prov,
            "DC_LOAD_MW": load, "LOCAL_HEADROOM_MW": headroom, "LOCAL_STRESS": stress, "DC_GAP_MW": gap,
            "LOCAL_CLASS": cls, "LOCAL_STRESS_CAPPED": capped,
            "DC_EXISTING_MW": existing, "DC_NEW_MW": new[year],
            "GRID_STRENGTH_MW": strength, "PLANT_MW": plants, "TX_CAPACITY_MW": tx,
            "PROVINCE_HEADROOM_SHARE_MW": share_mw, "PROVINCE_STRESS_INDEX": prov_stress,
            "OSM_LINES": (prov != "Nova Scotia").astype(int),
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
