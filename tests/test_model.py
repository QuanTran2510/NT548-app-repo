import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data import LAMBDA, REGIONS, SITES
from model import (_pods, build_instance, nodes_needed, solve_exhaustive, solve_greedy)

SCENARIOS = ["normal", "asia-launch", "peak", "night"]


def test_demand_comes_from_users_times_rates():
    inst = build_instance("normal")                       # 3000 users, 10 % active
    assert inst["services"]["realtime-chat"]["demand"]["us-east"] == 3000 * 0.10 * 0.5     # 150 req/s
    assert math.isclose(inst["services"]["web-api"]["demand"]["us-east"], 60.0)
    inst2 = build_instance("normal", active_ratio=0.2)
    assert math.isclose(inst2["services"]["web-api"]["demand"]["us-east"], 120.0)


def test_pod_count_uses_utilisation_headroom():
    sv = {"cap": 80 * 0.7, "min_replicas": 1, "demand": {"us-east": 120.0, "eu-west": 0.0, "ap-southeast": 0.0}}
    assert _pods(sv, {"us-east": {"us-east": 1.0}, "eu-west": {}, "ap-southeast": {}})["us-east"] == 3   # ceil(120/56)


def test_min_replicas_enforced():
    inst = build_instance("night")
    for s, p in solve_exhaustive(inst)["placement"].items():
        assert sum(p["pods"].values()) >= inst["services"][s]["min_replicas"]


def test_base_scenario_fits_the_three_existing_nodes():
    e = solve_exhaustive(build_instance("normal"))
    assert e["status"] == "ok" and sum(e["nodes"].values()) >= 3
    try:
        import pulp  # noqa: F401
    except ImportError:
        print("skip ilp part (no pulp)")
        return
    from model import solve_ilp
    r = solve_ilp(build_instance("normal"))                  # ILP may split traffic, so it packs tighter
    assert r["status"] == "ok" and r["nodes"] == {j: 1 for j in REGIONS}
    assert math.isclose(r["cost"], sum(s["node_price"] for s in SITES.values()))


def test_memory_never_exceeds_node_capacity_and_slo_holds():
    for sc in SCENARIOS:
        inst = build_instance(sc)
        for solve in (solve_greedy, solve_exhaustive):
            r = solve(inst)
            if r["status"] != "ok":
                continue
            for j, m in r["memory"].items():
                assert m["used_mi"] <= m["capacity_mi"], (sc, r["solver"], j)
            for s, p in r["placement"].items():
                for region, shares in p["routing"].items():
                    for j in shares:
                        from data import LATENCY
                        assert LATENCY[region][j] <= inst["services"][s]["slo"]


def test_exhaustive_never_worse_than_greedy():
    for sc in SCENARIOS:
        inst = build_instance(sc)
        g, e = solve_greedy(inst), solve_exhaustive(inst)
        if g["status"] == "ok" and e["status"] == "ok":
            assert e["objective"] <= g["objective"] + 1e-9, sc


def test_nodes_grow_in_steps_with_demand():
    nodes = [sum(solve_exhaustive(build_instance("normal", demand_mult=m))["nodes"].values()) for m in (0.25, 4)]
    assert nodes[0] == 3 and nodes[1] > 3


def test_nodes_needed_is_exact_integer_ceiling():
    inst = build_instance("normal")
    free = {j: SITES[j]["node_mem_mi"] - SITES[j]["reserved_mi"] for j in REGIONS}   # RAM of one node left for pods
    assert nodes_needed(inst, dict(free)) == {j: 1 for j in REGIONS}
    over = dict(free)
    over["ap-southeast"] += 1
    assert nodes_needed(inst, over)["ap-southeast"] == 2


def test_tiny_budget_and_impossible_slo_fail_cleanly():
    assert solve_exhaustive(build_instance("normal", budget=0.05))["status"] == "infeasible"
    assert solve_greedy(build_instance("normal", budget=0.05))["status"] == "over_budget"
    assert solve_exhaustive(build_instance("normal", slo_mult=0.1))["status"] == "infeasible"


def test_peak_does_not_fit_when_node_limit_is_tight():
    r = solve_exhaustive(build_instance("peak", budget=0.10))
    assert r["status"] == "infeasible"


def test_objective_is_cost_plus_pods_plus_latency():
    r = solve_exhaustive(build_instance("normal"))
    from data import POD_OVERHEAD
    assert math.isclose(r["objective"], r["cost"] + POD_OVERHEAD * r["pods_total"] + LAMBDA * r["mean_latency_ms"], abs_tol=2e-5)


def test_ilp_not_worse_than_exhaustive_and_has_no_idle_pods():
    try:
        import pulp  # noqa: F401
    except ImportError:
        print("skip (no pulp)")
        return
    from model import solve_ilp
    for sc in SCENARIOS:
        inst = build_instance(sc)
        e, i = solve_exhaustive(inst), solve_ilp(inst)
        assert i["status"] == "ok", (sc, i)
        if e["status"] == "ok":
            assert i["objective"] <= e["objective"] + 1e-6, (sc, i["objective"], e["objective"])
        for s, p in i["placement"].items():                     # pods are free, so none may be spare
            load = {j: sum(inst["services"][s]["demand"][r] * sh.get(j, 0) for r, sh in p["routing"].items())
                    for j in REGIONS}
            need = sum(math.ceil(load[j] / inst["services"][s]["cap"] - 1e-4) for j in REGIONS if load[j] > 1e-6)
            assert sum(p["pods"].values()) <= max(need, inst["services"][s]["min_replicas"]) + 1, (sc, s)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
