"""
config.py

Shared paths, assumptions and small helpers for the GGR442 pipeline.
Every script imports from here so a parameter only ever changes in one place.
"""

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"
REFERENCE_DIR = ROOT / "reference_outputs"

# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------
DATACENTERS_GEOJSON = DATA_DIR / "datacenters.geojson"
DATACENTERS_KML = DATA_DIR / "datacenters.kml"      # fallback: same 6,131 points, no country field
IM3_CSV = DATA_DIR / "im3_open_source_data_center_atlas.csv"
DATABRICKS_CSV = DATA_DIR / "databricks.csv"
GROWTH_GEOJSON = DATA_DIR / "high_growth_50_market_gravity.geojson"
CONTROL_AREAS_SHP = DATA_DIR / "control-areas-shapefile" / "Control_Areas.shp"
TIGER_STATES_ZIP = DATA_DIR / "tl_2025_us_state.zip"
STATCAN_PROVINCES_ZIP = DATA_DIR / "lpr_000b21a_e.zip"
FOOTPRINT_LINKS_CSV = DATA_DIR / "dataset-links.csv"
CER_CAPACITY_CSV = DATA_DIR / "electricity-capacity-technology-2026.csv"
CER_EF2023_SUPPLEMENT_XLSX = DATA_DIR / "2023-data-supplement-figures-supplement-donnees-2023.xlsx"

# ----------------------------------------------------------------------------
# Assumptions (see PROJECT_HANDOFF.md §4)
# ----------------------------------------------------------------------------
W_PER_SQFT = 150.0              # <-- REVIEW: placeholder, needs a citable source or Timo's benchmark
DEDUP_RADIUS_M = 150.0          # 400 m merged separate buildings on the same campus
NA_BBOX_LAT = (15.0, 72.0)
NA_BBOX_LON = (-170.0, -50.0)
NORTH_AMERICA_COUNTRIES = {"United States", "Canada"}
HIFLD_NULL = -999999
CONTROL_AREAS_FALLBACK_CRS = "EPSG:3857"   # used only if the .prj is missing

FOOTPRINT_ZOOM = 9
FOOTPRINT_SEARCH_RADIUS_M = 200.0
SQM_TO_SQFT = 10.7639
EARTH_RADIUS_M = 6_371_000.0

# ----------------------------------------------------------------------------
# Manual geocode corrections, applied at load time so a rerun never reverts them.
# Matched on (name, country) as labelled in the source directory.
# ----------------------------------------------------------------------------
GEOCODE_FIXES = [
    {
        # Directory geocoded Charlottetown, PEI to Charlotte, NC (inside the bbox, so
        # the bad-geocode filter can't catch it). Location from Google Maps.
        "name": "VisionQuest ATC Data Center",
        "country": "Canada",
        "lat": 46.23723498843401,
        "lon": -63.12817766145676,
        "city": "Charlottetown",
        "note": "90 University Ave, Charlottetown PE; directory had Charlotte NC",
        # Footprint result from the handoff: nearest building 37 m, 260 m2 ~ 0.42 MW.
        "expected_footprint_mw": 0.42,
    },
]

# ----------------------------------------------------------------------------
# Canada (user-supplied approximate system-operator peaks; Ontario is a summer
# peak, the rest are winter peaks -- footnote in the methodology)
# ----------------------------------------------------------------------------
CANADA_PEAK_LOAD_2025_MW = {
    "Ontario": 25_600,
    "Quebec": 40_000,
    "Alberta": 12_200,
    "British Columbia": 10_500,
    "New Brunswick": 3_200,
    "Nova Scotia": 2_300,
}
PROVINCE_ABBREV = {
    "Ontario": "ON", "Quebec": "QC", "Alberta": "AB", "British Columbia": "BC",
    "New Brunswick": "NB", "Nova Scotia": "NS", "Prince Edward Island": "PE",
    "Manitoba": "MB", "Saskatchewan": "SK", "Newfoundland and Labrador": "NL",
    "Yukon": "YT", "Northwest Territories": "NT", "Nunavut": "NU",
}
CER_SCENARIO = "Current Measures"
BASE_YEAR = 2025
TARGET_YEAR = 2035
FIG8_BASE_YEAR = 2021
FIG8_END_YEAR = 2050

# IESO 2025 APO §2.4.2.1: data centre demand 3 TWh (2026) -> ~16 TWh (2050), 7.1% CAGR
IESO_DC_TWH_2026 = 3.0
IESO_DC_CAGR = 0.071
IESO_BASE_YEAR = 2026
HOURS_PER_YEAR = 8760


def lat_lon_to_quadkey(lat: float, lon: float, zoom: int = FOOTPRINT_ZOOM) -> str:
    """Bing-maps quadkey. Returned as str -- never let it become an int (leading zeros)."""
    lat_rad = math.radians(lat)
    x = (lon + 180.0) / 360.0
    y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0
    n = 2 ** zoom
    tile_x, tile_y = int(x * n), int(y * n)
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
