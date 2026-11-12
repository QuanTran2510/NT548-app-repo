import os
import time

from flask import Flask, Response, jsonify, render_template, request

import metrics
from data import DEFAULT_BUDGET, LATENCY, REGIONS, SERVICES, SITES, list_scenarios
from catalog import CATALOG_REL, load_catalog
from exporter import ExportError, plan
from model import build_instance, solve
from portal import (CatalogCache, PortalError, github_open_pr, onboard, preview_in_repo, scrub, with_clone)
from sensitivity import DEFAULT_SWEEPS, sweep


def _params():
    g = request.args
    p = dict(scenario=g.get("scenario", "normal"), demand_mult=float(g.get("demand_mult", 1)),
             cost_mult=float(g.get("cost_mult", 1)), slo_mult=float(g.get("slo_mult", 1)),
             budget=float(g.get("budget", DEFAULT_BUDGET)))
    if g.get("active_ratio"):
        p["active_ratio"] = float(g["active_ratio"])
    return p


PORTAL_REPO = os.environ.get("PORTAL_REPO", "")        # "owner/name" of the config-repo
PORTAL_TOKEN = os.environ.get("GITHUB_TOKEN", "")      # fine-grained PAT: contents + pull requests (read/write)
_cache = CatalogCache(lambda: with_clone(PORTAL_REPO, PORTAL_TOKEN, lambda p: load_catalog(p / CATALOG_REL))) if PORTAL_REPO else None


def _instance(p):
    """ap_demand scales only ap-southeast demand (same meaning as in the sweeps)."""
    p = dict(p)
    if _cache:
        p["services"] = _cache.services()          # built-in services + those onboarded through the portal
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
    def missing_file(e):
        if "scenarios" in str(getattr(e, "filename", "")):
            reason = f"unknown scenario; choose one of {list(list_scenarios())}"
        else:
            reason = f"file not found: {getattr(e, 'filename', None) or e}"
        return jsonify({"status": "error", "reason": reason, "errors": [reason]}), 400

    @app.get("/")
    def index():
        return render_template("index.html", regions=REGIONS, services=SERVICES, sites=SITES, latency=LATENCY,
                               budget=DEFAULT_BUDGET, sweeps=list(DEFAULT_SWEEPS), scenarios=list_scenarios())

    @app.get("/portal")
    def portal_page():
        return render_template("portal.html", scenarios=list(list_scenarios()),
                               can_submit=bool(PORTAL_REPO and PORTAL_TOKEN), repo=PORTAL_REPO)

    def _portal_request():
        body = request.get_json(silent=True) or {}
        scenario = body.pop("scenario", "normal")
        solver = body.pop("solver", "auto") 
        return body, scenario, solver

    def _portal_unexpected(e):
        """Never a bare HTML 500: the page must be able to show what went wrong. Details go to the pod log."""
        app.logger.exception("portal request failed")
        msg = scrub(f"{type(e).__name__}: {e}", PORTAL_TOKEN)[:250]
        return jsonify({"ok": False, "errors": [f"unexpected error ({msg}). See: kubectl logs deploy/placement-optimiser-eu-west -n placement-optimiser"]}), 500

    @app.post("/api/portal/preview")
    def portal_preview():
        spec, scenario, solver = _portal_request() 
        try:
            if scenario not in list_scenarios():
                raise PortalError(f"unknown scenario '{scenario}'; choose one of {list(list_scenarios())}")
            if PORTAL_REPO:
                out = with_clone(PORTAL_REPO, PORTAL_TOKEN,
                                 lambda p: preview_in_repo(spec, scenario, load_catalog(p / CATALOG_REL), solver=solver, repo_dir=p))
            else:
                out = preview_in_repo(spec, scenario, {}, solver=solver)   # no repo configured: an empty platform
            return jsonify({"ok": True, "preview": out})
        except PortalError as e:
            return jsonify({"ok": False, "errors": e.messages}), 400
        except Exception as e:                                    # noqa: BLE001
            return _portal_unexpected(e)

    @app.post("/api/portal/submit")
    def portal_submit():
        if not (PORTAL_REPO and PORTAL_TOKEN):
            return jsonify({"ok": False, "errors": ["the portal cannot open pull requests: PORTAL_REPO / GITHUB_TOKEN are not configured"]}), 503
        spec, scenario, solver = _portal_request() 
        try:
            if scenario not in list_scenarios():
                raise PortalError(f"unknown scenario '{scenario}'; choose one of {list(list_scenarios())}")
            branch, url = with_clone(PORTAL_REPO, PORTAL_TOKEN,
                                     lambda p: onboard(spec, scenario, p, github_open_pr(PORTAL_REPO, PORTAL_TOKEN), solver=solver, token=PORTAL_TOKEN))
            if not branch:
                raise PortalError("nothing to propose: the config-repo already contains this service")
            return jsonify({"ok": True, "branch": branch, "pull_request": url})
        except (PortalError, ExportError) as e:
            return jsonify({"ok": False, "errors": getattr(e, "messages", [str(e)])}), 400
        except Exception as e:                                    # noqa: BLE001
            return _portal_unexpected(e)

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
