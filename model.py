"""Placement model: where to run pods of each service and how many nodes each site needs.

Decisions
  n[s,j]  pods of service s at site j (integer)
  m[j]    t3.small nodes at site j (integer)
  x[s,r,j] share of region r's demand for s served from site j (routing; model only,
          it is NOT deployed - the cluster only receives n and m)
Minimise   node cost + POD_OVERHEAD * pods + LAMBDA * demand-weighted mean latency
Subject to every request is served within the service's SLO (x only where latency <= SLO),
           capacity   sum_r demand[s,r] x[s,r,j] <= cap_eff[s] n[s,j]   (cap_eff = capacity * utilisation),
           min pods   sum_j n[s,j] >= min_replicas[s],
           memory     sum_s mem[s] n[s,j] + reserved[j] <= node_mem[j] m[j],
           nodes      min_nodes[j] <= m[j] <= max_nodes[j],  sum_j m[j] <= 9,
           node cost  sum_j price[j] m[j] <= budget.
Pods cost nothing by themselves; money is spent on NODES, so cost rises in steps.
Three solvers: ILP (PuLP/CBC), greedy heuristic, exhaustive (exact over nearest-site routing).
"""
import itertools
import math

from data import (DEFAULT_BUDGET, LAMBDA, LATENCY, MAX_TOTAL_NODES, POD_OVERHEAD, REGIONS,
                  SERVICES, SITES, UTILIZATION, load_scenario)


def build_instance(scenario="normal", demand_mult=1.0, cost_mult=1.0, region_mult=None,
                   slo_mult=1.0, budget=DEFAULT_BUDGET, active_ratio=None, services=None):
    """services: {name: {slo_ms, capacity, mem_mi, rps_per_active_user, min_replicas}}; default = data.SERVICES.
    Services onboarded through the portal come from the config-repo catalog (see catalog.py)."""
    sc = scenario if isinstance(scenario, dict) else load_scenario(scenario)
    region_mult = region_mult or {}
    ratio = sc["active_ratio"] if active_ratio is None else active_ratio
    out = {}
    for s, spec in (services or SERVICES).items():
        mix = sc.get("service_mix", {}).get(s, 1.0)
        out[s] = {
            "slo": spec["slo_ms"] * slo_mult,
            "raw_cap": spec["capacity"],
            "cap": spec["capacity"] * UTILIZATION,          # effective req/s per pod
            "mem": spec["mem_mi"],
            "min_replicas": spec["min_replicas"],
            "demand": {r: sc["users"][r] * ratio * spec["rps_per_active_user"] * mix
                          * demand_mult * region_mult.get(r, 1.0) for r in REGIONS},
        }
    sites = {j: {**SITES[j], "node_price": SITES[j]["node_price"] * cost_mult} for j in REGIONS}
    return {"scenario": sc["name"], "budget": budget, "services": out, "sites": sites,
            "max_total_nodes": MAX_TOTAL_NODES}


def reachable(sv, r):
    return [j for j in REGIONS if LATENCY[r][j] <= sv["slo"]]


def _assign_nearest(sv, open_sites):
    assign = {}
    for r in REGIONS:
        ok = [j for j in open_sites if LATENCY[r][j] <= sv["slo"]]
        if not ok:
            return None
        assign[r] = {min(ok, key=lambda j: LATENCY[r][j]): 1.0}
    return assign


def _loads(sv, assign):
    load = {j: 0.0 for j in REGIONS}
    for r, shares in assign.items():
        for j, share in shares.items():
            load[j] += sv["demand"][r] * share
    return load


def _pods(sv, assign):
    """Pods per site derived from the routing (heuristics). Exact shares, so 1e-6 is plenty."""
    load = _loads(sv, assign)
    n = {j: (math.ceil(load[j] / sv["cap"] - 1e-6) if load[j] > 1e-6 else 0) for j in REGIONS}
    deficit = sv["min_replicas"] - sum(n.values())
    if deficit > 0:                      # keep at least min_replicas pods, next to the busiest site
        busiest = max(REGIONS, key=lambda j: (load[j], -REGIONS.index(j)))
        n[busiest] += deficit
    return n


