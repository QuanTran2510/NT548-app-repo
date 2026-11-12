import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import CATALOG_REL, load_catalog
from exporter import NODES_FILE, default_run, read_current_detail, write_all
from model import build_instance, solve_exhaustive
from portal import (CatalogCache, PortalError, clone, evaluate, github_open_pr, onboard, scrub)

SPEC = {"name": "shop-cart", "owner": "team-shop",
        "image": {"repository": "docker.io/nginxinc/nginx-unprivileged", "tag": "1.27-alpine"}, "port": 8080,
        "health_path": "/", "slo_ms": 150, "capacity": 120, "mem_mi": 96, "rps_per_active_user": 0.1, "min_replicas": 2}


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


def repo_with_remote(tmp):
    remote, repo = Path(tmp) / "remote.git", Path(tmp) / "config-repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(repo, "config", k, v)
    (repo / "README.md").write_text("x\n")
    git(repo, "add", "-A"); git(repo, "commit", "-m", "init")
    git(repo, "remote", "add", "origin", str(remote)); git(repo, "push", "-u", "origin", "main")
    return repo, remote


def test_evaluate_rejects_bad_and_duplicate_requests():
    for bad in ({**SPEC, "name": "web-api"}, {**SPEC, "image": {"repository": "evil.io/x", "tag": "1"}}):
        try:
            evaluate(bad, "normal", {}, solver="exhaustive")
            assert False
        except PortalError as e:
            assert e.messages
    spec, before, after = evaluate(SPEC, "normal", {}, solver="exhaustive")
    assert after["status"] == "ok" and "shop-cart" in after["placement"] and "shop-cart" not in before["placement"]


def test_unplaceable_service_is_refused_without_creating_anything():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)
        impossible = {**SPEC, "slo_ms": 10}                      # below the 20 ms local latency: no site can serve it
        try:
            onboard(impossible, "normal", repo, open_pr=lambda *a: "never", solver="exhaustive")
            assert False
        except PortalError as e:
            assert "no placement fits" in str(e)
        assert git(repo, "branch", "--list").count("onboard/") == 0 and git(remote, "branch", "--list").count("onboard/") == 0


def test_onboard_opens_a_pr_with_catalog_apps_envs_and_nodes():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)
        opened = {}

        def open_pr(base, branch, title, body):
            opened.update(base=base, branch=branch, title=title, body=body)
            return "https://github.com/u/config-repo/pull/9"

        branch, url = onboard(SPEC, "normal", repo, open_pr, solver="exhaustive")
        assert url.endswith("/pull/9") and branch.startswith("onboard/shop-cart-") and opened["base"] == "main"
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main" and not (repo / CATALOG_REL).exists()   # main untouched
        files = git(remote, "ls-tree", "-r", "--name-only", branch).split()
        assert "catalog/services/shop-cart.json" in files and "apps/shop-cart/values.yaml" in files
        assert str(NODES_FILE) in files and any(f.startswith("envs/shop-cart-") for f in files)
        entry = json.loads(git(remote, "show", f"{branch}:catalog/services/shop-cart.json"))
        assert entry["mem_mi"] == 96 and entry["owner"] == "team-shop"
        env = git(remote, "show", f"{branch}:{[f for f in files if f.startswith('envs/shop-cart-')][0]}")
        assert "memory: 96Mi" in env and "service: shop-cart" in env
        assert "New service `shop-cart`" in opened["body"] and "| SLO | 150 ms |" in opened["body"] and "### Nodes" in opened["body"]
        assert opened["title"].startswith("onboard(shop-cart):")


def test_second_service_sees_the_first_one_in_the_catalog():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)
        branch, _ = onboard(SPEC, "normal", repo, lambda *a: "u", solver="exhaustive")
        git(repo, "merge", "--no-edit", branch); git(repo, "push", "origin", "main")        # "merge the PR"
        assert "shop-cart" in load_catalog(repo / CATALOG_REL)
        try:
            onboard(SPEC, "normal", repo, lambda *a: "u", solver="exhaustive")               # same name again
            assert False
        except PortalError as e:
            assert "already exists" in str(e)
        second = {**SPEC, "name": "shop-search"}
        branch2, _ = onboard(second, "normal", repo, lambda *a: "u", solver="exhaustive")
        files = git(remote, "ls-tree", "-r", "--name-only", branch2).split()
        assert any(f.startswith("envs/shop-cart-") for f in files) and any(f.startswith("envs/shop-search-") for f in files)


