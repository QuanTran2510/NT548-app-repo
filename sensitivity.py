"""Sensitivity sweeps: how nodes, pods, cost and latency react to demand / price / SLO / budget changes."""
from data import REGIONS
from model import build_instance, solve

DEFAULT_SWEEPS = {
    "demand_mult":  [0.5, 1.0, 1.5, 2.0, 3.0, 4.0],
    "active_ratio": [0.03, 0.05, 0.10, 0.15, 0.25, 0.35],
    "ap_demand":    [0.5, 1.0, 2.0, 4.0, 6.0, 8.0],      # growth in ap-southeast demand only
    "cost_mult":    [0.5, 0.75, 1.0, 1.5, 2.0],           # multiplier on t3.small prices
    "slo_mult":     [0.5, 0.8, 1.0, 1.3, 2.0],
    "budget":       [0.07, 0.08, 0.10, 0.12, 0.15, 0.20],  # $/h for all nodes (0.07 = the 3 base nodes)
}

FIELDS = ["value", "status", "cost", "latency", "nodes", "pods"] + [f"nodes_{j}" for j in REGIONS] + ["reason"]


def sweep(param, values=None, solver="auto", **base):
    rows = []
    for v in values or DEFAULT_SWEEPS[param]:
        kw = dict(base)
        if param == "ap_demand":
            kw["region_mult"] = {"ap-southeast": v}
        else:
            kw[param] = v
        res = solve(build_instance(**kw), solver)
        row = {"value": v, "status": res["status"], "reason": res.get("reason", "")}
        if res["status"] == "ok":
            row.update(cost=res["cost"], latency=res["mean_latency_ms"], nodes=sum(res["nodes"].values()),
                       pods=res["pods_total"], **{f"nodes_{j}": res["nodes"][j] for j in REGIONS})
        rows.append(row)
    return rows