def nodes_needed(inst, mem_used):
    """Smallest node count per site that holds the memory requested (exact integer maths)."""
    return {j: max(site["min_nodes"], math.ceil((mem_used[j] + site["reserved_mi"]) / site["node_mem_mi"]))
            for j, site in inst["sites"].items()}


def _fail(solver, reason, status="infeasible"):
    return {"status": status, "solver": solver, "reason": reason}


def finish(inst, solver, assign, n_given=None):
    """Routing (+ optional pod counts from the solver) -> full result with nodes, cost, latency.

    n_given: {service: {site: pods}} chosen by the ILP itself. The ILP's own integers are used
    instead of re-deriving them from fractional x, which carry ~1e-6 solver noise.
    """
    wlat = total = 0.0
    placement, mem_used = {}, {j: 0 for j in REGIONS}
    for s, sv in inst["services"].items():
        n = n_given[s] if n_given else _pods(sv, assign[s])
        for j in REGIONS:
            mem_used[j] += sv["mem"] * n[j]
        for r, shares in assign[s].items():
            for j, share in shares.items():
                wlat += sv["demand"][r] * share * LATENCY[r][j]
        total += sum(sv["demand"].values())
        placement[s] = {"pods": {j: n[j] for j in REGIONS if n[j]}, "mem_mi": sv["mem"],
                        "routing": {r: {j: round(sh, 3) for j, sh in shares.items()}
                                    for r, shares in assign[s].items()}}
    nodes = nodes_needed(inst, mem_used)
    node_cost = sum(inst["sites"][j]["node_price"] * nodes[j] for j in REGIONS)
    pods_total = sum(sum(p["pods"].values()) for p in placement.values())
    lat = wlat / total if total else 0.0
    res = {"status": "ok", "solver": solver, "scenario": inst["scenario"],
           "cost": round(node_cost, 4), "mean_latency_ms": round(lat, 2),
           "objective": round(node_cost + POD_OVERHEAD * pods_total + LAMBDA * lat, 5),
           "nodes": nodes, "pods_total": pods_total, "placement": placement,
           "memory": {j: {"used_mi": mem_used[j] + inst["sites"][j]["reserved_mi"],
                          "capacity_mi": nodes[j] * inst["sites"][j]["node_mem_mi"]} for j in REGIONS}}
    over = [j for j in REGIONS if nodes[j] > inst["sites"][j]["max_nodes"]]
    if over or sum(nodes.values()) > inst["max_total_nodes"]:
        res.update(status="over_capacity", reason=f"needs {nodes}: more nodes than the lab allows")
    elif node_cost > inst["budget"] + 1e-9:
        res.update(status="over_budget", reason=f"node cost ${node_cost:.4f}/h exceeds budget")
    return res


def solve_exhaustive(inst):
    options = {}
    for s, sv in inst["services"].items():
        opts = []
        for k in range(1, len(REGIONS) + 1):
            for open_sites in itertools.combinations(REGIONS, k):
                a = _assign_nearest(sv, open_sites)
                if a:
                    opts.append(a)
        if not opts:
            return _fail("exhaustive", f"{s}: SLO cannot be met from any site set")
        options[s] = opts
    best = None
    for combo in itertools.product(*options.values()):
        res = finish(inst, "exhaustive", dict(zip(options, combo)))
        if res["status"] == "ok" and (best is None or res["objective"] < best["objective"]):
            best = res
    return best or _fail("exhaustive", "no SLO-feasible placement fits the budget / node limits")


