"""
us_time_series.py

U.S. Balancing Authority Grid Headroom Stress Index, yearly 2025-2035
(PROJECT_HANDOFF.md §8 step 1), in long format for an ArcGIS Experience
Builder year slider.

Demand
  DC_LOAD(y) = existing 2025 load + new 2035 load x ramp(y)
  - existing: per-BA scaled load from harmonize_datacenters.py (point layer
    scaled to Databricks Active state totals).
  - new: IM3 projected sites (high growth, 50% market gravity) -- NEW facilities
    through 2035 -- joined to BAs by cell centroid, IT MW x GROWTH_PUE.
  - ramp(y) = ((1+r)^(y-2025) - 1) / ((1+r)^10 - 1) with r the scenario's own
    annual growth rate, so 2025 = 0 and 2035 = 1.

Supply
  HIFLD capacity/peak are 2020-vintage. TOTAL_CAP is rolled forward with the
  EIA-860M July 2026 inventory, generators assigned to BAs by EIA's own BA code
  (config.EIA_BA_CODE_TO_HIFLD) -- not by location, since BA polygons overlap:
  + units that came online after the HIFLD year (Operating and Retired sheets)
  + planned units with approvals or under construction (EIA_PLANNED_STATUSES)
  - units retired after the HIFLD year (Retired sheet)
  - operating units with a planned retirement year from 2026 on
  Capacity is EIA net summer MW, not derated for solar/wind availability.
  PEAK_LOAD grows at NON_DC_PEAK_GROWTH from the HIFLD year.

Stress
  AVAIL_CAP = TOTAL_CAP - PEAK_LOAD  (headroom; same basis as canada_stress.py)
  STRESS_INDEX = DC_LOAD / AVAIL_CAP when AVAIL_CAP > 0, else null with
  STRESS_CLASS = "no_headroom".

Writes to output/:
  us_ba_time_series_2025_2035.csv      master table (one row per BA per year)
  us_ba_capacity_changes.csv           per-BA roll-forward detail, for QA
  us_generator_ba_qa.csv               EIA operating MW vs HIFLD TOTAL_CAP per BA
"""

import json

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape as shapely_shape

from config import (
    EIA860M_XLSX, EIA_BA_CODE_TO_HIFLD, EIA_CAPACITY_COLUMN, EIA_PLANNED_STATUSES, GROWTH_PUE,
    GROWTH_SCENARIO_ANNUAL_RATE, NON_DC_PEAK_GROWTH, OUT_DIR, TS_YEARS,
)
from geo import join_ba, load_control_areas

FIRST_YEAR, LAST_YEAR = TS_YEARS[0], TS_YEARS[-1]
EIA_FIRST_FORWARD_YEAR = 2026   # planned retirements/additions count from here on


# ----------------------------------------------------------------------------
# Demand
# ----------------------------------------------------------------------------
def existing_load_by_ba() -> pd.Series:
    ba = pd.read_csv(OUT_DIR / "ba_level_stress_index.csv")
    return ba.set_index("ba_name")["scaled_dc_load_mw"]


def new_load_by_ba(control_areas) -> tuple[pd.Series, pd.DataFrame]:
    with open(OUT_DIR / "growth_2035_wgs84.geojson", encoding="utf-8") as f:
        gj = json.load(f)
    cells = pd.DataFrame([feat["properties"] for feat in gj["features"]])
    centroids = shapely.centroid([shapely_shape(feat["geometry"]) for feat in gj["features"]])
    cells["lon"], cells["lat"] = shapely.get_x(centroids), shapely.get_y(centroids)
    cells["ba_name"] = join_ba(cells["lat"], cells["lon"], control_areas)
    cells["new_load_mw"] = cells["data_center_it_power_mw"] * GROWTH_PUE
    unmatched = cells["ba_name"].isna()
    if unmatched.any():
        print(f"  WARNING: {unmatched.sum()} growth cells ({cells.loc[unmatched, 'new_load_mw'].sum():.0f} MW) "
              "fall outside every BA polygon")
    return cells.groupby("ba_name")["new_load_mw"].sum(), cells


