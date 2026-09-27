# GGR442 × Red Horses Solutions: Project Handoff

**Project:** Geospatial Analysis of Data Center Electricity Demand and Emerging Grid Capacity Gaps in North America
**Course:** GGR442H5F (U of T Mississauga) · **Team Red Horses:** Hamza Alkhatib, Zain Owais Arif, Hyeonwoo (Aaron) Kang, Jerry Li
**Mentor:** Timo Stueven, COO, Red Horses Solutions Ltd.
**Handoff date:** 2026-09-26

> **Important for the next session:** the sandbox that ran this pipeline has been reset, so its `output/` folder no longer exists. The two main scripts (`harmonize_datacenters.py` and `canada_footprint_mw.py`) and every output CSV were sent to the chat as file downloads. Put those downloaded copies in the new repo. If any are missing, rebuild them from §4 and §5 below, which give the logic and parameters in full.

---

## 1. Goal and deliverables

The project investigates how data center electricity demand growth maps onto North American grid capacity through 2035. The aim is to find hotspots where new generation, transmission, or substations will be needed.

**Final deliverables (late Nov 2026):**
- ArcGIS Experience Builder dashboard with a year slider (2025 → 2035) and gap visualization
- Esri ArcGIS StoryMap

**Proposal milestones:**

| Milestone | Target | Status |
|---|---|---|
| Datasets complete | Late October | Effectively done |
| Gaps and hotspots identified | Mid-November | First pass done |
| Deliverables | Late November | Not started |
| Final presentation | Early December | Not started |

## 2. Core metric: Grid Headroom Stress Index

```
Stress Index = Σ Data Center Load (MW) / (Available Capacity − Baseline Peak Demand)
```

- **< 1.0:** surplus
- **= 1.0:** at the limit
- **> 1.0:** power gap

How the denominator is computed:
- **U.S.:** the HIFLD `AVAIL_CAP` field when present, otherwise `TOTAL_CAP − PEAK_LOAD`.
- **Canada:** provincial `total capacity − peak load`.

---

## 3. Data inventory

| File | What it is | Notes |
|---|---|---|
| `datacenters.geojson` / `.csv` | Global DC directory (name, company, city, address) | **No MW field.** The GeoJSON has 6,131 geocoded points; the CSV has 18,110 rows. `datacenters.kml` has no attributes and is not used. |
| `im3_open_source_data_center_atlas.csv` | U.S. existing DCs with lat/lon and `sqft` | U.S. only, 1,242 rows, no MW field. |
| `databricks.csv` | U.S. DCs at **state level only** (`TOT_POWER_MW`, `DATACENTER_STAGE`) | No coordinates. Used as a QA/control total per state. Stage split: Active 1,701, Announcement 843, Construction 364. |
| `high_growth_50_market_gravity.geojson` | Modeled 2035 "high" growth siting grid (`data_center_it_power_mw`), 909 polygons, 34 states | CRS is **ESRI:102003**; reproject to WGS84. Only the `high` scenario is present. |
| `Control_Areas.shp` (+`.dbf`) | HIFLD Balancing Authority polygons (71: 70 U.S. + 1 New Brunswick) | **No .prj/.shx.** The CRS was confirmed as **EPSG:3857**. `-999999` is the null sentinel. Fields include `NAME`, `STATE`, `TOTAL_CAP`, `PEAK_LOAD`, `MIN_LOAD`, `AVAIL_CAP`. |
| `tl_2025_us_state.zip` | Census TIGER state boundaries | NAD83 geographic, treated as WGS84. pyshp reads it straight from the zip. The key fields are `STUSPS` and `NAME`. |
| `lpr_000b21a_e.zip` | StatCan provinces/territories (13) | NAD83 Statistics Canada Lambert (≈EPSG:3347). **DBF is latin1.** The key field is `PRENAME`. |
| `GGR442_Datasets.zip` → `electricity-capacity-technology-2026.csv` | CER Canada Energy Future 2026: capacity by province, year, scenario | The `Variable == "Total"` row is provincial capacity in MW. The baseline is the `Current Measures` scenario. |
| `GGR442_Datasets.zip` → `electricity-capacity-2026.csv` | The same by primary fuel | Redundant; use it only for supply-mix narrative. |
| `GGR442_Datasets.zip` → `dataset-links.csv` | Microsoft Global Building Footprint tile URLs | Read `QuadKey` as **str**. All Canada tiles are zoom 9. |
| 24 × `part-*.csv.gz` | MS Building Footprint tiles covering all Canadian DC points (23 + PEI tile `part-00169`) | Line-delimited GeoJSON. Filenames are **not** unique per quadkey (see §5). |
| `2023-data-supplement-figures...xlsx` | CER EF2023 supplement | Only **Figure 8** is used: peak-demand growth ratio 2050/2021 by province and scenario. |
| IESO 2025 APO (PDF) | Ontario Annual Planning Outlook | **The source of the Ontario DC growth figure** (see §6). |
| IESO 2026 APO, AESO 2024 LTO, Hydro-Québec Plan d'action 2035 docs, BC Hydro/FortisBC IRP TAC summary | Context only | None gives a quantified data-center-specific growth figure. Useful for StoryMap narrative. |
| `25100015-eng.zip` (StatCan monthly generation, 2008–2026) | Historical generation by fuel | Not used yet. |