def solve_greedy(inst):
    """Cover every region within SLO using the most demand per dollar, then route to the nearest site.
    Decides each service alone, so it cannot pack pods of different services onto shared nodes."""
    assign = {}
    for s, sv in inst["services"].items():
        uncovered, opened = set(REGIONS), []
        while uncovered:
            def gain(j):
                cov = [r for r in uncovered if LATENCY[r][j] <= sv["slo"]]
                return sum(sv["demand"][r] for r in cov) / inst["sites"][j]["node_price"] if cov else -1
            cands = [j for j in REGIONS if j not in opened and gain(j) >= 0]
            if not cands:
                return _fail("greedy", f"{s}: SLO cannot be met from any site set")
            j = max(cands, key=gain)
            opened.append(j)
            uncovered -= {r for r in uncovered if LATENCY[r][j] <= sv["slo"]}
        assign[s] = _assign_nearest(sv, opened)
    return finish(inst, "greedy", assign)


def solve_ilp(inst):
    import pulp
    prob = pulp.LpProblem("placement", pulp.LpMinimize)
    total = sum(sum(sv["demand"].values()) for sv in inst["services"].values()) or 1.0
    n, m, x, lat_terms = {}, {}, {}, []
    for ji, j in enumerate(REGIONS):
        site = inst["sites"][j]
        m[j] = pulp.LpVariable(f"m_{ji}", site["min_nodes"], site["max_nodes"], cat="Integer")
    for si, (s, sv) in enumerate(inst["services"].items()):
        for ji, j in enumerate(REGIONS):
            n[s, j] = pulp.LpVariable(f"n_{si}_{ji}", 0, 60, cat="Integer")
        for ri, r in enumerate(REGIONS):
            ok = reachable(sv, r)
            if not ok:
                return _fail("ilp", f"{s}: no site meets the SLO for {r}")
            for j in ok:
                x[s, r, j] = pulp.LpVariable(f"x_{si}_{ri}_{REGIONS.index(j)}", 0, 1)
                lat_terms.append(sv["demand"][r] * LATENCY[r][j] * x[s, r, j])
            prob += pulp.lpSum(x[s, r, j] for j in ok) == 1
        for j in REGIONS:
            load = [sv["demand"][r] * x[s, r, j] for r in REGIONS if (s, r, j) in x]
            prob += pulp.lpSum(load) <= sv["cap"] * n[s, j]
        prob += pulp.lpSum(n[s, j] for j in REGIONS) >= sv["min_replicas"]
    for j in REGIONS:
        site = inst["sites"][j]
        prob += (pulp.lpSum(sv["mem"] * n[s, j] for s, sv in inst["services"].items())
                 <= site["node_mem_mi"] * m[j] - site["reserved_mi"])
    prob += pulp.lpSum(m[j] for j in REGIONS) <= inst["max_total_nodes"]
    node_cost = pulp.lpSum(inst["sites"][j]["node_price"] * m[j] for j in REGIONS)
    pod_count = pulp.lpSum(n.values())
    prob += node_cost + POD_OVERHEAD * pod_count + (LAMBDA / total) * pulp.lpSum(lat_terms)
    prob += node_cost <= inst["budget"]
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[prob.status] != "Optimal":
        return _fail("ilp", "infeasible: SLO, memory, node limits and budget cannot all be met")
    assign = {s: {r: {j: x[s, r, j].value() for j in reachable(sv, r)
                      if (x[s, r, j].value() or 0) > 1e-6} for r in REGIONS}
              for s, sv in inst["services"].items()}
    n_given = {s: {j: int(round(n[s, j].value())) for j in REGIONS} for s in inst["services"]}
    return finish(inst, "ilp", assign, n_given)


def solve(inst, solver="auto"):
    solvers = {"ilp": solve_ilp, "greedy": solve_greedy, "exhaustive": solve_exhaustive}
    if solver == "auto":
        try:
            return solve_ilp(inst)
        except Exception:
            return solve_exhaustive(inst)
    return solvers[solver](inst)