def test_token_is_scrubbed_from_errors():
    assert "SECRET" not in scrub("fatal: https://x-access-token:SECRET@github.com/o/r.git not found", "SECRET")
    try:
        clone("https://x-access-token:SECRETTOKEN@127.0.0.1:1/none.git", "/tmp/never-created-dir", token="SECRETTOKEN")
        assert False
    except PortalError as e:
        assert "SECRETTOKEN" not in str(e)


class _FakeGitHub(BaseHTTPRequestHandler):
    seen = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeGitHub.seen = {"path": self.path, "auth": self.headers["Authorization"], "body": body}
        if body["head"] == "refuse":
            self.send_response(422); self.end_headers(); self.wfile.write(b'{"message":"bad token SECRETTOKEN"}'); return
        self.send_response(201); self.end_headers(); self.wfile.write(json.dumps({"html_url": "https://github.com/o/r/pull/3"}).encode())

    def log_message(self, *a):
        pass


def test_github_api_pull_request_call():
    srv = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        open_pr = github_open_pr("o/r", "SECRETTOKEN", api_base=f"http://127.0.0.1:{srv.server_port}")
        assert open_pr("main", "onboard/x", "title", "body") == "https://github.com/o/r/pull/3"
        seen = _FakeGitHub.seen
        assert seen["path"] == "/repos/o/r/pulls" and seen["auth"] == "Bearer SECRETTOKEN"
        assert seen["body"] == {"title": "title", "head": "onboard/x", "base": "main", "body": "body"}
        try:
            open_pr("main", "refuse", "t", "b")
            assert False
        except PortalError as e:
            assert "422" in str(e) and "SECRETTOKEN" not in str(e)
    finally:
        srv.shutdown()


def test_catalog_cache_keeps_last_good_copy_when_github_fails():
    state = {"calls": 0, "fail": False}

    def loader():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("github down")
        return {"shop-cart": {"name": "shop-cart", **{k: SPEC[k] for k in ("slo_ms", "capacity", "mem_mi", "rps_per_active_user", "min_replicas")}}}

    cache = CatalogCache(loader, ttl=0)
    assert "shop-cart" in cache.services()
    state["fail"] = True
    assert "shop-cart" in cache.services() and state["calls"] == 2                      # served from the last good copy
    assert "realtime-chat" in CatalogCache(lambda: (_ for _ in ()).throw(RuntimeError()), ttl=0).services()   # built-ins survive


def test_http_endpoints_preview_errors_and_disabled_submit():
    import app as A                                           # no PORTAL_REPO / GITHUB_TOKEN in the test environment
    c = A.app.test_client()
    assert c.get("/portal").status_code == 200
    ok = c.post("/api/portal/preview", json={**SPEC, "scenario": "normal"})
    assert ok.status_code == 200 and ok.get_json()["preview"]["service"] == "shop-cart"
    bad = c.post("/api/portal/preview", json={**SPEC, "name": "Bad_Name", "scenario": "normal"})
    assert bad.status_code == 400 and bad.get_json()["errors"]
    assert c.post("/api/portal/submit", json=SPEC).status_code == 503


def test_missing_git_binary_is_a_readable_error():
    def no_git(args, cwd=None):
        raise FileNotFoundError(2, "No such file or directory", "git")
    try:
        clone("https://example.invalid/x.git", "/tmp/never-created", run=no_git)
        assert False
    except PortalError as e:
        assert "git is not installed" in str(e)


def test_failed_push_becomes_a_portal_error_with_a_hint_and_no_token():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)

        def run(args, cwd=None):
            if "push" in args:
                raise subprocess.CalledProcessError(128, args, stderr="remote: Permission to o/r.git denied to u. SECRETTOKEN")
            return default_run(args, cwd)

        try:
            onboard(SPEC, "normal", repo, lambda *a: "never", solver="exhaustive", run=run, token="SECRETTOKEN")
            assert False
        except PortalError as e:
            assert "Contents: read and write" in str(e) and "SECRETTOKEN" not in str(e)
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"                      # back on main, nothing half-done


