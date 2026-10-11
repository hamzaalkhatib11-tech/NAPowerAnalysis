# GGR442 × Red Horses: data centre grid-stress pipeline

Background, data sources and methodology are in [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md).

## Layout

| Path | Contents |
|---|---|
| `config.py` | Paths, assumptions (`W_PER_SQFT`, dedup radius, bbox), geocode fixes, Canadian peaks |
| `harmonize_datacenters.py` | Point layer, bad-geocode filter, BA/state/province joins, dedup, Databricks QA, U.S. BA stress index |
| `canada_footprint_mw.py` | Canadian MW from Microsoft building footprints |
| `canada_stress.py` | Canadian provincial stress for each year 2025 to 2035, using IESO, Hydro-Québec, AESO and B.C. data-centre figures (reference and high cases) |
| `us_time_series.py` | U.S. BA stress index for each year 2025 to 2035, written to `output/us_ba_time_series_2025_2035.csv` for the Experience Builder year slider |
| `local_stress.py` | Local stress on a 25 km hexagon grid, 2025 to 2035: data-centre load ÷ high-voltage supply reachable from each hex |
| `canada_local_stress.py` | The same local gaps and yearly hot spots for 9 provinces (11,831 hexes), with a siting model for new load |
| `export_gis.py` | Joins the time series to BA and province polygons and writes time-enabled layers to `output/gis/` (File Geodatabase, zipped copy, GeoJSON) |
| `sensitivity.py` | Re-runs the U.S. steps at 100 and 200 W/ft² and writes `output/sensitivity/w_per_sqft_summary.csv` (run separately, about 8 minutes) |
| `geo.py` | Shared point-in-polygon helpers |
| `verify_outputs.py` | Diffs `output/` against `reference_outputs/`, then checks invariants and the time series |
| `run_pipeline.py` | Runs all of the above in order |
| `data/` | Raw inputs (git-ignored) |
| `output/` | Generated results (git-ignored) |
| `reference_outputs/` | The sandbox-run CSVs behind the handoff's §6 numbers |

## Run

```
pip install pandas numpy pyproj shapely pyshp scikit-learn openpyxl
python run_pipeline.py
```

The full run takes about 17 minutes, mostly loading footprint tiles. Pass `--skip-footprint` to reuse the existing Canadian footprint results.

## Inputs expected in `data/`

- `datacenters.geojson`. If it's missing, the pipeline falls back to `datacenters.kml` and infers country from the joins.
- `im3_open_source_data_center_atlas.csv` and `high_growth_50_market_gravity.geojson`. Extract them from the IM3 zips.
- `databricks.csv`
- `july_generator2026.xlsx`: EIA-860M, July 2026, from eia.gov
- `EIA930_BALANCE_2025_Jan_Jun.csv` and `EIA930_BALANCE_2025_Jul_Dec.csv`: EIA-930 hourly demand, from eia.gov/electricity/gridmonitor/sixMonthFiles
- `control-areas-shapefile/`
- `tl_2025_us_state.zip`
- `US_Electric_Power_Transmission_Lines_-124296575735781399.zip`: HIFLD transmission lines, used by the local tier
- `lpr_000b21a_e.zip`
- `dataset-links.csv` and `electricity-capacity-technology-2026.csv`. Extract them from `GGR442_Datasets.zip`.
- `2023-data-supplement-figures-supplement-donnees-2023.xlsx`
- `nacei_plants_layer15.geojson` and `nacei_plants_layer28.geojson`: NRCan NACEI Canadian power plants (≥100 MW, and renewables ≥1 MW), from the `geoappext.nrcan.gc.ca` NACEI MapServer, layers 15 and 28
- `osm_power_lines/`: OpenStreetMap data from the Overpass API (© OpenStreetMap contributors, ODbL):
  - `CA-<prov>.json`: power lines;
  - `subs-CA-<prov>.json`: substations;
  - `places-CA.json`: cities and towns with population.
- `ieso_apo2025_Demand-Forecast-Module-Data.xlsb` (read with `pyxlsb`) and `ieso_apo2025_2025-Annual-Planning-Outlook-Data-Tables.xlsx`: IESO 2025 APO data tables
- `part-*.csv.gz`, the footprint tiles. The footprint step prints the URL of any tile that's missing.

## U.S. time-series method (`us_time_series.py`)

