"""Prometheus text-format metrics for the optimiser (no extra dependency).

The app runs as ONE gunicorn worker (several threads), so this in-memory state is the
single source of truth for what /metrics reports.
"""
import threading
import time

_lock = threading.Lock()
_last = {"result": None, "budget": None, "seconds": None, "at": None}
_counts = {}          # (solver, status) -> n


def record(result, budget, seconds):
    with _lock:
        key = (result.get("solver", "?"), result.get("status", "?"))
        _counts[key] = _counts.get(key, 0) + 1
        if result.get("status") == "ok":     # keep the last GOOD plan on /metrics
            _last.update(result=result, budget=budget, seconds=seconds, at=time.time())


def has_plan():
    return _last["result"] is not None


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def render():
    with _lock:
        res, budget, secs, at = _last["result"], _last["budget"], _last["seconds"], _last["at"]
        counts = dict(_counts)
    out = []

    def metric(name, kind, help_, samples):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lab = "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}" if labels else ""
            out.append(f"{name}{lab} {value}")

    metric("placement_solves_total", "counter", "Optimiser runs by solver and status.",
           [({"solver": s, "status": st}, n) for (s, st), n in sorted(counts.items())])
    metric("placement_has_plan", "gauge", "1 if a valid placement has been computed.", [({}, 1 if res else 0)])
    if res:
        metric("placement_cost_dollars_per_hour", "gauge", "Hourly cost of the nodes in the current plan.",
               [({"solver": res["solver"]}, res["cost"])])
        metric("placement_budget_dollars_per_hour", "gauge", "Node budget used for the current plan.", [({}, budget)])
        metric("placement_mean_latency_ms", "gauge", "Demand-weighted mean latency (model, not measured).",
               [({"solver": res["solver"]}, res["mean_latency_ms"])])
        metric("placement_nodes", "gauge", "t3.small nodes per site in the current plan.",
               [({"site": j}, n) for j, n in res["nodes"].items()])
        metric("placement_pods", "gauge", "Pods of a service at a site in the current plan.",
               [({"service": s, "site": j}, n) for s, p in res["placement"].items() for j, n in p["pods"].items()])
        metric("placement_memory_used_mi", "gauge", "RAM requested per site (pods + reserved).",
               [({"site": j}, m["used_mi"]) for j, m in res["memory"].items()])
        metric("placement_memory_capacity_mi", "gauge", "RAM available for requests per site.",
               [({"site": j}, m["capacity_mi"]) for j, m in res["memory"].items()])
        metric("placement_solve_seconds", "gauge", "Wall time of the last successful solve.",
               [({"solver": res["solver"]}, round(secs, 4))])
        metric("placement_last_success_timestamp_seconds", "gauge", "Unix time of the last valid plan.", [({}, round(at, 3))])
        metric("placement_scenario_info", "gauge", "Scenario of the current plan.", [({"scenario": res.get("scenario", "")}, 1)])
    return "\n".join(out) + "\n"
