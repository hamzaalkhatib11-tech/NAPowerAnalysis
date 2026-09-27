# GGR442 × Red Horses: data centre grid-stress pipeline

Background, data sources and methodology are in [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md).

## Layout

| Path | Contents |
|---|---|
| `config.py` | Paths, assumptions (`W_PER_SQFT`, dedup radius, bbox), geocode fixes, Canadian peaks |
| `harmonize_datacenters.py` | Point layer, bad-geocode filter, BA/state/province joins, dedup, Databricks QA, U.S. BA stress index |
| `canada_footprint_mw.py` | Canadian MW from Microsoft building footprints |
| `canada_stress.py` | Canadian headroom and stress index, 2025 to 2035 (yearly), IESO Ontario growth |
| `us_time_series.py` | U.S. BA stress index for each year 2025 to 2035, written to `output/us_ba_time_series_2025_2035.csv` for the Experience Builder year slider |
| `export_gis.py` | Joins the time series to BA and province polygons and writes time-enabled layers to `output/gis/` (File Geodatabase, zipped copy, GeoJSON) |
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

The full run takes about 13 minutes, mostly loading footprint tiles. Pass `--skip-footprint` to reuse the existing Canadian footprint results.

## Inputs expected in `data/`

- `datacenters.geojson`. If it's missing, the pipeline falls back to `datacenters.kml` and infers country from the joins.
- `im3_open_source_data_center_atlas.csv` and `high_growth_50_market_gravity.geojson`. Extract them from the IM3 zips.
- `databricks.csv`
- `july_generator2026.xlsx`: EIA-860M, July 2026, from eia.gov
- `EIA930_BALANCE_2025_Jan_Jun.csv` and `EIA930_BALANCE_2025_Jul_Dec.csv`: EIA-930 hourly demand, from eia.gov/electricity/gridmonitor/sixMonthFiles
- `control-areas-shapefile/`
- `tl_2025_us_state.zip`
- `lpr_000b21a_e.zip`
- `dataset-links.csv` and `electricity-capacity-technology-2026.csv`. Extract them from `GGR442_Datasets.zip`.
- `2023-data-supplement-figures-supplement-donnees-2023.xlsx`
- `part-*.csv.gz`, the footprint tiles. The footprint step prints the URL of any tile that's missing.

## U.S. time-series method (`us_time_series.py`)

- **Demand:** the 2025 existing load (the point layer scaled to Databricks Active state totals) plus new IM3 sites for 2035 (high growth, 50% gravity). New sites are counted as IT load × PUE 1.2 and ramped at the scenario's own 10%/yr rate.
- **Supply:** HIFLD 2020 capacity rolled forward with EIA-860M additions and retirements. Units are assigned to BAs by EIA's BA code (see `EIA_BA_CODE_TO_HIFLD` in `config.py`). Planned units count only if they have approval or are under construction.
- **Peak:** the EIA-930 2025 hourly peak (adjusted demand, with spikes filtered out), or HIFLD's peak where EIA-930's BA boundary doesn't match HIFLD's. `PEAK_LOAD_MW` is the baseline, non-data-centre peak: the system peak minus existing data-centre load. `SYSTEM_PEAK_MW` is the total.
- **Headroom:** `AVAIL_CAP_MW = TOTAL_CAP_MW - PEAK_LOAD_MW`, and `STRESS_INDEX = DC_LOAD_MW / AVAIL_CAP_MW`. So `STRESS_INDEX > 1` exactly when capacity can't cover the system peak.
- **Data artifacts:** `DATA_ARTIFACT_FLAG` marks BAs whose numbers reflect how the data is defined rather than real grid conditions. These are Duke Energy Florida, IID, and any BA whose 2025 capacity is below its own 2025 peak.
- **Assumptions to review in `config.py`:** `W_PER_SQFT`, `GROWTH_PUE` (1.2), `NON_DC_PEAK_GROWTH` (0.0, so the baseline peak is held at its 2025 level) and `EIA_PLANNED_STATUSES`.
- **Legacy file:** `ba_level_stress_index.csv` keeps the old HIFLD snapshot basis (load / `AVAIL_CAP`) for comparison with the handoff.

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

**To set up the time slider:**

1. Enable time on both stress layers using `YEAR_DATE`, with a 1-year interval.
2. In Experience Builder, add one Timeline widget that drives both layers.
3. Symbolize `STRESS_INDEX` with a diverging ramp centred on 1.0.
4. Grey out features where `DATA_ARTIFACT_FLAG = 1`.
