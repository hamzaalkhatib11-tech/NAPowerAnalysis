"""
canada_stress.py

Canadian provincial Grid Headroom Stress Index, 2025 vs 2035 (consolidates the
steps that were done inline in the sandbox chat; PROJECT_HANDOFF.md §4, §6).

  - Capacity: CER Canada Energy Future 2026, Variable == "Total", Current Measures.
  - 2025 peak: user-supplied system-operator peaks (config.CANADA_PEAK_LOAD_2025_MW).
  - 2035 peak: 2025 peak x (1 + (Fig8 ratio - 1) x (2035-2021)/(2050-2021)), using the
    CER EF2023 Figure 8 peak-demand growth ratio (2050/2021), Current Measures.
  - DC load 2025: footprint-derived provincial totals (output/canada_dc_by_province.csv).
  - DC load 2035: Ontario grows at the IESO 2025 APO rate (7.1% CAGR from 2026);
    other provinces are held flat (no DC-specific growth source was found).

Stress Index = DC load / (total capacity - peak load).

Writes to output/:
  ca_peak_demand_growth_ratio_2050.csv
  canada_stress_index_by_province.csv       (2025)
  canada_headroom_2025_vs_2035.csv
  ontario_datacenter_growth_ieso.csv        (yearly 2026-2050)
  canada_stress_index_2025_vs_2035.csv
  canada_stress_index_timeseries.csv        (yearly 2025-2035, for the year slider)
"""

import pandas as pd

from config import (
    BASE_YEAR, CANADA_PEAK_LOAD_2025_MW, CER_CAPACITY_CSV, CER_EF2023_SUPPLEMENT_XLSX,
    CER_SCENARIO, FIG8_BASE_YEAR, FIG8_END_YEAR, HOURS_PER_YEAR, IESO_BASE_YEAR,
    IESO_DC_CAGR, IESO_DC_TWH_2026, OUT_DIR, PROVINCE_ABBREV, TARGET_YEAR,
)

ONTARIO_GROWTH_SOURCE = ("IESO 2025 APO (3 TWh 2026 -> 16 TWh 2050, 7.1% CAGR), "
                         "scaled onto point-layer baseline")
FLAT_SOURCE = "no DC-specific growth projection available (supply-side tightening only)"


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
    """Linear interpolation of the CER 2021->2050 peak growth ratio, applied to the 2025 peak."""
    frac = (year - FIG8_BASE_YEAR) / (FIG8_END_YEAR - FIG8_BASE_YEAR)
    return peak_base * (1 + (ratio_2050 - 1) * frac)


def ieso_dc_twh(year: int) -> float:
    return IESO_DC_TWH_2026 * (1 + IESO_DC_CAGR) ** (year - IESO_BASE_YEAR)


def ontario_growth_factor(year: int) -> float:
    """Multiplier on the point-layer baseline. The baseline is treated as the
    IESO 2026 starting point, so 2025 and 2026 both map to 1.0."""
    return (1 + IESO_DC_CAGR) ** max(0, year - IESO_BASE_YEAR)


def dc_load(province: str, base_mw: float, year: int) -> float:
    return base_mw * ontario_growth_factor(year) if province == "Ontario" else base_mw


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ratios = load_fig8_ratios()
    ratios.to_csv(OUT_DIR / "ca_peak_demand_growth_ratio_2050.csv", index=False)
    cm_ratio = (ratios[ratios["scenario"] == CER_SCENARIO]
                .set_index("province_abbrev")["ratio_2050_over_2021"])

    dc_base = pd.read_csv(OUT_DIR / "canada_dc_by_province.csv").set_index("joined_province")["known_mw_total"]
    years = list(range(BASE_YEAR, TARGET_YEAR + 1))
    capacity = load_cer_capacity(years)
    provinces = list(CANADA_PEAK_LOAD_2025_MW)

    # yearly series -- every other table is a slice of this
    rows = []
    for prov in provinces:
        peak_base = CANADA_PEAK_LOAD_2025_MW[prov]
        ratio = cm_ratio[PROVINCE_ABBREV[prov]]
        base_mw = float(dc_base.get(prov, 0.0))
        for year in years:
            peak = peak_base if year == BASE_YEAR else projected_peak(peak_base, ratio, year)
            cap = capacity.loc[prov, year]
            load = dc_load(prov, base_mw, year)
            rows.append({
                "province": prov, "year": year, "dc_load_mw": load, "total_cap_mw": cap,
                "peak_load_mw": peak, "avail_cap_mw": cap - peak,
                "stress_index": load / (cap - peak),
                "dc_growth_source": ONTARIO_GROWTH_SOURCE if prov == "Ontario" else FLAT_SOURCE,
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
        "dc_growth_source": y1["dc_growth_source"],
    }).sort_values("stress_index_2025", ascending=False).reset_index(names="province")
    combined.to_csv(OUT_DIR / "canada_stress_index_2025_vs_2035.csv", index=False)

    print(f"Ontario DC growth factor {IESO_BASE_YEAR}->{TARGET_YEAR}: {ontario_growth_factor(TARGET_YEAR):.3f}")
    print(combined[["province", "stress_index_2025", "stress_index_2035", "stress_index_pct_change"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
