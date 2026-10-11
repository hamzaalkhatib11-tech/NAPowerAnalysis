"""
run_pipeline.py

Runs the full pipeline in order, then verifies against reference_outputs/.

    python run_pipeline.py            # everything (~6 min; footprint tiles are the slow part)
    python run_pipeline.py --skip-footprint   # reuse output/canada_dc_with_mw.csv
"""

import subprocess
import sys
import time

STEPS = [
    "harmonize_datacenters.py",
    "canada_footprint_mw.py",
    "canada_stress.py",
    "us_time_series.py",
    "local_stress.py",
    "canada_local_stress.py",
    "export_gis.py",
    "verify_outputs.py",
]


def main():
    skip = {"canada_footprint_mw.py"} if "--skip-footprint" in sys.argv else set()
    for script in STEPS:
        if script in skip:
            print(f"--- skipping {script}")
            continue
        print(f"\n{'=' * 70}\n--- {script}\n{'=' * 70}", flush=True)
        start = time.time()
        result = subprocess.run([sys.executable, "-u", script])
        print(f"--- {script} finished in {time.time() - start:.0f}s (exit {result.returncode})")
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
