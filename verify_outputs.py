"""
verify_outputs.py

Compares output/ against reference_outputs/ (the sandbox-run CSVs the handoff's
§6 results came from; differences are expected after the methodology updates),
then checks invariants that must still hold and the U.S. time series' shape and
conservation of load. Exit code 1 if an invariant or time-series check fails.
"""

import sys

import numpy as np
import pandas as pd

from config import DATA_ARTIFACT_BAS, GIS_DIR, GROWTH_PUE, OUT_DIR, REFERENCE_DIR

# file -> key column(s) used to align rows
KEYED_FILES = {
    "ba_level_stress_index.csv": "ba_name",
    "qa_state_mw_totals.csv": "state",
    "adjusted_baseline_load_by_state.csv": "state",
    "canada_dc_by_province.csv": "joined_province",
    "canada_stress_index_by_province.csv": "province",
    "canada_headroom_2025_vs_2035.csv": "province",
    "canada_stress_index_2025_vs_2035.csv": "province",
    "ontario_datacenter_growth_ieso.csv": "year",
    "ca_peak_demand_growth_ratio_2050.csv": ["scenario", "province_abbrev"],
    "canada_building_footprint_tiles_needed.csv": "QuadKey",
}
COUNT_FILES = ["harmonized_existing_dc.csv", "harmonized_existing_dc_with_ba.csv",
               "flagged_bad_geocodes.csv", "canada_dc_with_mw.csv"]

REL_TOL = 0.01   # 1% -- the reference rounds some columns to 0.1 / 0.01


def read(path):
    return pd.read_csv(path, dtype={"QuadKey": str})


def compare_keyed(name, key):
    new, ref = read(OUT_DIR / name), read(REFERENCE_DIR / name)
    keys = [key] if isinstance(key, str) else key
    merged = new.merge(ref, on=keys, how="outer", suffixes=("", "__ref"), indicator=True)
    def labels(side):
        rows = merged[merged["_merge"] == side][keys].astype(str)
        return [" / ".join(r) for r in rows.itertuples(index=False)]
    only_new, only_ref = labels("left_only"), labels("right_only")
    both = merged[merged["_merge"] == "both"]

    diffs = []
    shared = [c for c in ref.columns if c in new.columns and c not in keys]
    for col in shared:
        a = pd.to_numeric(both[col], errors="coerce")
        b = pd.to_numeric(both[f"{col}__ref"], errors="coerce")
        if a.isna().all() and b.isna().all():
            continue
        both_nan = a.isna() & b.isna()
        close = np.isclose(a, b, rtol=REL_TOL, atol=1e-6) | both_nan
        # NaN vs 0 in count columns is a representation change, not a result change
        close |= (a.fillna(0) == 0) & (b.fillna(0) == 0)
        for i in np.where(~close.to_numpy())[0]:
            row = both.iloc[i]
            diffs.append((" / ".join(str(row[k]) for k in keys), col, b.iloc[i], a.iloc[i]))

    status = "MATCH" if not (diffs or only_new or only_ref) else "DIFF"
    print(f"\n[{status}] {name}: {len(both)} shared rows, "
          f"{len(only_new)} new-only, {len(only_ref)} reference-only, {len(diffs)} value diffs")
    if only_new:
        print(f"   new-only: {only_new[:10]}{' ...' if len(only_new) > 10 else ''}")
    if only_ref:
        print(f"   reference-only: {only_ref[:10]}{' ...' if len(only_ref) > 10 else ''}")
    for k, col, ref_v, new_v in diffs[:12]:
        print(f"   {k:<55.55} {col:<28} ref={ref_v:<14.6g} new={new_v:.6g}")
    if len(diffs) > 12:
        print(f"   ... {len(diffs) - 12} more")


def compare_counts(name):
    new, ref = read(OUT_DIR / name), read(REFERENCE_DIR / name)
    print(f"\n[{'MATCH' if len(new) == len(ref) else 'DIFF'}] {name}: {len(new)} rows (reference {len(ref)})")


def _check(label, value, target, tol) -> bool:
    ok = value is not None and pd.notna(value) and abs(value - target) <= tol
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:<44} got {value if value is None else round(float(value), 4)}")
    return ok


def invariants() -> bool:
    """Results the methodology updates must NOT change."""
    ca = read(OUT_DIR / "canada_stress_index_2025_vs_2035.csv").set_index("province")
    head = read(OUT_DIR / "canada_headroom_2025_vs_2035.csv").set_index("province")
    dc = read(OUT_DIR / "canada_dc_with_mw.csv")
    vq = dc.loc[dc["name"] == "VisionQuest ATC Data Center"]
    flagged = read(OUT_DIR / "flagged_bad_geocodes.csv")

    print("\n=== invariants (must hold) ===")
    ok = all([
        _check("VisionQuest footprint MW ~0.42", vq["mw_estimate"].iloc[0] if len(vq) else None, 0.42, 0.02),
        _check("VisionQuest lat (Charlottetown)", vq["lat"].iloc[0] if len(vq) else None, 46.2372, 0.001),
        _check("Ontario DC growth 2025->2035 x1.854",
               ca.loc["Ontario", "dc_load_2035_mw"] / ca.loc["Ontario", "dc_load_2025_mw"], 1.854, 0.001),
        _check("Ontario headroom change -46.8%", head.loc["Ontario", "headroom_change_pct"], -46.8, 0.1),
        _check("Nova Scotia headroom change +395.5%", head.loc["Nova Scotia", "headroom_change_pct"], 395.5, 0.1),
        _check("bad geocodes flagged = 13", len(flagged), 13, 0),
    ])
    return ok


