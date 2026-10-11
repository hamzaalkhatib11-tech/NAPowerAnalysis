"""
canada_stress.py

Canadian provincial Grid Headroom Stress Index, yearly 2025-2035, on the same
basis as the U.S. BA index.

  - Capacity: CER Canada Energy Future 2026, Variable == "Total", Current Measures.
  - 2025 system peak: system-operator peaks (config.CANADA_PEAK_LOAD_2025_MW).
  - Baseline (non-data-centre) peak = system peak - existing DC load, grown to
    the CER EF2023 Fig. 8 ratio by 2035 and interpolated linearly in between.
  - DC load = existing + new:
      existing  max(footprint-derived provincial total, config floor)
                (IESO: Toronto 293 MW; Hydro-Quebec: 190 MW at peak)
      new       Ontario: IESO 2025 APO data-centre energy growth since 2026 (exact
                table, Demand Forecast Module Fig. 11) / ONTARIO_DC_LOAD_FACTOR;
                others: config.CANADA_DC_NEW_REF_MW --
                Hydro-Quebec 1,000 MW by 2035 (QC), AESO Phase I 1,200 MW (AB),
                BC legislated allocation caps (BC); held flat where no source
      high case config.CANADA_DC_NEW_HIGH_MW (all proposed / requested load)

  AVAIL_CAP = capacity - baseline peak;  STRESS_INDEX = DC load / AVAIL_CAP.

Writes to output/:
  ca_peak_demand_growth_ratio_2050.csv
  canada_stress_index_by_province.csv       (2025)
  canada_headroom_2025_vs_2035.csv
  ontario_datacenter_growth_ieso.csv        (IESO 2025 APO energy path, context only)
  canada_stress_index_2025_vs_2035.csv
  canada_stress_index_timeseries.csv        (yearly 2025-2035, for the year slider)
"""

import pandas as pd

from config import (
    BASE_YEAR, CANADA_ARTIFACT_PROVINCES, CANADA_DC_EXISTING_FLOOR_MW, IESO_APO_DFM_XLSB, ONTARIO_DC_LOAD_FACTOR, CANADA_DC_NEW_HIGH_MW, CANADA_DC_NEW_REF_MW,
    CANADA_PEAK_LOAD_2025_MW, CER_CAPACITY_CSV, CER_EF2023_SUPPLEMENT_XLSX, CER_SCENARIO,
    FIG8_BASE_YEAR, FIG8_END_YEAR, HOURS_PER_YEAR, IESO_BASE_YEAR, IESO_DC_CAGR, IESO_DC_TWH_2026,
    OUT_DIR, PROVINCE_ABBREV, TARGET_YEAR,
)

SOURCES = {
    "Ontario": "IESO 2025 APO data-centre energy forecast (Demand Forecast Module Fig. 11), "
               "converted at a 0.70 load factor",
    "Quebec": "Hydro-Quebec: 190 MW at peak today, up to 1,000 MW by 2035",
    "Alberta": "AESO Large Load Phase I: 1,200 MW (Meta 970, Keephills 230), 2027-28",
    "British Columbia": "BC data-centre allocation caps (400 MW 2026-28, 200 MW 2028-29)",
}
FLAT_SOURCE = "no data-centre growth source found; held at existing load"


def load_fig8_ratios() -> pd.DataFrame:
    raw = pd.read_excel(CER_EF2023_SUPPLEMENT_XLSX, sheet_name="Figure 8", header=None, skiprows=3)
    ratios = raw.iloc[:, [0, 2, 4]].dropna()
    ratios.columns = ["scenario", "province_abbrev", "ratio_2050_over_2021"]
    ratios["ratio_2050_over_2021"] = ratios["ratio_2050_over_2021"].astype(float)
    return ratios.reset_index(drop=True)


def load_cer_capacity(years) -> pd.DataFrame:
    cer = pd.read_csv(CER_CAPACITY_CSV)
    total = cer[(cer["Variable"] == "Total") & (cer["Scenario"] == CER_SCENARIO) & cer["Year"].isin(years)]
    return total.pivot(index="Region", columns="Year", values="Value")


def projected_peak(peak_base: float, ratio_2050: float, year: int) -> float:
    """2035 target: the 2025 peak x the CER 2021->2050 growth ratio interpolated to
    2035 (the handoff's formula). Years in between are interpolated linearly from
    the 2025 peak to that target -- applying the 2021-based fraction to each year
    directly would make every year after 2025 jump by four years' growth."""
    frac = (TARGET_YEAR - FIG8_BASE_YEAR) / (FIG8_END_YEAR - FIG8_BASE_YEAR)
    target = peak_base * (1 + (ratio_2050 - 1) * frac)
    return peak_base + (target - peak_base) * (year - BASE_YEAR) / (TARGET_YEAR - BASE_YEAR)


def existing_dc(province: str, footprint_mw: float) -> float:
    return max(footprint_mw, CANADA_DC_EXISTING_FLOOR_MW.get(province, 0.0))


def ontario_apo_new_mw() -> dict | None:
    """{year: MW} of new Ontario data-centre load from IESO's 2025 APO table:
    net annual energy growth since the start of 2026, converted to MW with
    ONTARIO_DC_LOAD_FACTOR. None if the workbook isn't in data/."""
    if not IESO_APO_DFM_XLSB.exists():
        return None
    sheet = pd.read_excel(IESO_APO_DFM_XLSB, sheet_name="Figure 11", header=None, engine="pyxlsb")
    years = sheet.iloc[8, 4:].dropna().astype(int).tolist()
    row = sheet[sheet.iloc[:, 2].astype(str).str.strip() == "Data Centres"].iloc[0, 4:4 + len(years)]
    twh = dict(zip(years, row.astype(float)))
    out = {BASE_YEAR: 0.0}
    out.update({y: max(t, 0.0) * 1e6 / HOURS_PER_YEAR / ONTARIO_DC_LOAD_FACTOR for y, t in twh.items()})
    return out