def ramp(year: int) -> float:
    r = GROWTH_SCENARIO_ANNUAL_RATE
    return ((1 + r) ** (year - FIRST_YEAR) - 1) / ((1 + r) ** (LAST_YEAR - FIRST_YEAR) - 1)


# ----------------------------------------------------------------------------
# Supply
# ----------------------------------------------------------------------------
def read_eia_sheet(sheet: str) -> pd.DataFrame:
    df = pd.read_excel(EIA860M_XLSX, sheet_name=sheet, header=2)
    df = df[pd.to_numeric(df["Plant ID"], errors="coerce").notna()].copy()   # drop footnote rows
    df["mw"] = pd.to_numeric(df[EIA_CAPACITY_COLUMN], errors="coerce").fillna(0.0)
    for col in ["Operating Year", "Planned Operation Year", "Retirement Year", "Planned Retirement Year"]:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")
    return df


def load_generators() -> dict[str, pd.DataFrame]:
    print("  reading EIA-860M (Operating / Planned / Retired) ...")
    sheets = {s: read_eia_sheet(s) for s in ["Operating", "Planned", "Retired"]}
    for name, df in sheets.items():
        df["ba_name"] = df["Balancing Authority Code"].map(EIA_BA_CODE_TO_HIFLD)
        unmapped = df[df["ba_name"].isna()]
        print(f"    {name:<9} {len(df):>6} units, {df['mw'].sum():>9,.0f} MW; "
              f"{unmapped['mw'].sum():,.0f} MW with no HIFLD BA "
              f"({', '.join(unmapped['Balancing Authority Code'].dropna().astype(str).unique()[:6]) or 'none'})")
    return sheets


def capacity_changes(sheets: dict, ba_base_year: pd.Series) -> pd.DataFrame:
    """Cumulative MW added and retired per BA, from each BA's HIFLD year to each TS year."""
    op, pl, rt = sheets["Operating"], sheets["Planned"], sheets["Retired"]
    pl = pl[pl["Status"].astype(str).str.startswith(EIA_PLANNED_STATUSES)]

    # (ba, year the MW arrives/leaves, MW)
    additions = pd.concat([
        op[["ba_name", "Operating Year", "mw"]].rename(columns={"Operating Year": "year"}),
        rt[["ba_name", "Operating Year", "mw"]].rename(columns={"Operating Year": "year"}),
        pl[["ba_name", "Planned Operation Year", "mw"]].rename(columns={"Planned Operation Year": "year"}),
    ])
    op_ret = op[op["Planned Retirement Year"] >= EIA_FIRST_FORWARD_YEAR]
    retirements = pd.concat([
        rt[["ba_name", "Retirement Year", "mw"]].rename(columns={"Retirement Year": "year"}),
        op_ret[["ba_name", "Planned Retirement Year", "mw"]].rename(columns={"Planned Retirement Year": "year"}),
    ])

    rows = []
    for ba, base in ba_base_year.items():
        add_ba = additions[additions["ba_name"] == ba]
        ret_ba = retirements[retirements["ba_name"] == ba]
        for year in TS_YEARS:
            rows.append({
                "ba_name": ba, "year": year,
                "cap_added_mw": add_ba.loc[(add_ba["year"] > base) & (add_ba["year"] <= year), "mw"].sum(),
                "cap_retired_mw": ret_ba.loc[(ret_ba["year"] > base) & (ret_ba["year"] <= year), "mw"].sum(),
            })
    return pd.DataFrame(rows)


def generator_qa(sheets: dict, bas: pd.DataFrame) -> pd.DataFrame:
    """EIA July-2026 operating MW per BA next to HIFLD's 2020 TOTAL_CAP. Only the
    change is applied to HIFLD, but a large ratio flags BAs whose HIFLD
    capacity is defined differently (e.g. owned/contracted rather than all
    plants in the BA)."""
    eia = sheets["Operating"].groupby("ba_name")["mw"].sum()
    qa = pd.DataFrame({"hifld_total_cap_mw": bas["TOTAL_CAP"], "eia_operating_mw_2026": eia}).dropna(how="all")
    qa["ratio_eia_to_hifld"] = qa["eia_operating_mw_2026"] / qa["hifld_total_cap_mw"].where(qa["hifld_total_cap_mw"] > 0)
    return qa.sort_values("eia_operating_mw_2026", ascending=False).reset_index(names="ba_name")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def classify(stress: float, avail: float, has_cap: bool) -> str:
    if not has_cap:
        return "no_capacity_data"
    if avail <= 0:
        return "no_headroom"
    if stress > 1.0:
        return "gap"
    if stress >= 0.9:
        return "near_limit"
    return "surplus"