- **Demand:** the 2025 existing load (the point layer scaled to Databricks Active state totals) plus new IM3 sites for 2035 (high growth, 50% gravity). New sites are counted as IT load × PUE 1.2 and ramped at the scenario's own 10%/yr rate.
- **Supply:** HIFLD 2020 capacity rolled forward with EIA-860M additions and retirements. From 2028 on, each BA adds at least its own 2021–2025 average build rate each year, because EIA lists almost no new plants after 2029. `STRESS_INDEX_PIPELINE_ONLY` keeps the committed-pipeline-only result as a sensitivity case. Units are assigned to BAs by EIA's BA code (see `EIA_BA_CODE_TO_HIFLD` in `config.py`). Planned units count only if they have approval or are under construction.
- **Peak:** the EIA-930 2025 hourly peak (adjusted demand, with spikes filtered out), or HIFLD's peak where EIA-930's BA boundary doesn't match HIFLD's. `PEAK_LOAD_MW` is the baseline, non-data-centre peak: the system peak minus existing data-centre load. `SYSTEM_PEAK_MW` is the total.
- **Headroom:** `AVAIL_CAP_MW = TOTAL_CAP_MW - PEAK_LOAD_MW`, and `STRESS_INDEX = DC_LOAD_MW / AVAIL_CAP_MW`. So `STRESS_INDEX > 1` exactly when capacity can't cover the system peak.
- **Data artifacts:** `DATA_ARTIFACT_FLAG` marks BAs whose numbers reflect how the data is defined rather than real grid conditions. These are Duke Energy Florida, IID, and any BA whose 2025 capacity is below its own 2025 peak.
- **Assumptions to review in `config.py`:** `W_PER_SQFT`, `GROWTH_PUE` (1.2), `NON_DC_PEAK_GROWTH` (0.0, so the baseline peak is held at its 2025 level) and `EIA_PLANNED_STATUSES`.
- **Legacy file:** `ba_level_stress_index.csv` keeps the old HIFLD snapshot basis (load / `AVAIL_CAP`) for comparison with the handoff.

## Local (hex) tier (`local_stress.py`)

The BA index says whether a region has headroom. The hex index says whether the share of that headroom deliverable in a given place covers the data centres planned there.

- **Grid:** 15,431 flat-topped hexagons, 25 km across, covering the lower 48 states.
- **`LOCAL_HEADROOM_MW`:** the BA's headroom for that year (capacity minus non-data-centre peak, from the BA tier), multiplied by the hex's share of the BA's grid strength. It is never less than the hex's existing data-centre load, because what runs today is served.
  - Grid strength is generation in the hex, plus lines of 115 kV and above crossing the hex boundary, rated with the FERC-data fit in Athari & Wang (2017), arXiv:1709.06930.
  - Hex values add back up to the BA's headroom, so the two tiers agree.
- **`LOCAL_STRESS`:** `DC_LOAD_MW / LOCAL_HEADROOM_MW`, where above 1 is a local gap.
- **`DC_GAP_MW`:** the data-centre load that the local headroom can't serve.
- **`LOCAL_CLASS`:** one of `no_dc`, `distribution_served`, `surplus`, `near_limit`, `gap`, `no_capacity_data` or `data_artifact`.
- **Yearly hot spots of capacity gaps:**
  - Getis-Ord Gi\* on `DC_GAP_MW` each year, with a 75 km neighbourhood and the same false-discovery correction ArcGIS's Optimized Hot Spot tool uses.
  - Standardized against the pooled 2025–2035 mean, so years can be compared.
  - Output fields: `GI_Z`, `GI_P`, `GI_BIN` (−3 to 3, the ArcGIS convention) and `GI_PATTERN`. These drive the year slider.
- **Absolute local supply and demand were tested and rejected:**
  - Gross line ratings found almost no gaps, because they count power passing through a hex as if it were available there.
  - Spreading peak demand by Census population marked nearly every city as short before any data centre was built.
- **It's a screening model, not a power-flow study.**

## Canada

**Data-centre demand (`canada_stress.py`, settings in `config.py`).** Building footprints alone gave only 434 MW for all of Canada, so these sources replace them where they exist:

| Province | Existing load | New load, reference case | High case |
|---|---|---|---|
| Ontario | at least 293 MW (Toronto alone, per the IESO) | IESO 2025 APO data-centre energy forecast, read from the exact table (Demand Forecast Module, Fig. 11): +1,073 MW by 2035. Converted to MW at a 0.70 load factor, which reconciles it with the Large Step Loads paper's 1,070 MW | All proposed projects: +4,400 MW (Large Step Loads paper, Fig. 4) |
| Quebec | 190 MW at peak (Hydro-Québec) | Up to 1,000 MW by 2035 (Hydro-Québec) | 2,500 MW of requests under review, phased in by 2030 |
| Alberta | building footprints | AESO Phase I, 2027–28: +1,200 MW (Meta, Industrial Heartland, 970 MW; TransAlta Keephills, 230 MW) | 4,800 MW qualified in Phase I |
| B.C. | building footprints | Legislated allocation caps: +600 MW by 2029 | same as reference |

- The baseline peak excludes existing data-centre load, the same rule as for U.S. BAs.
- Alberta's 2025 peak is now the 12,785 MW record from December 2025.
- Manitoba (5,111 MW), Saskatchewan (3,910 MW) and Newfoundland & Labrador (1,765 MW, Island system) were added. They have no known data centres yet.
- Newfoundland & Labrador is flagged as a data artifact. Its CER capacity includes Churchill Falls (about 5.4 GW), whose output is contracted to Hydro-Québec until 2041.

**Hex tier (`canada_local_stress.py`).** This uses the U.S. method with Canadian inputs:

- **Headroom:** each province's headroom is shared across its hexes by grid strength. Grid strength is NRCan NACEI plants plus OpenStreetMap lines of 115 kV and above crossing the hex boundary.
- **New load placement:** named sites go where they are (Alberta). The rest goes through a siting model that stands in for IM3, which covers only the U.S.:
  - Eligible hexes contain an OpenStreetMap substation of 115 kV or above.
  - `SITING_SCORE` = 0.5 × market gravity (population within 100 km) + 0.5 × grid strength, each log-scaled within the province. This mirrors IM3's 50% market-gravity scenario.
  - Each province's unsited new load goes to its 10 best hexes, in proportion to their score.
- **Limitation:** the NACEI plant data is from 2013–2021. It only sets each hex's share of headroom, so its age matters little.

## ArcGIS

Two things are ready to upload:

- `output/gis/ggr442_grid_stress.gdb.zip`: upload it to ArcGIS Online as a File Geodatabase, or open the `.gdb` in ArcGIS Pro.
- The GeoJSON files in `output/gis/`: these can be uploaded to ArcGIS Online directly.

**Layers in the geodatabase:**

| Layer | Contents |
|---|---|
| `us_ba_stress_timeseries` | U.S. BA stress, 770 polygons (70 BAs × 11 years). Time field: `YEAR_DATE` (Date) |
| `ca_province_stress_timeseries` | Canadian province stress, 66 polygons, same schema |
| `dc_existing_points` | Existing data centres |
| `dc_growth_sites_2035` | IM3 projected sites for 2035 |
| `us_hex_local_stress_timeseries` | 169,741 hexagons (15,431 × 11 years). Time field: `YEAR_DATE` |
| `transmission_115kv_plus` | 51,097 lines of 115 kV and above, with the MW rating used |
| `ca_hex_local_stress_timeseries` | 94,446 Canadian hexagons (8,586 × 11 years), same fields as the U.S. hex layer (`PROVINCE` instead of `BA_NAME`) |
| `ca_transmission_osm_115kv_plus` | 10,187 OpenStreetMap lines of 115 kV and above. Credit "© OpenStreetMap contributors" on the map |

**To set up the time slider:**

1. Enable time on both stress layers using `YEAR_DATE`, with a 1-year interval.
2. In Experience Builder, add one Timeline widget that drives both layers.
3. Symbolize `STRESS_INDEX` with a diverging ramp centred on 1.0.
4. Grey out features where `DATA_ARTIFACT_FLAG = 1`.

**Hot spots of capacity gaps (the year slider):**

On `us_hex_local_stress_timeseries`:

1. Enable time on `YEAR_DATE` with a 1-year step.
2. Symbolize with **Unique Values** on `GI_PATTERN`, using the ArcGIS hot spot colours, with "Not Significant" set to no fill.
3. Add a second copy filtered to `LOCAL_CLASS = 'gap'` to show the hexes that are actually short.

**Decade trend (static):**

1. Run **Create Space Time Cube From Defined Locations** on the same layer:
   - Location ID: `HEX_ID`
   - Time field: `YEAR_DATE`, 1-year step
   - Variable: `DC_GAP_MW`
2. Run **Emerging Hot Spot Analysis** on the cube. It labels each hex as a new, intensifying or persistent hot spot over the decade.
