"""Writes results/sensitivity_<scenario>_<param>_<solver>.csv for every sweep.

    python run_sensitivity.py                       # exhaustive + greedy, scenario normal
    python run_sensitivity.py --scenario asia-launch ilp exhaustive greedy
"""
import argparse
import csv
import os

from sensitivity import DEFAULT_SWEEPS, FIELDS, sweep

ap = argparse.ArgumentParser()
ap.add_argument("--scenario", default="normal")
ap.add_argument("solvers", nargs="*", default=["exhaustive", "greedy"])
a = ap.parse_args()

os.makedirs("results", exist_ok=True)
for solver in a.solvers:
    for param in DEFAULT_SWEEPS:
        rows = sweep(param, solver=solver, scenario=a.scenario)
        path = f"results/sensitivity_{a.scenario}_{param}_{solver}.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(path)
