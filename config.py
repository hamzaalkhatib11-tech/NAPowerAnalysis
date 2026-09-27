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
EIA860M_XLSX = DATA_DIR / "july_generator2026.xlsx"
FOOTPRINT_LINKS_CSV = DATA_DIR / "dataset-links.csv"
CER_CAPACITY_CSV = DATA_DIR / "electricity-capacity-technology-2026.csv"
CER_EF2023_SUPPLEMENT_XLSX = DATA_DIR / "2023-data-supplement-figures-supplement-donnees-2023.xlsx"

# ----------------------------------------------------------------------------
# Assumptions (see PROJECT_HANDOFF.md §4)
# ----------------------------------------------------------------------------
W_PER_SQFT = 150.0              # <-- REVIEW: placeholder, needs a citable source
DEDUP_RADIUS_M = 150.0          # 400 m merged separate buildings on the same campus
IM3_EXCLUDED_TYPES = {"campus"}  # IM3 campus "sqft" is acquired land area, not floor area
DATABRICKS_BASELINE_STAGES = {"Active"}   # 2025 control totals exclude Announcement/Construction
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


# ----------------------------------------------------------------------------
# U.S. 2025-2035 time series (us_time_series.py)
# ----------------------------------------------------------------------------
TS_YEARS = range(2025, 2036)
# IM3 projected sites are NEW facilities through 2035 under the EPRI (2024) "high"
# scenario: 10%/yr data-centre demand growth. The same rate shapes the
# 2025->2035 ramp so the in-between years follow the scenario's own curve.
GROWTH_SCENARIO_ANNUAL_RATE = 0.10
# IM3 gives IT power; grid draw = IT x PUE (cooling and power-conversion
# overhead). 1.2 is typical of new hyperscale builds. The existing 2025 layer is
# already whole-facility (W/ft2 of building area), so PUE applies to new sites only.
GROWTH_PUE = 1.20
# Annual growth of the non-data-centre baseline peak after the peak's base year
# (2025 where EIA-930 is used). 0 = held flat at the observed 2025 level.
NON_DC_PEAK_GROWTH = 0.0        # <-- REVIEW

# EIA-930 hourly demand, 2025. Peak = max of EIA's "Demand (MW) (Adjusted)"
# after dropping residual spikes above PEAK_SPIKE_FACTOR x the 99.9th percentile.
EIA930_FILES = [DATA_DIR / "EIA930_BALANCE_2025_Jan_Jun.csv", DATA_DIR / "EIA930_BALANCE_2025_Jul_Dec.csv"]
PEAK_SPIKE_FACTOR = 1.2
# EIA-930 BA footprints don't always match HIFLD's. If the 2025 EIA-930 peak is
# outside this ratio of HIFLD's own peak, the two datasets describe different
# systems (e.g. WALC 1,995 vs 159 MW) and pairing a 930 peak with HIFLD
# capacity would invent a gap, so the HIFLD peak is kept.
PEAK_SCOPE_RATIO = (0.7, 1.5)

# BAs whose stress values are data-quality artifacts, not findings. Kept in the
# outputs with DATA_ARTIFACT_FLAG = True so maps can grey them out. BAs whose
# HIFLD capacity is below their own 2025 system peak are flagged automatically
# too (the lights stayed on, so that capacity figure can't be the whole system).
DATA_ARTIFACT_BAS = {
    "DUKE ENERGY FLORIDA INC": "HIFLD TOTAL_CAP (9.8 GW) is well below EIA-860M plant capacity "
                               "in the BA (14.7 GW), so its headroom is understated",
    "IMPERIAL IRRIGATION DISTRICT": "HIFLD TOTAL_CAP (727 MW) is below the BA's own peak "
                                    "(~1.1 GW); no headroom even in 2025",
}

# GIS export (export_gis.py)
GIS_DIR = OUT_DIR / "gis"
GIS_SIMPLIFY_M = 500.0          # polygon simplification tolerance (Web Mercator metres)
# EIA-860M capacity measure and which planned projects count as coming online.
EIA_CAPACITY_COLUMN = "Net Summer Capacity (MW)"
# (T) approved, (U) <=50% built, (V) >50% built, (TS) built, not yet operating.
# (P) and (L) projects -- no approvals yet -- are excluded as too speculative.
EIA_PLANNED_STATUSES = ("(T)", "(U)", "(V)", "(TS)")