**Available but not yet used:**
- eGRID 2023 (`BA23`, `PLNT23`)
- EPRI 2024 projections (54 BAs, 2022–2040)
- EIA transmission lines shapefile
- `july_generator2026.xlsx`
- CER high-frequency data
- gridstatus.io

---

## 4. Pipeline

### `harmonize_datacenters.py`: North America demand layer and U.S. stress index

1. Load `datacenters.geojson`, filtered to U.S. and Canada (3,099 records), and the IM3 atlas (1,242 records).
2. **Estimate MW:** `sqft × W_PER_SQFT / 1e6`, tagged `mw_method = sqft_derived`. Records without sqft are tagged `unknown`.
3. **Bad-geocode filter:** drop records labeled U.S. or Canada whose coordinates fall outside the North America bounding box. They go to `flagged_bad_geocodes.csv` (8 records).
4. **Spatial dedup:** haversine BallTree with `DEDUP_RADIUS_M`. Within each cluster, keep the row that has MW. Result: 4,333 → about 2,422 points.
5. **QA:** point MW vs. `databricks.csv` state totals, written to `qa_state_mw_totals.csv`.
6. **Hybrid scaling per state:** `adjusted = max(point sum, databricks total)`, and `scale factor = adjusted / point sum`. Written to `adjusted_baseline_load_by_state.csv`.
7. Load `Control_Areas.shp`, reproject EPSG:3857 → WGS84, and convert `-999999` to NaN.
8. **Point-in-polygon joins:** each point is assigned to its BA (`ba_name`), then its state (`joined_state`, from TIGER), then its province (`joined_province`, from StatCan). Written to `harmonized_existing_dc_with_ba.csv`.
9. **BA stress index:** each point is scaled by the factor of **its own state**, then summed per BA and divided by the BA's available capacity. This fixes the MISO/PJM distortion caused by the single `STATE` field. Written to `ba_level_stress_index.csv`, which also has `states_touched` and `unknown_mw_count`.
10. Canada province summary (`canada_dc_by_province.csv`) and the reprojected growth layer (`growth_2035_wgs84.geojson`).

**Key parameters:**

| Parameter | Value | Note |
|---|---|---|
| `W_PER_SQFT` | 150.0 | **Placeholder.** Needs a citable source or Timo's benchmark. |
| `DEDUP_RADIUS_M` | 150.0 | 400 m merged separate buildings on the same campus (1,980 vs. 2,426 points). |
| `NA_BBOX_LAT` | (15, 72) | North America bounding box |
| `NA_BBOX_LON` | (-170, -50) | North America bounding box |

