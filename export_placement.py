"""Solve a scenario and write the result into a config-repo checkout.

    python export_placement.py --config-repo ~/config-repo --scenario normal --dry-run
    python export_placement.py --config-repo ~/config-repo --scenario asia-launch --solver ilp --commit
    python export_placement.py --config-repo ~/config-repo --scenario asia-launch --solver ilp --pr
"""
import argparse
import sys

from data import DEFAULT_BUDGET, list_scenarios
from exporter import ExportError, git_commit, plan, propose, read_current, summary_markdown, write_all
from model import build_instance, solve


def main(argv=None, run=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config-repo", required=True, help="path to the config-repo checkout")
    ap.add_argument("--scenario", default="normal", help=f"one of {list(list_scenarios())} or a path to a .json file")
    ap.add_argument("--solver", default="auto", choices=["auto", "ilp", "greedy", "exhaustive"])
    ap.add_argument("--demand-mult", type=float, default=1.0)
    ap.add_argument("--cost-mult", type=float, default=1.0, help="multiplier on node prices")
    ap.add_argument("--slo-mult", type=float, default=1.0)
    ap.add_argument("--ap-demand", type=float, default=1.0, help="multiplier for ap-southeast demand only")
    ap.add_argument("--active-ratio", type=float, default=None, help="override the scenario's active-user ratio")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="$/hour for all nodes")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    mode.add_argument("--commit", action="store_true", help="write files and git commit in the current branch")
    mode.add_argument("--pr", action="store_true", help="new branch + commit + push + open a pull request (needs gh)")
    a = ap.parse_args(argv)

    kw = dict(scenario=a.scenario, demand_mult=a.demand_mult, cost_mult=a.cost_mult, slo_mult=a.slo_mult,
              budget=a.budget, active_ratio=a.active_ratio)
    if a.ap_demand != 1:
        kw["region_mult"] = {"ap-southeast": a.ap_demand}
    res = solve(build_instance(**kw), a.solver)
    print(f"scenario={res.get('scenario', a.scenario)} solver={res['solver']} status={res['status']}")
    if res["status"] != "ok":
        print(f"ERROR: {res.get('reason')}", file=sys.stderr)
        return 2
    print(f"cost=${res['cost']}/h latency={res['mean_latency_ms']}ms nodes={res['nodes']} pods={res['pods_total']}")
    try:
        before_pods, before_nodes = read_current(a.config_repo)
        if a.dry_run:
            print(summary_markdown(res, before_pods, before_nodes))
            for path in plan(res):
                print("  would write", path)
            return 0
        if a.pr:
            kwargs = {"run": run} if run else {}
            branch, url = propose(res, a.config_repo, **kwargs)
            print("nothing to propose: config-repo already matches" if not branch else f"branch {branch}\npull request: {url}")
            return 0
        rep = write_all(res, a.config_repo)
    except ExportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    for k, v in rep["envs"].items():
        print(f"{k}: {', '.join(v) or '-'}")
    n = rep["nodes"]
    print(f"nodes: {n['old']} -> {n['new']}" + ("" if n["changed"] else " (unchanged)"))
    if a.commit:
        msg = (f"placement({res['scenario']}): {res['solver']} nodes={list(res['nodes'].values())} "
               f"cost=${res['cost']}/h latency={res['mean_latency_ms']}ms")
        print("committed" if git_commit(a.config_repo, msg) else "nothing to commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
