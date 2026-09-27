"""
verify_outputs.py

Compares output/ against reference_outputs/ (the sandbox-run CSVs the handoff's
§6 results came from) and checks the headline §6 benchmarks. Prints a report;
exit code 1 if any benchmark fails.
"""

import sys

import numpy as np
import pandas as pd

from config import OUT_DIR, REFERENCE_DIR

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


def benchmarks() -> bool:
    ba = read(OUT_DIR / "ba_level_stress_index.csv").set_index("ba_name")["stress_index"]
    ca = read(OUT_DIR / "canada_stress_index_2025_vs_2035.csv").set_index("province")
    dc = read(OUT_DIR / "canada_dc_with_mw.csv")
    vq = dc.loc[dc["name"] == "VisionQuest ATC Data Center"]

    checks = [
        ("PNM stress index ~1.25", ba.get("PUBLIC SERVICE COMPANY OF NEW MEXICO"), 1.25, 0.01),
        ("APS stress index ~0.99", ba.get("ARIZONA PUBLIC SERVICE COMPANY"), 0.99, 0.01),
        ("PJM stress index ~0.235", ba.get("PJM INTERCONNECTION, LLC"), 0.235, 0.005),
        ("Ontario stress 2025 ~0.0137", ca.loc["Ontario", "stress_index_2025"], 0.0137, 0.0001),
        ("Ontario stress 2035 ~0.0478", ca.loc["Ontario", "stress_index_2035"], 0.0478, 0.0001),
        ("Ontario DC load 2035 ~482 MW", ca.loc["Ontario", "dc_load_2035_mw"], 482.0, 1.0),
        ("VisionQuest footprint MW ~0.42", vq["mw_estimate"].iloc[0] if len(vq) else None, 0.42, 0.02),
    ]
    print("\n=== §6 benchmarks ===")
    ok_all = True
    for label, value, target, tol in checks:
        ok = value is not None and pd.notna(value) and abs(value - target) <= tol
        ok_all &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:<34} got {value if value is None else round(value, 4)}")
    if len(vq):
        print(f"  VisionQuest location: {vq['lat'].iloc[0]:.5f}, {vq['lon'].iloc[0]:.5f} "
              f"({vq['joined_province'].iloc[0]})")
    return ok_all


def main():
    print("=== output/ vs reference_outputs/ ===")
    for name, key in KEYED_FILES.items():
        compare_keyed(name, key)
    for name in COUNT_FILES:
        compare_counts(name)
    return 0 if benchmarks() else 1


if __name__ == "__main__":
    sys.exit(main())