# EIA-860M "Balancing Authority Code" -> HIFLD Control_Areas NAME. Generators are
# assigned by EIA's code, not by plant location: BA polygons overlap (APS/SRP,
# WAPA/PSCo, the Florida utilities), so a spatial join credits one BA's new
# plants to its neighbour. HIFLD BAs with no current EIA code (e.g. OVEC,
# folded into PJM) get no roll-forward.
EIA_BA_CODE_TO_HIFLD = {
    "PJM": "PJM INTERCONNECTION, LLC",
    "MISO": "MIDCONTINENT INDEPENDENT TRANSMISSION SYSTEM OPERATOR, INC..",
    "ERCO": "ELECTRIC RELIABILITY COUNCIL OF TEXAS, INC.",
    "SWPP": "SOUTHWEST POWER POOL",
    "CISO": "CALIFORNIA INDEPENDENT SYSTEM OPERATOR",
    "SOCO": "SOUTHERN COMPANY SERVICES, INC. - TRANS",
    "NYIS": "NEW YORK INDEPENDENT SYSTEM OPERATOR",
    "FPL": "FLORIDA POWER & LIGHT COMPANY",
    "TVA": "TENNESSEE VALLEY AUTHORITY",
    "ISNE": "ISO NEW ENGLAND INC.",
    "BPAT": "BONNEVILLE POWER ADMINISTRATION",
    "DUK": "DUKE ENERGY CAROLINAS",
    "PACE": "PACIFICORP - EAST",
    "PACW": "PACIFICORP - WEST",
    "SRP": "SALT RIVER PROJECT",
    "CPLE": "DUKE ENERGY PROGRESS EAST",
    "CPLW": "DUKE ENERGY PROGRESS WEST",
    "PSCO": "PUBLIC SERVICE COMPANY OF COLORADO",
    "FPC": "DUKE ENERGY FLORIDA INC",
    "NEVP": "NEVADA POWER COMPANY",
    "AZPS": "ARIZONA PUBLIC SERVICE COMPANY",
    "WACM": "WESTERN AREA POWER ADMINISTRATION - ROCKY MOUNTAIN REGION",
    "WALC": "WESTERN AREA POWER ADMINISTRATION - DESERT SOUTHWEST REGION",
    "WAUW": "WESTERN AREA POWER ADMINISTRATION UGP WEST",
    "LDWP": "LOS ANGELES DEPARTMENT OF WATER AND POWER",
    "LGEE": "LOUISVILLE GAS AND ELECTRIC COMPANY AND KENTUCKY UTILITIES",
    "TEC": "TAMPA ELECTRIC COMPANY",
    "PNM": "PUBLIC SERVICE COMPANY OF NEW MEXICO",
    "SCEG": "SOUTH CAROLINA ELECTRIC & GAS COMPANY",
    "SC": "SOUTH CAROLINA PUBLIC SERVICE AUTHORITY",
    "AECI": "ASSOCIATED ELECTRIC COOPERATIVE, INC.",
    "BANC": "BALANCING AUTHORITY OF NORTHERN CALIFORNIA",
    "IPCO": "IDAHO POWER COMPANY",
    "TEPC": "TUCSON ELECTRIC POWER COMPANY",
    "FMPP": "FLORIDA MUNICIPAL POWER POOL",
    "PGE": "PORTLAND GENERAL ELECTRIC COMPANY",
    "NWMT": "NORTHWESTERN ENERGY (NWMT)",
    "JEA": "JEA",
    "PSEI": "PUGET SOUND ENERGY",
    "AVRN": "AVANGRID RENEWABLES LLC",
    "SPA": "SOUTHWESTERN POWER ADMINISTRATION",
    "EPE": "EL PASO ELECTRIC COMPANY",
    "SEC": "SEMINOLE ELECTRIC COOPERATIVE",
    "AVA": "AVISTA CORPORATION",
    "SCL": "SEATTLE CITY LIGHT",
    "IID": "IMPERIAL IRRIGATION DISTRICT",
    "SEPA": "SOUTHEASTERN POWER ADMINISTRATION",
    "CHPD": "PUBLIC UTILITY DISTRICT NO. 1 OF CHELAN COUNTY",
    "DOPD": "PUD NO. 1 OF DOUGLAS COUNTY",
    "GCPD": "PUBLIC UTILITY DISTRICT NO. 2 OF GRANT COUNTY, WASHINGTON",
    "HECO": "HAWAIIAN ELECTRIC CO INC",
    "TAL": "CITY OF TALLAHASSEE",
    "TPWR": "CITY OF TACOMA, DEPARTMENT OF PUBLIC UTILITIES, LIGHT DIVISION",
    "TIDC": "TURLOCK IRRIGATION DISTRICT",
    "GVL": "GAINESVILLE REGIONAL UTILITIES",
    "GRID": "GRIDFORCE ENERGY MANAGEMENT, LLC",
    "GRIS": "GRIDFORCE SOUTH",
    "DEAA": "ARLINGTON VALLEY, LLC - AVBA",
    "GRIF": "GRIFFITH ENERGY, LLC",
    "HGMA": "NEW HARQUAHALA GENERATING COMPANY, LLC - HGBA",
    "GWA": "NATURENER POWER WATCH, LLC (GWA)",
    "WWA": "NATURENER WIND WATCH, LLC",
    "YAD": "ALCOA POWER GENERATING, INC. - YADKIN DIVISION",
    "CEA": "CHUGACH ELECTRIC ASSN INC",
    "HST": "CITY OF HOMESTEAD",
    "NSB": "NEW SMYRNA BEACH, UTILITIES COMMISSION OF",
    "GRMA": "GILA RIVER POWER, LLC",
    "PSOP": "SOUTHWEST POWER POOL",   # legacy code
    "OVEC": "OHIO VALLEY ELECTRIC CORPORATION",
    "EEI": "ELECTRIC ENERGY, INC.",
    "GLHB": "ELECTRIC ENERGY, INC.",   # successor BA code; Joppa (1,187 MW) retired 2022
    "AEC": "POWERSOUTH ENERGY COOPERATIVE",
}


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