### `canada_footprint_mw.py`: Canadian MW from building footprints

For each Canadian point:
1. Compute its zoom-9 quadkey.
2. Load the matching MS footprint tile into a BallTree over building centroids.
3. Find the nearest building within 200 m.
4. Compute that building's geodesic area with `pyproj.Geod`, convert to sqft, and apply the same W/ft². Tagged `mw_method = footprint_derived`.

When more than one downloaded tile shares a `part-NNNNN` number, the script resolves the right tile **by content**: it reads the file's first coordinate and checks which quadkey it falls in.

Output: `canada_dc_with_mw.csv` and a refreshed `canada_dc_by_province.csv`.

### Done inline in chat, not yet in scripts (consolidate these)

- **Canada 2025 stress index** by province, using the user-supplied peak loads in §6. Output: `canada_stress_index_by_province.csv`.
- **Canada headroom, 2025 vs. 2035.** Output: `canada_headroom_2025_vs_2035.csv`.
  - Capacity comes from CER `Total` rows for 2025 and 2035 under Current Measures.
  - The 2035 peak is `2025 peak × (1 + (Fig8_ratio_2050 − 1) × (2035−2021)/(2050−2021))`, using the Current Measures ratios from `ca_peak_demand_growth_ratio_2050.csv`.
- **Ontario DC growth and 2035 stress index.** Outputs: `ontario_datacenter_growth_ieso.csv` and `canada_stress_index_2025_vs_2035.csv`.
  - `TWh(y) = 3.0 × 1.071^(y−2026)` and `avg MW = TWh × 1e6 / 8760`.
  - The 2035/2026 growth factor is 1.854. It is applied to the point-layer baseline for Ontario only.
- **PEI tile (`part-00169`) and the VisionQuest coordinate fix.** These were patched directly into the output CSVs.

### Reference: quadkey function

```python
def lat_lon_to_quadkey(lat, lon, zoom=9):
    import math
    lat_rad = math.radians(lat)
    x = (lon + 180.0) / 360.0
    y = (1 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2.0
    n = 2 ** zoom
    tx, ty = int(x * n), int(y * n)
    qk = ""
    for i in range(zoom, 0, -1):
        d, m = 0, 1 << (i - 1)
        if tx & m: d += 1
        if ty & m: d += 2
        qk += str(d)
    return qk
```

---

## 5. Gotchas already hit (don't repeat these)

- **QuadKey leading zeros:** pandas parses `QuadKey` as an int and drops the leading zeros. Always read it with `dtype={"QuadKey": str}`. This broke tile matching twice.
- **Duplicate part numbers:** `part-00179` exists for both Calgary (quadkey `021213123`) and Montreal (quadkey `030230333`). The browser named the second download `...__1__...`. Never match tiles by filename alone; resolve by content.
- **Geocoding collisions:** some U.S. city names landed abroad: Birmingham→UK, Dublin OH→Ireland, Reading→UK, Manchester NH→UK, Providence→Rome, and Caledonia MI→the **Eiffel Tower** (48.8566, 2.3522). The bounding-box filter catches these.
- **VisionQuest ATC Data Center** (Charlottetown, PEI) was geocoded to Charlotte, NC, which is inside the bbox and so slipped through.
  - The correct location is 90 University Ave, Charlottetown: **46.23723498843401, -63.12817766145676**, taken from Google Maps.
  - Its footprint result: nearest building 37 m away, 260 m² ≈ 2,803 sqft ≈ **0.42 MW**.
  - **This fix exists only in the output CSVs. Hard-code it into `harmonize_datacenters.py`, or a rerun will revert it.**