def time_series_checks() -> bool:
    ts = read(OUT_DIR / "us_ba_time_series_2025_2035.csv")
    required = ["BA_NAME", "YEAR", "DC_LOAD_MW", "TOTAL_CAP_MW", "PEAK_LOAD_MW", "AVAIL_CAP_MW", "STRESS_INDEX"]
    print("\n=== U.S. time series ===")
    missing = [c for c in required if c not in ts.columns]
    print(f"  [{'PASS' if not missing else 'FAIL'}] required columns present {missing or ''}")
    per_ba = ts.groupby("BA_NAME")["YEAR"].agg(["min", "max", "nunique"])
    full = (per_ba["min"].eq(2025) & per_ba["max"].eq(2035) & per_ba["nunique"].eq(11)).all()
    print(f"  [{'PASS' if full else 'FAIL'}] every BA has 2025..2035 ({len(per_ba)} BAs, {len(ts)} rows)")
    no_new_2025 = ts.loc[ts["YEAR"] == 2025, "DC_NEW_MW"].abs().max() < 1e-6
    print(f"  [{'PASS' if no_new_2025 else 'FAIL'}] no projected new load in 2025")
    monotone = (ts.sort_values("YEAR").groupby("BA_NAME")["DC_LOAD_MW"].diff().dropna() >= -1e-6).all()
    print(f"  [{'PASS' if monotone else 'FAIL'}] DC load never decreases year to year")
    new_2035 = ts.loc[ts["YEAR"] == 2035, "DC_NEW_MW"].sum()
    import json
    cells = json.load(open(OUT_DIR / "growth_2035_wgs84.geojson", encoding="utf-8"))["features"]
    grid_total = sum(f["properties"]["data_center_it_power_mw"] for f in cells) * GROWTH_PUE
    conserved = abs(new_2035 - grid_total) < 1.0
    print(f"  [{'PASS' if conserved else 'FAIL'}] 2035 new load {new_2035:,.0f} MW = growth grid x PUE {grid_total:,.0f} MW")
    flagged = set(ts.loc[ts["DATA_ARTIFACT_FLAG"], "BA_NAME"])
    artifacts_ok = set(DATA_ARTIFACT_BAS) <= flagged
    print(f"  [{'PASS' if artifacts_ok else 'FAIL'}] configured artifact BAs flagged ({len(flagged)} flagged in total)")

    ca = read(OUT_DIR / "canada_stress_index_timeseries.csv").sort_values(["province", "year"])
    step = ca.groupby("province")["peak_load_mw"].diff().dropna()
    first_step = ca.groupby("province")["peak_load_mw"].apply(lambda s: s.iloc[1] - s.iloc[0])
    smooth = (first_step - step.groupby(ca["province"]).mean()).abs().max() < 1.0
    print(f"  [{'PASS' if smooth else 'FAIL'}] Canadian peaks rise evenly from 2025 (no 2026 jump)")

    gdb_ok = (GIS_DIR / "ggr442_grid_stress.gdb").exists()
    print(f"  [{'PASS' if gdb_ok else 'FAIL'}] GIS geodatabase exported")
    return all([not missing, full, no_new_2025, monotone, conserved, artifacts_ok, smooth, gdb_ok])


def legacy_benchmarks():
    """Handoff §6 U.S. figures. Informational: the campus / dedup / Active-only
    fixes are expected to move them."""
    ba = read(OUT_DIR / "ba_level_stress_index.csv").set_index("ba_name")["stress_index"]
    print("\n=== handoff §6 U.S. figures (informational; expected to change) ===")
    for name, handoff in [("PUBLIC SERVICE COMPANY OF NEW MEXICO", 1.25), ("ARIZONA PUBLIC SERVICE COMPANY", 0.99),
                          ("BONNEVILLE POWER ADMINISTRATION", 0.55), ("PJM INTERCONNECTION, LLC", 0.235),
                          ("TENNESSEE VALLEY AUTHORITY", 0.27)]:
        print(f"  {name:<40} handoff {handoff:<6} now {ba.get(name, np.nan):.3f}")


def main():
    print("=== output/ vs reference_outputs/ ===")
    for name, key in KEYED_FILES.items():
        compare_keyed(name, key)
    for name in COUNT_FILES:
        compare_counts(name)
    legacy_benchmarks()
    ok = invariants() & time_series_checks()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
