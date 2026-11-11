"""Self-service portal logic: a developer describes a service, the optimiser places it, a pull request is opened.

Flow (see onboard()): validate -> solve the whole platform with and without the new service -> refuse if nothing
fits the budget / node limits -> branch in a fresh clone of the config-repo -> catalog entry + apps values +
re-optimised envs/ and node file -> push -> pull request. A human merges it; ArgoCD (and Terraform if nodes change)
do the rest. Nothing here talks to the cluster.
"""
import json
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from catalog import (CATALOG_REL, app_values_yaml, catalog_json, load_catalog, merged_services, model_spec,
                     normalize, validate)
from data import SERVICES
from exporter import ExportError, default_run, propose, read_current
from model import build_instance, solve


class PortalError(Exception):
    def __init__(self, messages):
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        super().__init__("; ".join(self.messages))


def scrub(text, token):
    """Never let the token leak into an error message."""
    text = str(text)
    return text.replace(token, "***") if token else text


def clone(url, dest, token=None, run=default_run):
    try:
        run(["git", "clone", "--depth", "1", "--quiet", url, str(dest)])
    except FileNotFoundError:
        raise PortalError("git is not installed in this container image: rebuild the image from the updated Dockerfile")
    except subprocess.CalledProcessError as e:
        raise PortalError("could not clone the config-repo: " + scrub(e.stderr or e, token).strip()[:300])
    return Path(dest)


def repo_url(repo, token=None):
    return f"https://x-access-token:{token}@github.com/{repo}.git" if token else f"https://github.com/{repo}.git"


def github_open_pr(repo, token, api_base="https://api.github.com"):
    """open_pr(base, branch, title, body) -> pull request URL, through the REST API (no gh CLI needed in the pod)."""
    def open_pr(base, branch, title, body):
        req = urllib.request.Request(
            f"{api_base}/repos/{repo}/pulls", method="POST",
            data=json.dumps({"title": title, "head": branch, "base": base, "body": body}).encode(),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "User-Agent": "placement-optimiser-portal"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)["html_url"]
        except urllib.error.HTTPError as e:
            raise PortalError(f"GitHub refused to open the pull request (HTTP {e.code}): "
                              + scrub(e.read().decode(errors="replace"), token)[:300])
        except Exception as e:                                   # noqa: BLE001
            raise PortalError("could not reach GitHub: " + scrub(e, token))
    return open_pr


def evaluate(spec, scenario, catalog, solver="auto"):
    """Validate the request and compute the platform plan without and with the new service."""
    spec = normalize(spec)
    errors = validate(spec, taken_names=set(SERVICES) | set(catalog))
    if errors:
        raise PortalError(errors)
    before_services = merged_services(catalog)
    after_services = {**before_services, spec["name"]: model_spec(spec)}
    before = solve(build_instance(scenario, services=before_services), solver)
    after = solve(build_instance(scenario, services=after_services), solver)
    if after["status"] != "ok":
        raise PortalError(f"no placement fits: {after.get('reason', after['status'])}. "
                          f"Lower the memory, capacity needs or SLO strictness, or ask the platform team to raise the budget.")
    return spec, before, after


def summarize(spec, before, after):
    name = spec["name"]
    return {"service": name, "scenario": after.get("scenario"),
            "pods": after["placement"][name]["pods"],
            "nodes_before": before.get("nodes") if before["status"] == "ok" else None,
            "nodes_after": after["nodes"],
            "cost_before": before.get("cost") if before["status"] == "ok" else None,
            "cost_after": after["cost"],
            "latency_after_ms": after["mean_latency_ms"]}


def _spec_table(spec):
    rows = [("owner", spec["owner"] or "-"), ("image", f"{spec['image']['repository']}:{spec['image']['tag']}"),
            ("port / health", f"{spec['port']} {spec['health_path']}"), ("SLO", f"{spec['slo_ms']} ms"),
            ("capacity per pod", f"{spec['capacity']} req/s"), ("RAM per pod", f"{spec['mem_mi']} Mi"),
            ("requests per active user", f"{spec['rps_per_active_user']} /s"), ("min replicas", spec["min_replicas"])]
    return "| field | value |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)


def _git_failure(e, token):
    """A failed git command as a message a developer can act on."""
    detail = scrub((e.stderr or str(e)), token).strip()
    cmd = " ".join(e.cmd[3:5]) if isinstance(e.cmd, (list, tuple)) and len(e.cmd) > 4 else "git"
    hint = ""
    if "push" in cmd and any(w in detail.lower() for w in ("403", "denied", "permission", "authentication", "not found")):
        hint = " (the token needs Contents: read and write on the config-repo)"
    return f"git step failed ({cmd}){hint}: {detail[-250:]}"


def onboard(spec, scenario, repo_dir, open_pr, solver="auto", run=default_run, token=None):
    """Create the pull request in a checkout of the config-repo. Returns (branch, url)."""
    repo_dir = Path(repo_dir)
    catalog = load_catalog(repo_dir / CATALOG_REL)
    spec, before, after = evaluate(spec, scenario, catalog, solver)
    name = spec["name"]

    def write_extra(repo):
        (repo / CATALOG_REL).mkdir(parents=True, exist_ok=True)
        (repo / CATALOG_REL / f"{name}.json").write_text(catalog_json(spec))
        (repo / "apps" / name).mkdir(parents=True, exist_ok=True)
        (repo / "apps" / name / "values.yaml").write_text(app_values_yaml(spec))

    added = sum(after["nodes"].values()) - (sum(before["nodes"].values()) if before["status"] == "ok" else sum(after["nodes"].values()))
    title = f"onboard({name}): {added:+d} node(s), ${after['cost']}/h"
    body = (f"## New service `{name}` (requested through the portal)\n\n{_spec_table(spec)}\n\n"
            f"The platform was re-optimised for scenario `{scenario}` with this service included.\n\n")
    try:
        return propose(after, repo_dir, run=run, open_pr=open_pr, branch_prefix=f"onboard/{name}", title=title,
                       body_prefix=body, write_extra=write_extra, extra_paths=[CATALOG_REL, f"apps/{name}"])
    except subprocess.CalledProcessError as e:
        raise PortalError(_git_failure(e, token))
    except ExportError as e:
        raise PortalError(str(e))


class CatalogCache:
    """The optimiser pod re-reads the catalog from GitHub every few minutes so its UI and /metrics include onboarded
    services. If GitHub is unreachable it keeps the last good copy (or the built-in services)."""

    def __init__(self, loader, ttl=300):
        self.loader, self.ttl, self._at, self._catalog, self._lock = loader, ttl, 0.0, {}, threading.Lock()

    def catalog(self):
        with self._lock:
            if time.time() - self._at > self.ttl:
                try:
                    self._catalog = self.loader()
                except Exception:                                # noqa: BLE001 - soft failure by design
                    pass
                self._at = time.time()
            return dict(self._catalog)

    def services(self):
        return merged_services(self.catalog())


def preview_in_repo(spec, scenario, catalog, solver="auto"):
    spec, before, after = evaluate(spec, scenario, catalog, solver)
    return summarize(spec, before, after)


def with_clone(repo, token, fn):
    """Run fn(path_of_fresh_clone) and delete the clone afterwards (it may hold the token in .git/config)."""
    with tempfile.TemporaryDirectory() as tmp:
        return fn(clone(repo_url(repo, token), Path(tmp) / "repo", token))