- **Single-state BA assumption:** the HIFLD `STATE` field lists one state per BA even for MISO and PJM. Scaling by that one state inflated MISO to 3.19, a false hotspot. It was fixed by the per-point state join.
- **StatCan DBF** needs `shapefile.Reader(path, encoding="latin1")`.
- **Brute-force nearest neighbour** over ~120k buildings per tile timed out. Use a BallTree and compute area only for the winning polygon.
- **Network:** the sandbox couldn't reach `bfppub.z5.web.core.windows.net`, so the team downloaded the tiles by hand.
- **Unmatched points:** 63 of the ~65 points outside every BA polygon are Canadian, because HIFLD has only one Canadian polygon. This is expected.

---

## 6. Key results so far

### U.S. (BA level, current, per-point state scaling)

Top Balancing Authorities by stress index:

| BA | Stress index |
|---|---|
| Public Service Co. of New Mexico | **1.25** |
| Arizona Public Service | 0.99 |
| Bonneville Power Administration | 0.55 |
| Nevada Power | 0.46 |
| Associated Electric Coop | 0.35 |
| PacifiCorp East | 0.35 |
| SC Electric & Gas | 0.30 |
| TVA | 0.27 |
| PJM | 0.235 |
| WAPA Rocky Mountain | 0.23 |

**QA flags:**
- The point layer captures only part of the Databricks state totals. Virginia is about 36% (8,971 vs. 24,836 MW), Texas about 20%, and Louisiana and Indiana about 0%.
- Four states show point sums **above** the Databricks totals: Tennessee (7,014 vs. 245 MW), Oregon, Iowa, and Nebraska. **These need investigating.** The likely cause is the sqft-derived MW running too high, or IM3 counting whole campuses.

### Canada (province level)

**Demand** (64 points, all with footprint-derived MW):

| Province | MW | Sites |
|---|---|---|
| Ontario | 260 | 27 |
| Quebec | 83 | 18 |
| BC | 41 | 10 |
| Alberta | 11 | 2 |
| Nova Scotia | 9 | 4 |
| New Brunswick | 1 | 2 |
| PEI | 0.4 | 1 |

**Peak loads** (user-supplied, approximate system-operator figures). Ontario's is a summer peak; the rest are winter peaks. Footnote this in the methodology.

| Province | Peak (MW) | Season |
|---|---|---|
| Ontario | 25,600 | Summer |
| Quebec | 40,000 | Winter |
| Alberta | 12,200 | Winter |
| BC | 10,500 | Winter |
| New Brunswick | 3,200 | Winter |
| Nova Scotia | 2,300 | Winter |

**Headroom change, 2025 → 2035** (Current Measures):

| Province | Change |
|---|---|
| Ontario | −47% (18,964 → 10,082 MW) |
| New Brunswick | −47% |
| Quebec | −8% |
| Alberta | +38% |
| BC | +103% |
| Nova Scotia | +396% |

**Ontario DC growth** (IESO 2025 APO §2.4.2.1): 3 TWh in 2026 → 16 TWh in 2050, a 7.1% CAGR. That works out to about 5.56 TWh, or about 635 MW average, in 2035.
- The IESO trajectory is applied as a *rate* (×1.854) to the point baseline: 260 → 482 MW. IESO's absolute figure isn't substituted because the two measurement bases differ.

**Canada stress index:**
- **Ontario: 0.0137 (2025) → 0.0478 (2035), +249%.** This is the strongest Canadian hotspot narrative, because demand growth and the supply squeeze point the same way.
- The other provinces are held at their current DC load, since no DC-specific growth source was found; their 2035 change reflects supply alone. Quebec rises 8%, New Brunswick rises 88%, and Nova Scotia, BC, and Alberta ease.
- The Canadian indices are far below the U.S. ones and are **likely underestimates**. Canada has no Databricks-style control total to correct the undercount.

---

## 7. Output files (sent as downloads in the chat)

