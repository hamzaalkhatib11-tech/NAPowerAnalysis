"""
geo.py

Point-in-polygon helpers shared by the pipeline scripts (pyshp + shapely; no
GeoPandas dependency).
"""

import zipfile

import numpy as np
import pandas as pd
import shapefile
import shapely
from pyproj import CRS, Transformer

from config import (
    CONTROL_AREAS_FALLBACK_CRS, CONTROL_AREAS_SHP, HIFLD_NULL, STATCAN_PROVINCES_ZIP,
    TIGER_STATES_ZIP,
)


def read_polygon_layer(reader: shapefile.Reader, prj_wkt: str | None, fallback_crs: str | None = None):
    """Returns (records DataFrame, rings, CRS). rings = (ring polygons, owning
    record index). Rings are kept separate rather than assembled into (often
    invalid) polygons: a point is inside a record when it falls in an odd number
    of that record's rings, which handles holes and needs no make_valid pass --
    too slow on the 16M-vertex StatCan layer."""
    fields = [f[0] for f in reader.fields[1:]]
    records, ring_geoms, ring_owner = [], [], []
    for i, sr in enumerate(reader.iterShapeRecords()):
        records.append(dict(zip(fields, sr.record)))
        pts = np.asarray(sr.shape.points)
        bounds = list(sr.shape.parts) + [len(pts)]
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b - a >= 4:
                ring_geoms.append(shapely.polygons(pts[a:b]))
                ring_owner.append(i)
    crs = CRS.from_wkt(prj_wkt) if prj_wkt else CRS.from_user_input(fallback_crs)
    return pd.DataFrame(records), (ring_geoms, np.asarray(ring_owner)), crs


def read_prj(path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def read_prj_from_zip(zip_path) -> str:
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".prj"))
        return z.read(name).decode("latin1")


def first_polygon_match(lats, lons, rings, crs) -> np.ndarray:
    """Index of the first record (in file order) containing each point, or -1."""
    ring_geoms, ring_owner = rings
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = transformer.transform(np.asarray(lons, dtype=float), np.asarray(lats, dtype=float))
    points = shapely.points(xs, ys)
    pt_idx, ring_idx = shapely.STRtree(ring_geoms).query(points, predicate="intersects")
    hits = pd.DataFrame({"pt": pt_idx, "owner": ring_owner[ring_idx]})
    parity = hits.groupby(["pt", "owner"]).size()
    inside = parity[parity % 2 == 1].reset_index()
    first = inside.groupby("pt")["owner"].min()
    result = np.full(len(points), -1)
    result[first.index.to_numpy()] = first.to_numpy()
    return result


def load_control_areas():
    """HIFLD Balancing Authority polygons with -999999 converted to NaN."""
    reader = shapefile.Reader(str(CONTROL_AREAS_SHP))
    prj = read_prj(CONTROL_AREAS_SHP.with_suffix(".prj"))
    recs, rings, crs = read_polygon_layer(reader, prj, CONTROL_AREAS_FALLBACK_CRS)
    for col in ["TOTAL_CAP", "PEAK_LOAD", "MIN_LOAD", "AVAIL_CAP", "YEAR"]:
        recs[col] = pd.to_numeric(recs[col], errors="coerce").replace(HIFLD_NULL, np.nan)
    return recs, rings, crs


def join_ba(lats, lons, control_areas) -> np.ndarray:
    recs, rings, crs = control_areas
    idx = first_polygon_match(lats, lons, rings, crs)
    return np.where(idx >= 0, recs["NAME"].to_numpy()[idx], None)


def join_state(lats, lons) -> np.ndarray:
    recs, rings, crs = read_polygon_layer(
        shapefile.Reader(str(TIGER_STATES_ZIP)), read_prj_from_zip(TIGER_STATES_ZIP))
    idx = first_polygon_match(lats, lons, rings, crs)
    return np.where(idx >= 0, recs["NAME"].to_numpy()[idx], None)


def join_province(lats, lons) -> np.ndarray:
    recs, rings, crs = read_polygon_layer(
        shapefile.Reader(str(STATCAN_PROVINCES_ZIP), encoding="latin1"),
        read_prj_from_zip(STATCAN_PROVINCES_ZIP))
    idx = first_polygon_match(lats, lons, rings, crs)
    return np.where(idx >= 0, recs["PRENAME"].to_numpy()[idx], None)
