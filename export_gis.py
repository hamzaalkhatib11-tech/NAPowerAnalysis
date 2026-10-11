"""
export_gis.py

Joins the time-series tables back to their polygons and writes ArcGIS-ready,
time-enabled layers to output/gis/:

  ggr442_grid_stress.gdb   File Geodatabase (open in ArcGIS Pro, publish to ArcGIS Online)
    us_ba_stress_timeseries        70 HIFLD BAs x 2025-2035 (770 polygons), YEAR_DATE is a Date field
    ca_province_stress_timeseries  6 provinces x 2025-2035 (66 polygons), same schema
    dc_existing_points             deduplicated existing data centres (2025 layer)
    dc_growth_sites_2035           IM3 projected new sites (high growth, 50% gravity)
    us_hex_local_stress_timeseries 15,431 hexes x 2025-2035 (local_stress.py); feed
                                   LOCAL_STRESS_CAPPED to Emerging Hot Spot Analysis
    transmission_115kv_plus        HIFLD lines >= 115 kV with the MW rating used
    ca_hex_local_stress_timeseries Canadian hexes x 2025-2035 (canada_local_stress.py),
                                   same fields as the U.S. hex layer, PROVINCE for BA
    ca_transmission_osm_115kv_plus OpenStreetMap lines >= 115 kV (c) OpenStreetMap
                                   contributors, ODbL -- credit on the map
  us_ba_stress_timeseries.geojson / ca_province_stress_timeseries.geojson
                                   same time layers for a direct ArcGIS Online upload
                                   (set YEAR_DATE as the time field; it is ISO text there)

Polygons are simplified (config.GIS_SIMPLIFY_M) so each year's copy stays light,
then made valid and reprojected to WGS84.

In ArcGIS Pro: Layer Properties > Time > "Each feature has a single time field"
= YEAR_DATE, interval 1 year. Experience Builder's Timeline widget then drives
both time layers together.
"""

import json
import shutil
import zipfile

import numpy as np
import pandas as pd
import pyogrio
import shapefile
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import shape as shapely_shape

from config import (
    CONTROL_AREAS_SHP, GIS_DIR, GIS_SIMPLIFY_M, OUT_DIR, STATCAN_PROVINCES_ZIP,
)
from geo import read_prj, read_prj_from_zip
from config import HEX_CRS
from local_stress import load_hv_lines
from canada_local_stress import TO_WGS as CA_TO_WGS, load_osm_lines

GDB = GIS_DIR / "ggr442_grid_stress.gdb"
WGS84 = "EPSG:4326"
MIN_RING_AREA_M2 = 1e6     # drop islands/holes under 1 km2 after simplification
TIME_FIELDS = ["REGION_NAME", "REGION_ID", "COUNTRY", "YEAR", "YEAR_DATE", "DC_LOAD_MW",
               "TOTAL_CAP_MW", "PEAK_LOAD_MW", "AVAIL_CAP_MW", "STRESS_INDEX", "STRESS_CLASS",
               "DATA_ARTIFACT_FLAG", "ARTIFACT_REASON"]


