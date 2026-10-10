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
  + from TREND_START_YEAR, a top-up so each BA adds at least its own 2021-2025
    average build rate per year (EIA's pipeline thins out after ~3 years);
    the pipeline-only figures are kept as a sensitivity
  Capacity is EIA net summer MW, not derated for solar/wind availability.

Peak
  System peak = EIA-930 2025 hourly peak (adjusted demand, spike-filtered)
  where the EIA-930 BA footprint matches HIFLD's (PEAK_SCOPE_RATIO); otherwise
  HIFLD's own peak. PEAK_LOAD_MW is the *baseline (non-data-centre)* peak:
  system peak - existing DC load, grown at NON_DC_PEAK_GROWTH. Subtracting the
  existing DC load keeps it from being counted twice (numerator + peak).

Stress (PROJECT_HANDOFF.md §2)
  AVAIL_CAP = TOTAL_CAP - PEAK_LOAD  (headroom left for data centres)
  STRESS_INDEX = DC_LOAD / AVAIL_CAP, so STRESS_INDEX > 1 exactly when
  capacity can't cover the system peak (baseline + DC). Null with
  STRESS_CLASS = "no_headroom" when AVAIL_CAP <= 0.
  DATA_ARTIFACT_FLAG marks BAs whose values reflect data definitions rather
  than grid conditions (config.DATA_ARTIFACT_BAS + HIFLD capacity below the
  BA's own 2025 peak); STRESS_CLASS = "data_artifact" for those.

Writes to output/:
  us_ba_time_series_2025_2035.csv      master table (one row per BA per year)
  us_ba_capacity_changes.csv           per-BA roll-forward detail, for QA
  us_generator_ba_qa.csv               EIA operating MW vs HIFLD TOTAL_CAP per BA
  us_ba_peak_2025.csv                  EIA-930 vs HIFLD peak per BA and which was used
"""

import json

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape as shapely_shape

from config import (
    DATA_ARTIFACT_BAS, EIA860M_XLSX, EIA930_FILES, EIA_BA_CODE_TO_HIFLD, EIA_CAPACITY_COLUMN,
    EIA_PLANNED_STATUSES, GROWTH_PUE, GROWTH_SCENARIO_ANNUAL_RATE, NON_DC_PEAK_GROWTH, OUT_DIR,
    PEAK_SCOPE_RATIO, PEAK_SPIKE_FACTOR, TREND_BUILD_FACTOR, TREND_HISTORY_YEARS, TREND_START_YEAR,
    TS_YEARS,
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

    # recent build rate: units that came online in TREND_HISTORY_YEARS (whether
    # still operating or since retired), averaged per year
    h0, h1 = TREND_HISTORY_YEARS
    built = additions.iloc[: len(op) + len(rt)]
    built = built[built["year"].between(h0, h1)]
    build_rate = built.groupby("ba_name")["mw"].sum() / (h1 - h0 + 1) * TREND_BUILD_FACTOR
    committed = pl.groupby(["ba_name", "Planned Operation Year"])["mw"].sum()

    rows = []
    for ba, base in ba_base_year.items():
        add_ba = additions[additions["ba_name"] == ba]
        ret_ba = retirements[retirements["ba_name"] == ba]
        rate = float(build_rate.get(ba, 0.0))
        trend_cum = 0.0
        for year in TS_YEARS:
            if year >= TREND_START_YEAR:
                trend_cum += max(0.0, rate - float(committed.get((ba, year), 0.0)))
            rows.append({
                "ba_name": ba, "year": year,
                "cap_added_mw": add_ba.loc[(add_ba["year"] > base) & (add_ba["year"] <= year), "mw"].sum(),
                "cap_retired_mw": ret_ba.loc[(ret_ba["year"] > base) & (ret_ba["year"] <= year), "mw"].sum(),
                "cap_trend_added_mw": trend_cum,
                "recent_build_rate_mw_yr": rate,
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
# Peak
# ----------------------------------------------------------------------------
def eia930_peaks() -> pd.DataFrame:
    """2025 hourly peak per EIA BA code from EIA-930 adjusted demand."""
    col = "Demand (MW) (Adjusted)"
    demand = pd.concat([pd.read_csv(f, usecols=["Balancing Authority", col], thousands=",")
                        for f in EIA930_FILES])
    out = []
    for code, s_ in demand.groupby("Balancing Authority")[col]:
        s_ = s_.dropna()
        if s_.empty:
            continue
        p999 = s_.quantile(0.999)
        kept = s_[s_ <= PEAK_SPIKE_FACTOR * p999]
        out.append({"eia_code": code, "eia930_peak_2025_mw": kept.max(),
                    "spike_hours_dropped": int(len(s_) - len(kept))})
    return pd.DataFrame(out)


def system_peaks(bas: pd.DataFrame, base_year: pd.Series) -> pd.DataFrame:
    """Per HIFLD BA: the system peak used, its year and source."""
    eia = eia930_peaks()
    eia["ba_name"] = eia["eia_code"].map(EIA_BA_CODE_TO_HIFLD)
    eia = eia.dropna(subset=["ba_name"]).groupby("ba_name").agg(
        eia930_peak_2025_mw=("eia930_peak_2025_mw", "sum"),
        spike_hours_dropped=("spike_hours_dropped", "sum"))
    peaks = pd.DataFrame({"hifld_peak_mw": bas["PEAK_LOAD"], "hifld_year": base_year}).join(eia)
    ratio = peaks["eia930_peak_2025_mw"] / peaks["hifld_peak_mw"]
    lo, hi = PEAK_SCOPE_RATIO
    use_930 = ratio.between(lo, hi)
    peaks["ratio_930_to_hifld"] = ratio
    peaks["system_peak_mw"] = np.where(use_930, peaks["eia930_peak_2025_mw"], peaks["hifld_peak_mw"])
    peaks["peak_year"] = np.where(use_930, 2025, peaks["hifld_year"])
    peaks["peak_source"] = np.select(
        [use_930, peaks["eia930_peak_2025_mw"].notna()],
        ["EIA-930 2025", "HIFLD (EIA-930 footprint differs)"], "HIFLD (not in EIA-930)")
    return peaks


def artifact_reasons(bas: pd.DataFrame, peaks: pd.DataFrame, changes: pd.DataFrame) -> dict:
    """config.DATA_ARTIFACT_BAS plus any BA whose rolled-forward 2025 capacity is
    below its own 2025 system peak -- the lights stayed on, so that capacity
    figure can't describe the whole system."""
    reasons = dict(DATA_ARTIFACT_BAS)
    c25 = changes[changes["year"] == FIRST_YEAR].set_index("ba_name")
    for ba in bas.index:
        base_cap, peak = bas.loc[ba, "TOTAL_CAP"], peaks.loc[ba, "system_peak_mw"]
        if ba in reasons or pd.isna(base_cap) or base_cap <= 0 or pd.isna(peak):
            continue
        cap25 = base_cap + c25.loc[ba, "cap_added_mw"] - c25.loc[ba, "cap_retired_mw"]
        if cap25 < peak:
            reasons[ba] = (f"2025 capacity ({cap25:,.0f} MW, HIFLD rolled forward) is below "
                           f"the BA's own 2025 peak ({peak:,.0f} MW)")
    return reasons


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def classify(stress: float, avail: float, has_cap: bool, artifact: bool) -> str:
    if not has_cap:
        return "no_capacity_data"
    if artifact:
        return "data_artifact"
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
          f"new by 2035: {new_2035.sum():,.0f} MW (IT x PUE {GROWTH_PUE}) across {new_2035.size} BAs")

    print("Peak ...")
    peaks = system_peaks(bas, base_year)
    peaks.to_csv(OUT_DIR / "us_ba_peak_2025.csv", index_label="ba_name")
    print("  " + ", ".join(f"{k}: {v}" for k, v in peaks["peak_source"].value_counts().items()))

    print("Supply ...")
    sheets = load_generators()
    changes = capacity_changes(sheets, base_year)
    changes.to_csv(OUT_DIR / "us_ba_capacity_changes.csv", index=False)
    generator_qa(sheets, bas).to_csv(OUT_DIR / "us_generator_ba_qa.csv", index=False)
    artifacts = artifact_reasons(bas, peaks, changes)

    rows = []
    for _, ch in changes.iterrows():
        ba, year = ch["ba_name"], ch["year"]
        rec, pk = bas.loc[ba], peaks.loc[ba]
        has_cap = pd.notna(rec["TOTAL_CAP"]) and rec["TOTAL_CAP"] > 0 and pd.notna(pk["system_peak_mw"])
        dc_existing = float(existing.get(ba, 0.0))
        dc_new = float(new_2035.get(ba, 0.0)) * ramp(year)
        dc = dc_existing + dc_new
        if has_cap:
            cap_pipeline = rec["TOTAL_CAP"] + ch["cap_added_mw"] - ch["cap_retired_mw"]
            cap = cap_pipeline + ch["cap_trend_added_mw"]
            baseline = max(pk["system_peak_mw"] - dc_existing, 0.0)
            peak = baseline * (1 + NON_DC_PEAK_GROWTH) ** max(0, year - pk["peak_year"])
            avail = cap - peak
            stress = dc / avail if avail > 0 else np.nan
            avail_pipeline = cap_pipeline - peak
            stress_pipeline = dc / avail_pipeline if avail_pipeline > 0 else np.nan
        else:
            cap = peak = avail = stress = cap_pipeline = stress_pipeline = np.nan
        artifact = ba in artifacts
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
            "STRESS_CLASS": classify(stress, avail, has_cap, artifact),
            "DATA_ARTIFACT_FLAG": artifact,
            "ARTIFACT_REASON": artifacts.get(ba, ""),
            "SYSTEM_PEAK_MW": peak + dc if has_cap else np.nan,
            "PEAK_SOURCE": pk["peak_source"],
            "DC_EXISTING_MW": dc_existing,
            "DC_NEW_MW": dc_new,
            "CAP_ADDED_MW": ch["cap_added_mw"],
            "CAP_RETIRED_MW": ch["cap_retired_mw"],
            "CAP_TREND_ADDED_MW": ch["cap_trend_added_mw"],
            "TOTAL_CAP_PIPELINE_ONLY_MW": cap_pipeline,
            "STRESS_INDEX_PIPELINE_ONLY": stress_pipeline,
        })
    ts = pd.DataFrame(rows)
    ts.to_csv(OUT_DIR / "us_ba_time_series_2025_2035.csv", index=False, float_format="%.4f")
    print(f"Wrote us_ba_time_series_2025_2035.csv: {len(ts)} rows ({ts['BA_NAME'].nunique()} BAs x {len(TS_YEARS)} years)")
    print(f"Data-artifact BAs: {sorted(artifacts)}")

    valid = ts[~ts["DATA_ARTIFACT_FLAG"]]
    for year in (FIRST_YEAR, LAST_YEAR):
        top = (valid[valid["YEAR"] == year].sort_values("STRESS_INDEX", ascending=False).head(10)
               [["BA_NAME", "DC_LOAD_MW", "AVAIL_CAP_MW", "STRESS_INDEX"]])
        print(f"\nTop 10 BAs by stress index, {year} (artifacts excluded):")
        print(top.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    print("\n2035 classes:", valid[valid["YEAR"] == LAST_YEAR]["STRESS_CLASS"].value_counts().to_dict())
    y35 = valid[valid["YEAR"] == LAST_YEAR]
    print(f"BAs over 1.0 in {LAST_YEAR}: trend scenario {int((y35['STRESS_INDEX'] > 1).sum())}, "
          f"pipeline-only {int((y35['STRESS_INDEX_PIPELINE_ONLY'] > 1).sum())}")


if __name__ == "__main__":
    main()
