# GGR442 × Red Horses: data centre grid-stress pipeline

Background, data sources and methodology are in [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md).

## Layout

| Path | Contents |
|---|---|
| `config.py` | Paths, assumptions (`W_PER_SQFT`, dedup radius, bbox), geocode fixes, Canadian peaks |
| `harmonize_datacenters.py` | Point layer, bad-geocode filter, BA/state/province joins, dedup, Databricks QA, U.S. BA stress index |
| `canada_footprint_mw.py` | Canadian MW from Microsoft building footprints |
| `canada_stress.py` | Canadian headroom and stress index, 2025 to 2035 (yearly), IESO Ontario growth |
| `verify_outputs.py` | Diffs `output/` against `reference_outputs/` and checks the §6 benchmarks |
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
- `control-areas-shapefile/`
- `tl_2025_us_state.zip`
- `lpr_000b21a_e.zip`
- `dataset-links.csv` and `electricity-capacity-technology-2026.csv`. Extract them from `GGR442_Datasets.zip`.
- `2023-data-supplement-figures-supplement-donnees-2023.xlsx`
- `part-*.csv.gz`, the footprint tiles. The footprint step prints the URL of any tile that's missing.