def clean_polygon(shp, src_crs, tolerance):
    """pyshp shape -> simplified, valid WGS84 (Multi)Polygon.

    Works ring by ring: each ring is simplified on its own (Douglas-Peucker in
    the source CRS's metres), rings under MIN_RING_AREA_M2 are dropped, and the
    rest are combined by even-odd XOR so holes come out right. Simplifying the
    assembled polygon instead took ~100 s for Quebec's 21,000 rings."""
    pts = np.asarray(shp.points)
    bounds = list(shp.parts) + [len(pts)]
    rings = np.array([shapely.polygons(pts[a:b]) for a, b in zip(bounds[:-1], bounds[1:]) if b - a >= 4])
    rings = shapely.make_valid(shapely.simplify(rings, tolerance, preserve_topology=False))
    rings = rings[~shapely.is_empty(rings)]
    areas = shapely.area(rings)
    keep = rings[areas >= MIN_RING_AREA_M2] if (areas >= MIN_RING_AREA_M2).any() else rings[np.argsort(areas)[-1:]]
    geom = shapely.make_valid(shapely.symmetric_difference_all(keep))
    if geom.geom_type == "GeometryCollection":
        geom = shapely.union_all([g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")])
    transformer = Transformer.from_crs(src_crs, WGS84, always_xy=True)
    return shapely.transform(geom, lambda xy: np.column_stack(transformer.transform(xy[:, 0], xy[:, 1])))


def ba_polygons() -> pd.DataFrame:
    reader = shapefile.Reader(str(CONTROL_AREAS_SHP))
    crs = CRS.from_wkt(read_prj(CONTROL_AREAS_SHP.with_suffix(".prj")) or "EPSG:3857")
    fields = [f[0] for f in reader.fields[1:]]
    rows = []
    for sr in reader.iterShapeRecords():
        rec = dict(zip(fields, sr.record))
        rows.append({"REGION_NAME": rec["NAME"],
                     "geometry": clean_polygon(sr.shape, crs, GIS_SIMPLIFY_M)})
    return pd.DataFrame(rows)


def province_polygons(names) -> pd.DataFrame:
    reader = shapefile.Reader(str(STATCAN_PROVINCES_ZIP), encoding="latin1")
    crs = CRS.from_wkt(read_prj_from_zip(STATCAN_PROVINCES_ZIP))
    rows = []
    for i, rec in enumerate(reader.iterRecords(fields=["PRENAME"])):
        if rec["PRENAME"] in names:
            rows.append({"REGION_NAME": rec["PRENAME"],
                         "geometry": clean_polygon(reader.shape(i), crs, GIS_SIMPLIFY_M)})
    return pd.DataFrame(rows)


def us_table() -> pd.DataFrame:
    ts = pd.read_csv(OUT_DIR / "us_ba_time_series_2025_2035.csv")
    ts = ts.rename(columns={"BA_NAME": "REGION_NAME", "BA_ID": "REGION_ID"})
    ts["COUNTRY"] = "United States"
    ts["REGION_ID"] = ts["REGION_ID"].astype(str)
    ts["ARTIFACT_REASON"] = ts["ARTIFACT_REASON"].fillna("")
    return ts


def canada_table() -> pd.DataFrame:
    ca = pd.read_csv(OUT_DIR / "canada_stress_index_timeseries.csv")
    return pd.DataFrame({
        "REGION_NAME": ca["province"],
        "REGION_ID": ca["province"],
        "COUNTRY": "Canada",
        "YEAR": ca["year"],
        "YEAR_DATE": ca["year"].astype(str) + "-01-01",
        "DC_LOAD_MW": ca["dc_load_mw"],
        "TOTAL_CAP_MW": ca["total_cap_mw"],
        "PEAK_LOAD_MW": ca["peak_load_mw"],
        "AVAIL_CAP_MW": ca["avail_cap_mw"],
        "STRESS_INDEX": ca["stress_index"],
        "STRESS_CLASS": np.where(ca["data_artifact_flag"], "data_artifact",
                                 np.where(ca["stress_index"] > 1, "gap",
                                          np.where(ca["stress_index"] >= 0.9, "near_limit", "surplus"))),
        "DATA_ARTIFACT_FLAG": ca["data_artifact_flag"].astype(bool),
        "ARTIFACT_REASON": ca["artifact_reason"].fillna(""),
        "DC_GROWTH_SOURCE": ca["dc_growth_source"],
        "SYSTEM_PEAK_MW": ca["system_peak_mw"],
        "DC_EXISTING_MW": ca["dc_existing_mw"],
        "DC_NEW_MW": ca["dc_new_mw"],
        "DC_LOAD_HIGH_MW": ca["dc_load_high_mw"],
        "STRESS_INDEX_HIGH": ca["stress_index_high"],
    })


def write_layer(df: pd.DataFrame, layer: str, geometry_type: str, geojson: bool = False):
    df = df.reset_index(drop=True)
    geoms = shapely.to_wkb(df["geometry"].to_numpy())
    attrs = df.drop(columns="geometry")
    fields, data = [], []
    for col in attrs.columns:
        s = attrs[col]
        if col == "YEAR_DATE":
            data.append(pd.to_datetime(s).to_numpy(dtype="datetime64[ms]"))
        elif s.dtype == bool:
            data.append(s.astype(np.int16).to_numpy())     # FGDB has no boolean type
        elif pd.api.types.is_numeric_dtype(s):
            # int32: FGDB's Integer type (int64 would be written as Float64)
            data.append(s.to_numpy(dtype=float) if s.dtype.kind == "f" else s.to_numpy(dtype=np.int32))
        else:
            data.append(s.fillna("").astype(str).to_numpy(dtype=object))
        fields.append(col)
    pyogrio.raw.write(str(GDB), geoms, data, fields, layer=layer, driver="OpenFileGDB",
                      geometry_type=geometry_type, crs=WGS84, promote_to_multi=geometry_type.startswith("Multi"))
    print(f"  {layer}: {len(df)} features")

    if geojson:
        features = []
        for geom, (_, row) in zip(df["geometry"], attrs.iterrows()):
            props = {k: (None if isinstance(v, float) and np.isnan(v) else
                         bool(v) if isinstance(v, (bool, np.bool_)) else
                         v.item() if hasattr(v, "item") else v) for k, v in row.items()}
            features.append({"type": "Feature", "properties": props,
                             "geometry": json.loads(shapely.to_geojson(shapely.set_precision(geom, 1e-5)))})
        path = GIS_DIR / f"{layer}.geojson"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f)
        print(f"  {path.name}: {path.stat().st_size / 1e6:.1f} MB")


def main():
    GIS_DIR.mkdir(parents=True, exist_ok=True)
    if GDB.exists():
        # check for a lock before deleting anything, so a failed run can't leave a half-deleted .gdb
        locks = list(GDB.glob("*.lock"))
        if locks:
            raise SystemExit(f"{GDB.name} is open in ArcGIS (lock files: {', '.join(l.name for l in locks[:3])}). "
                             "Close the project or remove its layers from the map, then re-run export_gis.py.")
        shutil.rmtree(GDB)

    print("Building polygons ...")
    us = us_table()
    ba_geom = ba_polygons()
    us = us.merge(ba_geom, on="REGION_NAME", how="left")
    missing = us["geometry"].isna().sum()
    assert missing == 0, f"{missing} time-series rows have no BA polygon"

    ca = canada_table()
    ca = ca.merge(province_polygons(set(ca["REGION_NAME"])), on="REGION_NAME", how="left")
    assert ca["geometry"].notna().all(), "province polygon missing"

    print(f"Writing {GDB.name} ...")
    write_layer(us[TIME_FIELDS + [c for c in us.columns if c not in TIME_FIELDS]],
                "us_ba_stress_timeseries", "MultiPolygon", geojson=True)
    write_layer(ca[TIME_FIELDS + [c for c in ca.columns if c not in TIME_FIELDS]],
                "ca_province_stress_timeseries", "MultiPolygon", geojson=True)

    pts = pd.read_csv(OUT_DIR / "harmonized_existing_dc_with_ba.csv")
    ca_mw = pd.read_csv(OUT_DIR / "canada_dc_with_mw.csv")[["name", "lat", "lon", "mw_estimate", "mw_method"]]
    pts = pts.merge(ca_mw, on=["name", "lat", "lon"], how="left", suffixes=("", "_ca"))
    has_ca = pts["mw_estimate_ca"].notna()
    pts.loc[has_ca, "mw_estimate"] = pts.loc[has_ca, "mw_estimate_ca"]
    pts.loc[has_ca, "mw_method"] = pts.loc[has_ca, "mw_method_ca"]
    pts = pts.drop(columns=["mw_estimate_ca", "mw_method_ca"])
    pts["geometry"] = shapely.points(pts["lon"], pts["lat"])
    write_layer(pts, "dc_existing_points", "Point")

    with open(OUT_DIR / "growth_2035_wgs84.geojson", encoding="utf-8") as f:
        gj = json.load(f)
    growth = pd.DataFrame([feat["properties"] for feat in gj["features"]])
    growth["geometry"] = [shapely_shape(feat["geometry"]) for feat in gj["features"]]
    write_layer(growth, "dc_growth_sites_2035", "MultiPolygon")

    print("Hex layers ...")
    with open(OUT_DIR / "us_hex_grid.geojson", encoding="utf-8") as f:
        hex_feats = json.load(f)["features"]
    hex_geom = pd.DataFrame({"HEX_ID": [ft["properties"]["HEX_ID"] for ft in hex_feats],
                             "geometry": [shapely_shape(ft["geometry"]) for ft in hex_feats]})
    hx = pd.read_csv(OUT_DIR / "us_hex_local_stress_2025_2035.csv").merge(hex_geom, on="HEX_ID", how="left")
    assert hx["geometry"].notna().all(), "hex polygon missing"
    hx["BA_NAME"] = hx["BA_NAME"].fillna("")
    write_layer(hx, "us_hex_local_stress_timeseries", "Polygon")

    lines = load_hv_lines()
    to_wgs = Transformer.from_crs(HEX_CRS, WGS84, always_xy=True)
    geoms = shapely.simplify(lines["geometry"].to_numpy(), 100.0)
    lines["geometry"] = shapely.transform(geoms, lambda xy: np.column_stack(to_wgs.transform(xy[:, 0], xy[:, 1])))
    write_layer(lines[["kv", "rating_mw", "VOLT_CLASS", "STATUS", "OWNER", "geometry"]]
                .rename(columns={"kv": "VOLTAGE_KV", "rating_mw": "RATING_MW"}),
                "transmission_115kv_plus", "MultiLineString")

    print("Canadian hex layers ...")
    with open(OUT_DIR / "ca_hex_grid.geojson", encoding="utf-8") as f:
        ca_feats = json.load(f)["features"]
    ca_geom = pd.DataFrame({"HEX_ID": [ft["properties"]["HEX_ID"] for ft in ca_feats],
                            "geometry": [shapely_shape(ft["geometry"]) for ft in ca_feats]})
    cx = pd.read_csv(OUT_DIR / "ca_hex_local_stress_2025_2035.csv").merge(ca_geom, on="HEX_ID", how="left")
    assert cx["geometry"].notna().all(), "Canadian hex polygon missing"
    write_layer(cx, "ca_hex_local_stress_timeseries", "Polygon")

    osm = load_osm_lines()
    osm["geometry"] = shapely.transform(shapely.simplify(osm["geometry"].to_numpy(), 100.0),
                                        lambda xy: np.column_stack(CA_TO_WGS.transform(xy[:, 0], xy[:, 1])))
    write_layer(osm[["osm_id", "kv", "rating_mw", "geometry"]]
                .rename(columns={"osm_id": "OSM_ID", "kv": "VOLTAGE_KV", "rating_mw": "RATING_MW"}),
                "ca_transmission_osm_115kv_plus", "MultiLineString")

    # zip the .gdb so it can be uploaded to ArcGIS Online as a single file
    archive = shutil.make_archive(str(GIS_DIR / "ggr442_grid_stress.gdb"), "zip", GIS_DIR, GDB.name)
    print(f"Zipped for upload: {archive}")


if __name__ == "__main__":
    main()
