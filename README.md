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

The full run takes about 6 minutes, mostly loading footprint tiles. Pass `--skip-footprint` to reuse the existing Canadian footprint results.

## Inputs expected in `data/`

- `datacenters.geojson`. If it's missing, the pipeline falls back to `datacenters.kml` and infers country from the joins.
- `im3_open_source_data_center_atlas.csv` and `high_growth_50_market_gravity.geojson`. Extract them from the IM3 zips.
- `databricks.csv`
- `july_generator2026.xlsx`: EIA-860M, July 2026, from eia.gov
- `control-areas-shapefile/`
- `tl_2025_us_state.zip`
- `lpr_000b21a_e.zip`
- `dataset-links.csv` and `electricity-capacity-technology-2026.csv`. Extract them from `GGR442_Datasets.zip`.
- `2023-data-supplement-figures-supplement-donnees-2023.xlsx`
- `part-*.csv.gz`, the footprint tiles. The footprint step prints the URL of any tile that's missing.

## U.S. time-series method (`us_time_series.py`)

- **Demand:** the 2025 existing load (the point layer scaled to Databricks Active state totals) plus new IM3 sites for 2035 (high growth, 50% gravity), ramped at the scenario's own 10%/yr rate.
- **Supply:** HIFLD 2020 capacity rolled forward with EIA-860M additions and retirements. Units are assigned to BAs by EIA's BA code (see `EIA_BA_CODE_TO_HIFLD` in `config.py`). Planned units count only if they have approval or are under construction.
- **Headroom:** `AVAIL_CAP_MW = TOTAL_CAP_MW - PEAK_LOAD_MW`, and `STRESS_INDEX = DC_LOAD_MW / AVAIL_CAP_MW`. This is the same basis as Canada.
- **Assumptions to review in `config.py`:** `W_PER_SQFT`, `GROWTH_PUE` (1.0), `NON_DC_PEAK_GROWTH` (0.0, so peaks are held at HIFLD values) and `EIA_PLANNED_STATUSES`.
- **Legacy file:** `ba_level_stress_index.csv` keeps the old HIFLD snapshot basis (load / `AVAIL_CAP`) for comparison with the handoff.
