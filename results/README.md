# Results

Copies of the main tables from `output/`, from the run of 2026-10-26, so the
results can be read without the raw data or a pipeline run. `python run_pipeline.py`
regenerates them in `output/`; these copies are not updated automatically.

| File | Contents |
|---|---|
| `us_ba_time_series_2025_2035.csv` | U.S. Grid Headroom Stress Index per balancing authority per year (main U.S. result) |
| `canada_stress_index_timeseries.csv` | Canadian stress index per province per year, reference and high case (main Canadian result) |
| `canada_stress_index_2025_vs_2035.csv` | Canadian provinces, 2025 vs 2035 summary |
| `qa_state_mw_totals.csv` | Data-centre MW from our points vs Databricks operating totals, per state |
| `adjusted_baseline_load_by_state.csv` | 2025 data-centre load per state after scaling to Databricks, with scale factors |
| `us_ba_peak_2025.csv` | 2025 system peak per BA: EIA-930 vs HIFLD and which was used |
| `canada_dc_with_mw.csv` | Canadian data centres with building-footprint MW |

The 25 km hex results are not copied here as CSVs (about 35 MB each); they are layers in
the geodatabase, `gis/ggr442_grid_stress.gdb.zip`.