| Output | Contents |
|---|---|
| `harmonized_existing_dc.csv` / `.geojson` | Deduplicated U.S. and Canada points with MW |
| `harmonized_existing_dc_with_ba.csv` | Points with `ba_name`, `joined_state`, `joined_province`. VisionQuest coordinates patched. |
| `flagged_bad_geocodes.csv` | Records removed by the bbox filter |
| `qa_state_mw_totals.csv` | Point MW vs. Databricks by state |
| `adjusted_baseline_load_by_state.csv` | `max(point, databricks)` per state, with the source used |
| `ba_level_stress_index.csv` | U.S. BA stress index |
| `growth_2035_wgs84.geojson` | Reprojected 2035 growth-gravity layer |
| `canada_building_footprint_tiles_needed.csv` | The MS footprint tiles needed |
| `canada_dc_with_mw.csv` / `canada_dc_by_province.csv` | Canadian points with footprint MW, plus province totals |
| `canada_stress_index_by_province.csv` | Canada 2025 stress index |
| `ca_peak_demand_growth_ratio_2050.csv` | CER Figure 8 ratios |
| `canada_headroom_2025_vs_2035.csv` | Provincial headroom, 2025 vs. 2035 |
| `ontario_datacenter_growth_ieso.csv` | IESO-based trajectory, 2026–2050 |
| `canada_stress_index_2025_vs_2035.csv` | Combined Canada stress index, 2025 vs. 2035 |

---

## 8. Next steps (priority order)

1. **U.S. 2035 time series.** This is the biggest remaining gap, and the year slider depends on it.
   - Add the `growth_2035_wgs84.geojson` load: join grid cells to BAs with point-in-polygon, then sum `data_center_it_power_mw`.
   - Add the EPRI 2024 BA-level projections as well, or use them instead.
   - Project BA capacity and peak to 2035 using `july_generator2026.xlsx` (planned and retired units) and EPRI.
   - Output yearly values for 2025–2035.
2. **Consolidate** the inline steps (§4) into scripts, and **hard-code the VisionQuest fix** into the harmonization script.
3. **Validation:**
   - Source or confirm `W_PER_SQFT` with Timo.
   - Investigate the Tennessee, Oregon, Iowa, and Nebraska overcounts.
   - Backfill the major Louisiana and Indiana sites.
   - Cross-check HIFLD `TOTAL_CAP`/`PEAK_LOAD` against eGRID `BA23`; 7 BAs are null.
   - Consider using the Databricks `DATACENTER_STAGE` split to phase growth over time.
4. **Confirm scope with Timo.** Proposed: full quantitative treatment for the U.S. and Ontario, and qualitative StoryMap treatment for Quebec, Alberta, BC, and the Atlantic provinces. Also ask whether Mexico is in scope.
5. **ArcGIS Pro.** Import the outputs and run Optimized Hot Spot Analysis or Getis-Ord Gi* on the stress index, per year once the data is time-enabled.
6. **Deliverables.**
   - Experience Builder dashboard: year slider, diverging color ramp on the stress index, plant and transmission context layers.
   - StoryMap. Narrative anchors: Ontario, PNM/APS, PJM, the Canada/U.S. data asymmetry, and methodology limitations.

---

## 9. Environment notes for Claude Code

- **Python libraries:** `pandas`, `numpy`, `pyproj`, `shapely`, `pyshp` (imported as `shapefile`), `scikit-learn` (for BallTree). Install with `pip install pandas numpy pyproj shapely pyshp scikit-learn`.
- **Paths:** the scripts hard-code `DATA_DIR = /mnt/user-data/uploads` and `OUT_DIR = /home/claude/output`. Change them to `./data` and `./output`.
- **Suggested first prompt in Claude Code:**
  > "Read PROJECT_HANDOFF.md. Put the scripts in the repo root, the raw data in `./data`, and outputs in `./output`. Update paths, hard-code the VisionQuest fix, re-run the pipeline to confirm the outputs match §6, then start Next Step 1 (U.S. 2035 time series)."
