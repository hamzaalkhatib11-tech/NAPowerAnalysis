"""
grid_strength_sensitivity.py

Does the plant/line double count in grid strength change the hex findings?

Grid strength = plant MW + delivering-line MW. A plant's export lines reach its
own switchyard, which OSM maps as a substation, so its power can count twice
(once as the plant, again as the lines carrying it away). Grid strength only sets
each hex's *share* of BA/provincial headroom, so this can move headroom between
hexes but not change the totals. Runs local_stress.py and canada_local_stress.py
under each config.GRID_STRENGTH_MODES:

  sum                    default: plants + lines
  no_plant_switchyards   fix 1: lines reaching only a plant switchyard don't count
  max                    fix 2: max(plants, lines) per hex

and compares gap hexes, gap MW and Gi* hot spot hexes with the default.

Writes to output/sensitivity/:
  <mode>/us_hex_local_stress_2025_2035.csv, <mode>/ca_hex_local_stress_2025_2035.csv
  grid_strength_comparison.csv          one row per country x mode x year
  grid_strength_class_changes_2035.csv  hexes with data-centre load whose 2035 class changes
"""

import pandas as pd

import canada_local_stress
import local_stress
from config import GRID_STRENGTH_MODES, OUT_DIR

SENS_DIR = OUT_DIR / "sensitivity"
YEARS = (2030, 2035)
COUNTRIES = {"U.S.": (local_stress, "STATE"), "Canada": (canada_local_stress, "PROVINCE")}


def changed(base: set, alt: set) -> str:
    return f"{len(base & alt)} kept, {len(base - alt)} dropped, {len(alt - base)} added"


def main():
    runs = {}
    for mode in GRID_STRENGTH_MODES:
        out = SENS_DIR / mode
        out.mkdir(parents=True, exist_ok=True)
        for country, (module, _) in COUNTRIES.items():
            print(f"\n{'=' * 70}\n{country}, grid strength = {mode}\n{'=' * 70}", flush=True)
            runs[country, mode] = module.main(mode=mode, out_dir=out)

    rows, changes = [], []
    for country, (_, region) in COUNTRIES.items():
        base = runs[country, "sum"]
        for mode in GRID_STRENGTH_MODES:
            ts = runs[country, mode]
            for year in YEARS:
                y, b = ts[ts["YEAR"] == year], base[base["YEAR"] == year]
                gap, gap_b = set(y.loc[y["LOCAL_CLASS"] == "gap", "HEX_ID"]), set(b.loc[b["LOCAL_CLASS"] == "gap", "HEX_ID"])
                hot, hot_b = set(y.loc[y["GI_BIN"] > 0, "HEX_ID"]), set(b.loc[b["GI_BIN"] > 0, "HEX_ID"])
                dc = y["DC_LOAD_MW"] > 0
                rows.append({
                    "country": country, "mode": mode, "year": year,
                    "gap_hexes": len(gap), "gap_mw": y["DC_GAP_MW"].sum(),
                    "near_limit_hexes": int((y["LOCAL_CLASS"] == "near_limit").sum()),
                    "hot_spot_hexes": len(hot),
                    "headroom_in_dc_hexes_mw": y.loc[dc, "LOCAL_HEADROOM_MW"].sum(),
                    "gap_hexes_vs_sum": changed(gap_b, gap),
                    "hot_spots_vs_sum": changed(hot_b, hot),
                })
            if mode == "sum":
                continue
            y = ts[ts["YEAR"] == YEARS[-1]].set_index("HEX_ID")
            b = base[base["YEAR"] == YEARS[-1]].set_index("HEX_ID")
            diff = y.index[(y["LOCAL_CLASS"] != b["LOCAL_CLASS"]) & (y["DC_LOAD_MW"] > 0)]
            for hex_id in diff:
                changes.append({
                    "country": country, "mode": mode, "HEX_ID": hex_id, "region": y.at[hex_id, region],
                    "dc_load_mw": y.at[hex_id, "DC_LOAD_MW"],
                    "headroom_sum_mw": b.at[hex_id, "LOCAL_HEADROOM_MW"],
                    "headroom_mode_mw": y.at[hex_id, "LOCAL_HEADROOM_MW"],
                    "class_sum": b.at[hex_id, "LOCAL_CLASS"], "class_mode": y.at[hex_id, "LOCAL_CLASS"],
                })

    comparison = pd.DataFrame(rows)
    comparison.to_csv(SENS_DIR / "grid_strength_comparison.csv", index=False, float_format="%.1f")
    class_changes = pd.DataFrame(changes).sort_values(["country", "mode", "dc_load_mw"], ascending=[True, True, False])
    class_changes.to_csv(SENS_DIR / "grid_strength_class_changes_2035.csv", index=False, float_format="%.1f")

    # the default run must reproduce the main outputs exactly
    for country, name in (("U.S.", "us"), ("Canada", "ca")):
        main_csv = pd.read_csv(OUT_DIR / f"{name}_hex_local_stress_2025_2035.csv")
        sens_csv = pd.read_csv(SENS_DIR / "sum" / f"{name}_hex_local_stress_2025_2035.csv")
        same = main_csv.equals(sens_csv)
        print(f"{country}: 'sum' run matches output/{name}_hex_local_stress_2025_2035.csv: {same}")

    pd.set_option("display.width", 200)
    print("\n" + comparison.to_string(index=False, float_format=lambda v: f"{v:,.0f}"))
    print(f"\n2035 class changes, hexes with data-centre load ({len(class_changes)}):")
    print(class_changes.groupby(["country", "mode", "class_sum", "class_mode"]).size().to_string())


if __name__ == "__main__":
    main()
