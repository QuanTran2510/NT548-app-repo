import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metrics
from exporter import (ExportError, MARKER, NODES_FILE, default_run, plan, plan_envs, plan_nodes, propose,
                      read_current, summary_markdown, write_all)
from export_placement import main as export_main
from model import build_instance, solve_exhaustive, solve_greedy


def result(sc="normal", solver=solve_exhaustive, **kw):
    return solver(build_instance(sc, **kw))


def test_env_files_carry_replicas_node_selector_and_memory():
    r = result()
    files = plan_envs(r)
    expected = {f"{s}-{j}" for s, p in r["placement"].items() for j in p["pods"]}
    assert set(files) == expected
    for name, t in files.items():
        service, site = t.split("service: ")[1].split("\n")[0], t.split("site: ")[1].split("\n")[0]
        n = r["placement"][service]["pods"][site]
        assert t.startswith(MARKER) and f"replicaCount: {n}\n" in t
        assert f"nodeSelector:\n  region: {site}" in t
        assert f"memory: {r['placement'][service]['mem_mi']}Mi" in t


def test_node_file_is_what_terraform_reads():
    r = result("asia-launch")
    counts, text = plan_nodes(r)
    assert counts == r["nodes"] and counts["ap-southeast"] == 2
    assert json.loads(text) == {"node_counts": counts}
    assert str(NODES_FILE) in plan(result("asia-launch"))


def test_write_all_is_idempotent_and_removes_stale_generated_folders():
    with tempfile.TemporaryDirectory() as d:
        r1 = write_all(result("normal"), d)
        assert r1["nodes"]["old"] is None and r1["envs"]["written"]
        r2 = write_all(result("normal"), d)
        assert not r2["envs"]["written"] and not r2["nodes"]["changed"]                # no churn in Git
        (Path(d) / "envs/hand-made").mkdir()
        (Path(d) / "envs/hand-made/values.yaml").write_text("replicaCount: 1\n")
        stale = Path(d) / "envs/old-svc-us-east"
        stale.mkdir()
        (stale / "values.yaml").write_text(MARKER + "\nservice: old-svc\nsite: us-east\nreplicaCount: 1\n")
        r3 = write_all(result("asia-launch"), d)
        assert "old-svc-us-east" in r3["envs"]["removed"] and r3["nodes"]["changed"]
        assert (Path(d) / "envs/hand-made/values.yaml").exists()


def test_read_current_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        r = result("normal")
        write_all(r, d)
        pods, nodes = read_current(d)
        assert nodes == r["nodes"]
        assert pods == {(s, j): n for s, p in r["placement"].items() for j, n in p["pods"].items()}


def test_bad_results_never_touch_the_repo():
    with tempfile.TemporaryDirectory() as d:
        write_all(result("normal"), d)
        before = sorted(p.name for p in (Path(d) / "envs").iterdir())
        for bad in (result("normal", budget=0.05), result("peak", solver=solve_greedy, budget=0.5, slo_mult=1.0)):
            if bad["status"] == "ok":
                continue
            try:
                write_all(bad, d)
                assert False, "should have refused"
            except ExportError:
                pass
        assert sorted(p.name for p in (Path(d) / "envs").iterdir()) == before


def test_node_plan_outside_lab_limits_is_refused():
    r = result("normal")
    r["nodes"] = {"us-east": 5, "eu-west": 1, "ap-southeast": 1}
    try:
        plan_nodes(r)
        assert False
    except ExportError:
        pass


def test_summary_mentions_new_nodes_and_ansible_step():
    before_pods, before_nodes = {}, {"us-east": 1, "eu-west": 1, "ap-southeast": 1}
    md = summary_markdown(result("asia-launch"), before_pods, before_nodes)
    assert "join-new-nodes.sh" in md and "| ap-southeast | 1 | 2 |" in md
    md2 = summary_markdown(result("normal"), before_pods, {"us-east": 4, "eu-west": 4, "ap-southeast": 4})   # any plan has fewer
    assert "REMOVED" in md2 and "drain" in md2


def _git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_remote(tmp):
    remote, repo = Path(tmp) / "remote.git", Path(tmp) / "config-repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(repo, "config", k, v)
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


def test_propose_creates_branch_pushes_and_opens_pr_then_returns_to_main():
    with tempfile.TemporaryDirectory() as tmp:
        repo, remote = _repo_with_remote(tmp)
        calls = []

        def run(args, cwd=None):
            if args[0] == "gh":
                calls.append((args, cwd))
                return types.SimpleNamespace(stdout="https://github.com/u/config-repo/pull/7\n")
            return default_run(args, cwd)

        branch, url = propose(result("asia-launch"), repo, run=run)
        assert branch.startswith("placement/asia-launch-") and url.endswith("/pull/7")
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"                  # back on main
        assert not (repo / NODES_FILE).exists()                                           # main untouched
        assert branch in _git(remote, "branch", "--list")                                 # pushed
        shown = _git(repo, "show", f"{branch}:{NODES_FILE}")
        assert json.loads(shown)["node_counts"]["ap-southeast"] == 2
        args, cwd = calls[0]
        assert "--base" in args and "main" in args and "join-new-nodes.sh" in args[args.index("--body") + 1]
        assert str(cwd) == str(repo)


def test_propose_refuses_dirty_repo_and_skips_when_nothing_changes():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = _repo_with_remote(tmp)
        (repo / "dirty.txt").write_text("x")
        try:
            propose(result("normal"), repo, run=lambda a, cwd=None: default_run(a, cwd))
            assert False
        except ExportError:
            pass
        (repo / "dirty.txt").unlink()
        write_all(result("normal"), repo)
        _git(repo, "add", "-A"); _git(repo, "commit", "-m", "apply normal"); _git(repo, "push")
        assert propose(result("normal"), repo, run=lambda a, cwd=None: default_run(a, cwd)) == (None, None)
        assert _git(repo, "branch", "--list").count("placement/") == 0                    # no empty branch left


def test_cli_dry_run_writes_nothing_and_commit_mode_commits_once():
    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = _repo_with_remote(tmp)
        assert export_main(["--config-repo", str(repo), "--scenario", "asia-launch", "--solver", "exhaustive", "--dry-run"]) == 0
        assert not (repo / "envs").exists()
        args = ["--config-repo", str(repo), "--scenario", "asia-launch", "--solver", "exhaustive", "--commit"]
        assert export_main(args) == 0 and export_main(args) == 0
        assert _git(repo, "rev-list", "--count", "HEAD") == "2"                           # init + one commit
        assert export_main(["--config-repo", str(repo), "--budget", "0.05", "--solver", "exhaustive"]) == 2


def test_metrics_text_format():
    metrics.record(result("asia-launch"), 0.15, 0.01)
    text = metrics.render()
    assert 'placement_nodes{site="ap-southeast"} 2' in text
    assert "# TYPE placement_cost_dollars_per_hour gauge" in text
    assert 'placement_solves_total{solver="exhaustive",status="ok"}' in text
    assert "placement_memory_capacity_mi" in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
