"""
run_pipeline.py

Runs the full pipeline in order, then verifies against reference_outputs/.

    python run_pipeline.py            # everything (~17 min; footprint tiles are the slow part)
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
    # skipping the footprint match still rebuilds the province summary, which
    # harmonize_datacenters.py overwrites with a placeholder that has no MW
    args = {"canada_footprint_mw.py": ["--summary-only"]} if "--skip-footprint" in sys.argv else {}
    for script in STEPS:
        extra = args.get(script, [])
        print(f"\n{'=' * 70}\n--- {script} {' '.join(extra)}\n{'=' * 70}", flush=True)
        start = time.time()
        result = subprocess.run([sys.executable, "-u", script, *extra])
        print(f"--- {script} finished in {time.time() - start:.0f}s (exit {result.returncode})")
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