def main():
    control_areas = load_control_areas()
    recs = control_areas[0]
    bas = recs[recs["COUNTRY"] == "USA"].set_index("NAME")
    base_year = bas["YEAR"].fillna(2020).astype(int)
    print(f"{len(bas)} U.S. BAs; HIFLD capacity/peak vintage: {base_year.value_counts().to_dict()}")

    print("Demand ...")
    existing = existing_load_by_ba()
    new_2035, cells = new_load_by_ba(control_areas)
    print(f"  existing 2025 load: {existing.reindex(bas.index).sum():,.0f} MW; "
          f"new by 2035: {new_2035.sum():,.0f} MW across {new_2035.size} BAs")

    print("Supply ...")
    sheets = load_generators()
    changes = capacity_changes(sheets, base_year)
    changes.to_csv(OUT_DIR / "us_ba_capacity_changes.csv", index=False)
    generator_qa(sheets, bas).to_csv(OUT_DIR / "us_generator_ba_qa.csv", index=False)

    rows = []
    for _, ch in changes.iterrows():
        ba, year = ch["ba_name"], ch["year"]
        rec = bas.loc[ba]
        has_cap = pd.notna(rec["TOTAL_CAP"]) and rec["TOTAL_CAP"] > 0 and pd.notna(rec["PEAK_LOAD"])
        dc_existing = float(existing.get(ba, 0.0))
        dc_new = float(new_2035.get(ba, 0.0)) * ramp(year)
        dc = dc_existing + dc_new
        if has_cap:
            cap = rec["TOTAL_CAP"] + ch["cap_added_mw"] - ch["cap_retired_mw"]
            peak = rec["PEAK_LOAD"] * (1 + NON_DC_PEAK_GROWTH) ** (year - base_year[ba])
            avail = cap - peak
            stress = dc / avail if avail > 0 else np.nan
        else:
            cap = peak = avail = stress = np.nan
        rows.append({
            "BA_NAME": ba,
            "BA_ID": rec["ID"],
            "YEAR": year,
            "YEAR_DATE": f"{year}-01-01",
            "DC_LOAD_MW": dc,
            "TOTAL_CAP_MW": cap,
            "PEAK_LOAD_MW": peak,
            "AVAIL_CAP_MW": avail,
            "STRESS_INDEX": stress,
            "STRESS_CLASS": classify(stress, avail, has_cap),
            "DC_EXISTING_MW": dc_existing,
            "DC_NEW_MW": dc_new,
            "CAP_ADDED_MW": ch["cap_added_mw"],
            "CAP_RETIRED_MW": ch["cap_retired_mw"],
        })
    ts = pd.DataFrame(rows)
    ts.to_csv(OUT_DIR / "us_ba_time_series_2025_2035.csv", index=False, float_format="%.4f")
    print(f"Wrote us_ba_time_series_2025_2035.csv: {len(ts)} rows ({ts['BA_NAME'].nunique()} BAs x {len(TS_YEARS)} years)")

    for year in (FIRST_YEAR, LAST_YEAR):
        top = (ts[ts["YEAR"] == year].sort_values("STRESS_INDEX", ascending=False).head(10)
               [["BA_NAME", "DC_LOAD_MW", "AVAIL_CAP_MW", "STRESS_INDEX"]])
        print(f"\nTop 10 BAs by stress index, {year}:")
        print(top.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    no_head = ts[(ts["YEAR"] == LAST_YEAR) & (ts["STRESS_CLASS"] == "no_headroom")]["BA_NAME"].tolist()
    if no_head:
        print(f"\nNo headroom in {LAST_YEAR} (capacity <= peak): {no_head}")


if __name__ == "__main__":
    main()