ONTARIO_APO = ontario_apo_new_mw()


def new_dc(province: str, year: int, high: bool = False) -> float:
    if province == "Ontario" and not high and ONTARIO_APO is not None:
        return float(ONTARIO_APO.get(year, 0.0))
    table = (CANADA_DC_NEW_HIGH_MW if high else CANADA_DC_NEW_REF_MW).get(province)
    if table is None and high:
        table = CANADA_DC_NEW_REF_MW.get(province)
    return float(table.get(year, 0.0)) if table else 0.0


def ieso_dc_twh(year: int) -> float:
    return IESO_DC_TWH_2026 * (1 + IESO_DC_CAGR) ** (year - IESO_BASE_YEAR)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ratios = load_fig8_ratios()
    ratios.to_csv(OUT_DIR / "ca_peak_demand_growth_ratio_2050.csv", index=False)
    cm_ratio = (ratios[ratios["scenario"] == CER_SCENARIO]
                .set_index("province_abbrev")["ratio_2050_over_2021"])

    footprint = pd.read_csv(OUT_DIR / "canada_dc_by_province.csv").set_index("joined_province")["known_mw_total"]
    years = list(range(BASE_YEAR, TARGET_YEAR + 1))
    capacity = load_cer_capacity(years)

    rows = []
    for prov, system_peak in CANADA_PEAK_LOAD_2025_MW.items():
        ratio = cm_ratio[PROVINCE_ABBREV[prov]]
        existing = existing_dc(prov, float(footprint.get(prov, 0.0)))
        baseline_2025 = system_peak - existing
        for year in years:
            baseline = baseline_2025 if year == BASE_YEAR else projected_peak(baseline_2025, ratio, year)
            cap = capacity.loc[prov, year]
            avail = cap - baseline
            new, new_high = new_dc(prov, year), new_dc(prov, year, high=True)
            load, load_high = existing + new, existing + new_high
            rows.append({
                "province": prov, "year": year,
                "dc_load_mw": load, "dc_existing_mw": existing, "dc_new_mw": new,
                "total_cap_mw": cap, "peak_load_mw": baseline, "system_peak_mw": baseline + load,
                "avail_cap_mw": avail,
                "stress_index": load / avail if avail > 0 else float("nan"),
                "dc_load_high_mw": load_high,
                "stress_index_high": load_high / avail if avail > 0 else float("nan"),
                "footprint_mw": float(footprint.get(prov, 0.0)),
                "dc_growth_source": SOURCES.get(prov, FLAT_SOURCE),
                "data_artifact_flag": prov in CANADA_ARTIFACT_PROVINCES,
                "artifact_reason": CANADA_ARTIFACT_PROVINCES.get(prov, ""),
            })
    ts = pd.DataFrame(rows)
    ts.to_csv(OUT_DIR / "canada_stress_index_timeseries.csv", index=False)

    y0 = ts[ts["year"] == BASE_YEAR].set_index("province")
    y1 = ts[ts["year"] == TARGET_YEAR].set_index("province")

    (y0[["dc_load_mw", "total_cap_mw", "peak_load_mw", "avail_cap_mw", "stress_index"]]
     .sort_values("stress_index", ascending=False).reset_index()
     .to_csv(OUT_DIR / "canada_stress_index_by_province.csv", index=False))

    headroom = pd.DataFrame({
        "peak_load_2025_mw": y0["peak_load_mw"],
        "peak_load_2035_proj_mw": y1["peak_load_mw"],
        "total_cap_2025_mw": y0["total_cap_mw"],
        "total_cap_2035_mw": y1["total_cap_mw"],
        "avail_cap_2025_mw": y0["avail_cap_mw"],
        "avail_cap_2035_mw": y1["avail_cap_mw"],
        "headroom_change_pct": 100 * (y1["avail_cap_mw"] / y0["avail_cap_mw"] - 1),
    }).sort_values("headroom_change_pct").reset_index(names="province")
    headroom.to_csv(OUT_DIR / "canada_headroom_2025_vs_2035.csv", index=False)

    ieso = pd.DataFrame({"year": range(IESO_BASE_YEAR, FIG8_END_YEAR + 1)})
    ieso["ieso_dc_demand_twh"] = ieso["year"].map(ieso_dc_twh)
    ieso["ieso_dc_avg_mw"] = ieso["ieso_dc_demand_twh"] * 1e6 / HOURS_PER_YEAR
    ieso.to_csv(OUT_DIR / "ontario_datacenter_growth_ieso.csv", index=False)

    combined = pd.DataFrame({
        "dc_load_2025_mw": y0["dc_load_mw"],
        "stress_index_2025": y0["stress_index"],
        "dc_load_2035_mw": y1["dc_load_mw"],
        "avail_cap_2035_mw": y1["avail_cap_mw"],
        "stress_index_2035": y1["stress_index"],
        "stress_index_pct_change": 100 * (y1["stress_index"] / y0["stress_index"] - 1),
        "dc_load_2035_high_mw": y1["dc_load_high_mw"],
        "stress_index_2035_high": y1["stress_index_high"],
        "dc_growth_source": y1["dc_growth_source"],
    }).sort_values("stress_index_2035", ascending=False).reset_index(names="province")
    combined.to_csv(OUT_DIR / "canada_stress_index_2025_vs_2035.csv", index=False)

    print(combined[["province", "dc_load_2025_mw", "stress_index_2025", "dc_load_2035_mw", "stress_index_2035",
                    "dc_load_2035_high_mw", "stress_index_2035_high"]].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))


if __name__ == "__main__":
    main()
