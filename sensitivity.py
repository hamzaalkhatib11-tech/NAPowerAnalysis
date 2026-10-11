"""
sensitivity.py

W/ft2 sensitivity. Re-runs the U.S. steps (harmonize -> BA time series -> hex
tier) at each W_PER_SQFT in VALUES, into output/sensitivity/w<value>/, and
compares the headline numbers with the main run in output/.

Canada is not re-run: every Canadian MW is footprint area x W/ft2 with no
control total, so Canadian loads and stress indices scale exactly with W/ft2
(x 100/150 or x 200/150).

Writes output/sensitivity/w_per_sqft_summary.csv.
"""

import os
import shutil
import subprocess
import sys

import pandas as pd

from config import OUT_DIR, W_PER_SQFT

VALUES = [100.0, 200.0]
STEPS = ["harmonize_datacenters.py", "us_time_series.py", "local_stress.py"]
SENS_DIR = OUT_DIR / "sensitivity"
TRACKED_BAS = ["PJM INTERCONNECTION, LLC", "TENNESSEE VALLEY AUTHORITY",
               "BALANCING AUTHORITY OF NORTHERN CALIFORNIA", "BONNEVILLE POWER ADMINISTRATION",
               "ASSOCIATED ELECTRIC COOPERATIVE, INC."]


def run(value: float):
    out = SENS_DIR / f"w{value:.0f}"
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GGR_OUT_DIR=str(out), GGR_W_PER_SQFT=str(value), PYTHONIOENCODING="utf-8")
    for script in STEPS:
        print(f"  W/ft2 = {value:.0f}: {script}", flush=True)
        result = subprocess.run([sys.executable, "-W", "ignore", script], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            sys.exit(f"{script} failed at W/ft2 = {value}:\n{result.stderr[-2000:]}")
    return out


def summarize(label: str, out) -> dict:
    ts = pd.read_csv(out / "us_ba_time_series_2025_2035.csv")
    ts = ts[~ts["DATA_ARTIFACT_FLAG"]]
    hx = pd.read_csv(out / "us_hex_local_stress_2025_2035.csv")
    y25, y35 = ts[ts["YEAR"] == 2025], ts[ts["YEAR"] == 2035]
    h35 = hx[hx["YEAR"] == 2035]
    row = {
        "W_PER_SQFT": label,
        "us_dc_load_2025_mw": y25["DC_LOAD_MW"].sum(),
        "us_dc_load_2035_mw": y35["DC_LOAD_MW"].sum(),
        "bas_over_1_2035": int((y35["STRESS_INDEX"] > 1).sum()),
        "bas_over_1_2035_pipeline_only": int((y35["STRESS_INDEX_PIPELINE_ONLY"] > 1).sum()),
        "hexes_stress_over_0.5_2035": int((h35["LOCAL_STRESS_CAPPED"] > 0.5).sum()),
    }
    for ba in TRACKED_BAS:
        short = ba.split(",")[0].split(" - ")[0].title()
        row[f"{short} 2035"] = y35.loc[y35["BA_NAME"] == ba, "STRESS_INDEX"].squeeze()
    return row


def main():
    if not (OUT_DIR / "us_ba_capacity_changes.csv").exists():
        sys.exit("Run the main pipeline first (python run_pipeline.py).")
    rows = []
    for value in VALUES:
        out = run(value)
        # the hex tier reads the capacity roll-forward, which W/ft2 doesn't touch
        rows.append(summarize(f"{value:.0f}", out))
    rows.insert(1, summarize(f"{W_PER_SQFT:.0f} (main)", OUT_DIR))
    summary = pd.DataFrame(rows)
    summary.to_csv(SENS_DIR / "w_per_sqft_summary.csv", index=False, float_format="%.3f")
    print()
    print(summary.set_index("W_PER_SQFT").T.to_string(float_format=lambda v: f"{v:,.2f}"))
    print(f"\nCanada scales linearly: Ontario 2035 stress x{100 / W_PER_SQFT:.2f} at 100, "
          f"x{200 / W_PER_SQFT:.2f} at 200 W/ft2.")
    for value in VALUES:   # keep only the summary; the reruns are large
        shutil.rmtree(SENS_DIR / f"w{value:.0f}", ignore_errors=True)


if __name__ == "__main__":
    main()