def test_http_portal_failures_are_always_json_with_errors():
    import app as A
    c = A.app.test_client()
    saved = (A.PORTAL_REPO, A.PORTAL_TOKEN, A.with_clone)
    try:
        A.PORTAL_REPO, A.PORTAL_TOKEN = "o/r", "SECRETTOKEN"
        for boom in (RuntimeError("kaboom SECRETTOKEN"), FileNotFoundError(2, "No such file", "git"),
                     subprocess.CalledProcessError(1, ["git", "x"], stderr="fatal")):
            def fail(*a, _b=boom, **k):
                raise _b
            A.with_clone = fail
            for path in ("/api/portal/submit", "/api/portal/preview"):
                r = c.post(path, json={**SPEC, "scenario": "normal"})
                j = r.get_json()
                assert j is not None and j["ok"] is False and j["errors"], (path, boom, r.status_code)
                assert "SECRETTOKEN" not in json.dumps(j) and "unknown scenario" not in json.dumps(j)
        A.with_clone = saved[2]
        r = c.post("/api/portal/preview", json={**SPEC, "scenario": "nope"})
        assert r.status_code == 400 and "unknown scenario" in r.get_json()["errors"][0]
    finally:
        A.PORTAL_REPO, A.PORTAL_TOKEN, A.with_clone = saved


def _deploy_asia_launch(repo):
    """A platform that is already running the asia-launch plan (the situation in which onboarding must be harmless)."""
    write_all(solve_exhaustive(build_instance("asia-launch")), repo)
    git(repo, "add", "-A"); git(repo, "commit", "-m", "deployed asia-launch"); git(repo, "push")
    return read_current_detail(repo)


def _other_services_changed(repo, branch):
    diff = git(repo, "diff", "--name-only", f"main...origin/{branch}").split()
    return [f for f in diff if f.startswith("envs/") and "shop-cart" not in f]


def test_onboarding_never_resizes_existing_services_or_removes_nodes():
    # the bug this guards against: onboarding with the quiet scenario "night" re-planned EVERY service for night
    # traffic, cut their pods and removed a node, even though the platform was running asia-launch
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)
        (detail, nodes) = _deploy_asia_launch(repo)
        for scenario in ("night", "normal", "peak"):
            opened = {}
            branch, _ = onboard(SPEC, scenario, repo, lambda b, br, t, body: opened.update(body=body) or "u", solver="exhaustive")
            after = json.loads(git(remote, "show", f"{branch}:{NODES_FILE}"))["node_counts"]
            assert all(after[j] >= nodes[j] for j in nodes), (scenario, nodes, after)           # no node removed
            assert _other_services_changed(repo, branch) == [], scenario                        # nothing else touched
            files = git(remote, "ls-tree", "-r", "--name-only", branch).split()
            assert any(f.startswith("envs/shop-cart-") for f in files) and "catalog/services/shop-cart.json" in files
            assert "Existing services are not changed" in opened["body"]
            assert "| realtime-chat |" not in opened["body"] and "| web-api |" not in opened["body"]


def test_a_heavy_service_adds_nodes_but_still_leaves_others_alone():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)
        (detail, nodes) = _deploy_asia_launch(repo)
        heavy = {**SPEC, "mem_mi": 400, "min_replicas": 4}
        branch, _ = onboard(heavy, "normal", repo, lambda *a: "u", solver="exhaustive")
        after = json.loads(git(remote, "show", f"{branch}:{NODES_FILE}"))["node_counts"]
        assert sum(after.values()) > sum(nodes.values()) and all(after[j] >= nodes[j] for j in nodes)
        assert _other_services_changed(repo, branch) == []


def test_a_service_that_cannot_fit_is_refused_and_nothing_is_created():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)
        _deploy_asia_launch(repo)
        try:
            onboard({**SPEC, "mem_mi": 1000, "min_replicas": 6}, "normal", repo, lambda *a: "never", solver="exhaustive")
            assert False
        except PortalError as e:
            assert "no placement fits" in str(e)
        assert git(remote, "branch", "--list").count("onboard/") == 0


def test_read_current_detail_returns_replicas_and_memory():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = repo_with_remote(tmp)
        res = solve_exhaustive(build_instance("normal"))
        write_all(res, repo)
        detail, nodes = read_current_detail(repo)
        for service, p in res["placement"].items():
            for site, n in p["pods"].items():
                assert detail[(service, site)] == (n, p["mem_mi"])
        assert nodes == res["nodes"]


def test_two_quick_submissions_get_different_branches():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = repo_with_remote(tmp)
        b1, _ = onboard(SPEC, "normal", repo, lambda *a: "u", solver="exhaustive")
        b2, _ = onboard(SPEC, "normal", repo, lambda *a: "u", solver="exhaustive")           # double click, same second
        assert b1 != b2


def test_preview_reports_the_current_nodes_as_the_starting_point():
    import app as A
    r = A.app.test_client().post("/api/portal/preview", json={**SPEC, "scenario": "night"})
    p = r.get_json()["preview"]
    assert p["nodes_before"] == {"us-east": 1, "eu-west": 1, "ap-southeast": 1} and all(p["nodes_after"][j] >= 1 for j in p["nodes_after"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
