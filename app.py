import time

from flask import Flask, Response, jsonify, render_template, request

import metrics
from data import DEFAULT_BUDGET, LATENCY, REGIONS, SERVICES, SITES, list_scenarios
from exporter import ExportError, plan
from model import build_instance, solve
from sensitivity import DEFAULT_SWEEPS, sweep


def _params():
    g = request.args
    p = dict(scenario=g.get("scenario", "normal"), demand_mult=float(g.get("demand_mult", 1)),
             cost_mult=float(g.get("cost_mult", 1)), slo_mult=float(g.get("slo_mult", 1)),
             budget=float(g.get("budget", DEFAULT_BUDGET)))
    if g.get("active_ratio"):
        p["active_ratio"] = float(g["active_ratio"])
    return p


def _instance(p):
    """ap_demand scales only ap-southeast demand (same meaning as in the sweeps)."""
    p = dict(p)
    ap = float(request.args.get("ap_demand", 1))
    if ap != 1:
        p["region_mult"] = {"ap-southeast": ap}
    return build_instance(**p)


def _solve_recorded():
    inst = _instance(_params())
    t0 = time.perf_counter()
    res = solve(inst, request.args.get("solver", "auto"))
    metrics.record(res, inst["budget"], time.perf_counter() - t0)
    return res


def create_app():
    app = Flask(__name__)

    @app.errorhandler(FileNotFoundError)
    def unknown_scenario(e):
        return jsonify({"status": "error", "reason": f"unknown scenario; choose one of {list(list_scenarios())}"}), 400

    @app.get("/")
    def index():
        return render_template("index.html", regions=REGIONS, services=SERVICES, sites=SITES, latency=LATENCY,
                               budget=DEFAULT_BUDGET, sweeps=list(DEFAULT_SWEEPS), scenarios=list_scenarios())

    @app.get("/api/scenarios")
    def api_scenarios():
        return jsonify(list_scenarios())

    @app.get("/api/solve")
    def api_solve():
        return jsonify(_solve_recorded())

    @app.get("/api/plan")
    def api_plan():
        """Dry run of the GitOps export: the files (pods AND nodes) a pull request would contain."""
        res = _solve_recorded()
        try:
            return jsonify({"result": res, "files": plan(res)})
        except ExportError as e:
            return jsonify({"result": res, "error": str(e)}), 422

    @app.get("/api/sensitivity")
    def api_sensitivity():
        param = request.args.get("param", "demand_mult")
        base = _params()
        base.pop(param, None)
        return jsonify(sweep(param, solver=request.args.get("solver", "auto"), **base))

    @app.get("/metrics")
    def prom_metrics():
        if not metrics.has_plan():                      # first scrape: compute the base plan once
            inst = build_instance()
            t0 = time.perf_counter()
            metrics.record(solve(inst, "auto"), inst["budget"], time.perf_counter() - t0)
        return Response(metrics.render(), mimetype="text/plain; version=0.0.4")

    @app.get("/healthz")
    def health():
        return "ok"

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
