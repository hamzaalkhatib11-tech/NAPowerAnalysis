"""
config.py

Shared paths, assumptions and small helpers for the GGR442 pipeline.
Every script imports from here so a parameter only ever changes in one place.
"""

import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
# GGR_OUT_DIR / GGR_W_PER_SQFT let sensitivity.py run the pipeline into a side folder
OUT_DIR = Path(os.environ.get("GGR_OUT_DIR", ROOT / "output"))
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
# 150 is consistent with the data: IM3 building sqft against Databricks Active MW
# implies ~159 W/ft2 nationally. sensitivity.py brackets it at 100 and 200.
W_PER_SQFT = float(os.environ.get("GGR_W_PER_SQFT", 150.0))
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
    "Alberta": 12_785,          # record peak, Dec 11 2025 (alberta.ca/datacentres/numbers); was 12,200
    "British Columbia": 10_500,
    "New Brunswick": 3_200,
    "Nova Scotia": 2_300,
    "Manitoba": 5_111,          # all-time record, Jan 2025 (Manitoba Hydro, via CBC)
    "Saskatchewan": 3_910,      # annual net peak, SaskPower Annual Report 2025-26
    "Newfoundland and Labrador": 1_765,   # Island system, winter 2023/24 (NL Hydro)
}
# Provinces whose stress values reflect data definitions rather than grid
# conditions (kept on the map, greyed out -- as DATA_ARTIFACT_FLAG for U.S. BAs).
CANADA_ARTIFACT_PROVINCES = {
    "Newfoundland and Labrador": "CER capacity includes Churchill Falls (~5.4 GW), whose output is "
                                 "contracted to Hydro-Quebec until 2041, so provincial headroom is overstated",
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
# Canadian data-centre demand (canada_stress.py, canada_local_stress.py)
# Building footprints alone gave 434 MW for all of Canada, with no control
# total. These utility/system-operator figures replace them where they exist.
# ----------------------------------------------------------------------------
# 2025 existing load floors (MW). Province load = max(footprint sum, floor).
CANADA_DC_EXISTING_FLOOR_MW = {
    # IESO, Large Step Loads technical paper (Jul 2025): "Toronto alone was home
    # to 293 MW of data centres" (Sept 2023) -- a floor for Ontario as a whole
    "Ontario": 293,
    # Hydro-Quebec: data centres subject to the proposed CD rate (>= 5 MW)
    # "currently consume 190 MW at peak"
    "Quebec": 190,
}
# New data-centre load on top of the existing floor, MW by year (reference).
CANADA_DC_NEW_REF_MW = {
    # IESO Large Step Loads paper, Fig. 5: data-centre project load included in
    # the 2025 APO (likelihood-weighted), read from the chart (~+/-20 MW)
    # Ontario is computed in canada_stress.py from IESO's exact 2025 APO table
    # (data-centre energy growth since 2026, Demand Forecast Module Fig. 11),
    # converted to MW with ONTARIO_DC_LOAD_FACTOR. These chart-read values
    # (Large Step Loads paper Fig. 5) are only the fallback if the file is absent.
    "Ontario": {2025: 70, 2026: 140, 2027: 320, 2028: 405, 2029: 540, 2030: 640,
                2031: 705, 2032: 785, 2033: 815, 2034: 825, 2035: 1070},
    # Hydro-Quebec: could reach 1,000 MW by 2035 -> linear from 190 MW in 2025
    "Quebec": {y: round((1000 - 190) * (y - 2025) / 10) for y in range(2025, 2036)},
    # AESO Phase I: 1,200 MW for in-service 2027-2028 (Meta, Sturgeon County,
    # 970 MW; TransAlta Keephills, Parkland County, 230 MW); held flat after
    "Alberta": {y: (0 if y < 2027 else 485 if y == 2027 else 1200) for y in range(2025, 2036)},
    # BC Energy Statutes Amendment Act 2025: caps of 400 MW (Feb 2026-Feb 2028)
    # and 200 MW (Feb 2028-Feb 2029) for new data-centre capacity, assumed taken
    # up in full; held flat after
    "British Columbia": {y: (0 if y < 2027 else 200 if y == 2027 else 400 if y == 2028 else 600)
                         for y in range(2025, 2036)},
}
# High case: every proposed project / request, MW by year.
CANADA_DC_NEW_HIGH_MW = {
    # IESO paper Fig. 4: all proposed data-centre load, tiers 1-3
    "Ontario": {2025: 100, 2026: 240, 2027: 500, 2028: 750, 2029: 1180, 2030: 1640,
                2031: 2200, 2032: 2650, 2033: 3100, 2034: 3550, 2035: 4400},
    # Hydro-Quebec: ~2,500 MW of data-centre block requests under government
    # review (Les Affaires, 2026), assumed phased in linearly by 2030
    "Quebec": {y: round(190 + min(1.0, max(0, y - 2025) / 5) * 2500) - 190 for y in range(2025, 2036)},
    # AESO: 4,800 MW qualified in Phase I (15 projects), phased in 2027-2030
    "Alberta": {y: round(4800 * min(1.0, max(0, y - 2026) / 4)) for y in range(2025, 2036)},
    "British Columbia": None,   # legislated caps -> same as reference
}
# Named projects with a known site; the rest of a province's new load goes to
# the siting model's top hexes (CA_SITING_*), or to its existing data-centre
# points if it has no eligible hex. Coordinates are approximate (municipality / site level).
CANADA_DC_SITES = [
    {"name": "Meta campus (Industrial Heartland)", "province": "Alberta", "lat": 53.80, "lon": -113.25,
     "share_of_new": 970 / 1200},
    {"name": "TransAlta Keephills", "province": "Alberta", "lat": 53.45, "lon": -114.45,
     "share_of_new": 230 / 1200},
]
NACEI_PLANT_FILES = [DATA_DIR / "nacei_plants_layer15.geojson", DATA_DIR / "nacei_plants_layer28.geojson"]
OSM_LINES_DIR = DATA_DIR / "osm_power_lines"
CA_HEX_CRS = "ESRI:102001"      # Canada Albers Equal Area
IESO_APO_DFM_XLSB = DATA_DIR / "ieso_apo2025_Demand-Forecast-Module-Data.xlsb"
# Energy -> peak MW for Ontario's data-centre growth. IESO's two 2025 products
# agree at this value: APO energy growth 2026->2035 of 6.58 TWh (751 MW average)
# vs the Large Step Loads paper's 1,070 MW of included project demand by 2035.
ONTARIO_DC_LOAD_FACTOR = 0.70

# Canadian siting model for new load without an announced site (stand-in for
# IM3's rules): eligible hexes hold a >= 115 kV substation (OSM); score =
# GRAVITY_WEIGHT x population within GRAVITY_RADIUS (market gravity, as IM3's
# 50% scenario) + (1 - GRAVITY_WEIGHT) x grid strength (cheaper connection);
# each province's unsited new load goes to its TOP_K hexes in proportion to score.
CA_SITING_GRAVITY_WEIGHT = 0.5
CA_SITING_GRAVITY_RADIUS_M = 100_000.0
CA_SITING_TOP_K = 10


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

# ----------------------------------------------------------------------------
# Local (hex) stress (local_stress.py)
# ----------------------------------------------------------------------------
TRANSMISSION_ZIP = DATA_DIR / "US_Electric_Power_Transmission_Lines_-124296575735781399.zip"
HEX_CRS = "EPSG:5070"           # NAD83 / CONUS Albers, equal-area metres
HEX_WIDTH_M = 25_000.0          # flat-to-flat width (~540 km2 per hex)
EXCLUDED_STATES = {"AK", "HI", "PR", "VI", "GU", "AS", "MP"}   # CONUS only
# Large data centres connect at 115-161 kV as well as 230 kV+ (TVA's backbone is
# 161 kV), so sub-transmission from 115 kV counts. Lower voltages can't carry a
# campus-scale load.
MIN_LINE_KV = 115
# Per-circuit capacity by voltage: the power-law fit to the average MVA rating of
# real U.S. transmission lines in FERC data, S = a * kV^b, from
#   Athari, M. H. & Wang, Z. (2017). "Interdependence of Transmission Branch
#   Parameters on the Voltage Levels." arXiv:1709.06930, Fig. 13.
# MVA is taken as MW (power factor ~1). Gives ~214 MW at 115 kV, ~633 at 230 kV,
# ~1,190 at 345 kV, ~2,170 at 500 kV, ~3,980 at 735 kV.
LINE_RATING_FIT = (0.1295, 1.565)
LINE_KV_VALID = (1, 1100)       # HIFLD uses -999999 for unknown voltage
# A line only adds to a hex's grid strength where it can deliver power there:
# it must cross into the hex AND reach a >= MIN_LINE_KV substation inside it
# (within SUBSTATION_SNAP_M of the substation's OSM point). Lines that merely pass
# through, with no substation, serve somewhere else and count for nothing.
US_SUBSTATIONS_DIR = DATA_DIR / "osm_us_substations"
SUBSTATION_SNAP_M = 1000.0
# How grid strength combines plants and lines (grid_strength_sensitivity.py runs all three):
#   "sum"                  plant MW + delivering-line MW (the default)
#   "no_plant_switchyards" as "sum", but a substation within PLANT_SWITCHYARD_RADIUS_M
#                          of a plant >= PLANT_SWITCHYARD_MIN_MW is treated as that
#                          plant's switchyard, so lines reaching only it (its export
#                          lines) don't count -- the plant's power is counted once
#   "max"                  max(plant MW, line MW), so the two are never added
GRID_STRENGTH_MODE = "sum"
GRID_STRENGTH_MODES = ("sum", "no_plant_switchyards", "max")
PLANT_SWITCHYARD_RADIUS_M = 1500.0   # <-- REVIEW: EIA/NACEI give one point per plant
PLANT_SWITCHYARD_MIN_MW = 50.0
# Yearly hot spots of capacity gaps (Getis-Ord Gi*, binary weights within this
# distance, self included). 75 km = the two rings of hexes around each hex
# (~18 neighbours), roughly a metro area or a utility planning zone.
GI_DISTANCE_M = 75_000.0
GI_FDR = True                   # Benjamini-Hochberg correction, as in ArcGIS Optimized Hot Spot
LOCAL_STRESS_CAP = 3.0         # analysis field is capped so one hex can't dominate a hot spot run
# Below this, a hex's data-centre load is small enough to be served from the
# distribution network, so having no >= 115 kV line isn't a pressure point. Without
# it, a 2.7 MW site in Wisconsin was capped at 3.0 and seeded a 7-hex hot spot.
MIN_DC_MW_FOR_HV = 10.0

# GIS export (export_gis.py)
GIS_DIR = OUT_DIR / "gis"
GIS_SIMPLIFY_M = 500.0          # polygon simplification tolerance (Web Mercator metres)
# EIA-860M capacity measure and which planned projects count as coming online.
EIA_CAPACITY_COLUMN = "Net Summer Capacity (MW)"
# (T) approved, (U) <=50% built, (V) >50% built, (TS) built, not yet operating.
# (P) and (L) projects -- no approvals yet -- are excluded as too speculative.
EIA_PLANNED_STATUSES = ("(T)", "(U)", "(V)", "(TS)")

# EIA-860M only sees about 3 years ahead: committed additions fall from ~49 GW
# (2026) to ~7 GW (2029) and ~2 GW/yr after, while BAs actually built ~44 GW/yr
# in 2021-2025 and scheduled retirements keep coming. Taken literally that means
# building stops around 2029. From TREND_START_YEAR on, each BA therefore adds
# at least its own 2021-2025 average build rate x TREND_BUILD_FACTOR per year
# (the committed pipeline is used where it is larger). The pipeline-only result
# is kept alongside as a sensitivity (STRESS_INDEX_PIPELINE_ONLY).
TREND_HISTORY_YEARS = (2021, 2025)
TREND_START_YEAR = 2028
TREND_BUILD_FACTOR = 1.0        # <-- REVIEW: 1.0 = keep building at the recent pace

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
