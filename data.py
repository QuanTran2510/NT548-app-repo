"""Simulated world: sites (t3.small nodes), services, latency matrix, and traffic scenarios.

Chain used everywhere:
  users per region x active_ratio x requests-per-active-user  =  demand (req/s)
  demand / (pod capacity x target utilisation)                =  pods
  pods x RAM request                                          =  node memory needed
  nodes x t3.small price                                      =  cost
"""
import json
import os
from pathlib import Path

REGIONS = ["us-east", "eu-west", "ap-southeast"]   # client regions AND candidate sites

# Round-trip latency (ms): client region -> site  (model parameters, NOT measured on the cluster)
LATENCY = {
    "us-east":      {"us-east": 20,  "eu-west": 85,  "ap-southeast": 220},
    "eu-west":      {"us-east": 85,  "eu-west": 20,  "ap-southeast": 165},
    "ap-southeast": {"us-east": 220, "eu-west": 165, "ap-southeast": 20},
}

# One site = a group of identical t3.small nodes (2 vCPU, 2 GiB).
#   node_price   $/hour of ONE node (Linux on-demand; check AWS Pricing Calculator before quoting)
#   node_mem_mi  RAM per node that pods may request (2 GiB minus OS, k3s, kube-proxy, flannel)
#   reserved_mi  RAM already requested by non-optimiser workloads on the site's FIRST node
#                (us-east: control plane + ArgoCD; eu-west: the optimiser pod). Measure with
#                `kubectl describe node <node> | grep -A8 "Allocated resources"`.
#   min_nodes    the lab always has one node per site (Terraform validation requires >= 1)
#   max_nodes    per-site cap (also enforced by terraform/variables.tf)
SITES = {
    "us-east":      {"node_price": 0.0208, "node_mem_mi": 1300, "reserved_mi": 700, "min_nodes": 1, "max_nodes": 4},
    "eu-west":      {"node_price": 0.0228, "node_mem_mi": 1300, "reserved_mi": 150, "min_nodes": 1, "max_nodes": 4},
    "ap-southeast": {"node_price": 0.0264, "node_mem_mi": 1300, "reserved_mi": 0,   "min_nodes": 1, "max_nodes": 4},
}
MAX_TOTAL_NODES = 9          # AWS Academy Learner Lab: at most 9 running instances

# Service keys are DNS-1123 names: they become Kubernetes names and config-repo folder names.
#   capacity              req/s one pod can serve (assumption; measure with a load test later)
#   mem_mi                memory request/limit of one pod, written into envs/*/values.yaml
#   rps_per_active_user   requests per second sent by one ACTIVE user to this service
#   min_replicas          pods across all sites (avoid a single point of failure)
SERVICES = {
    "realtime-chat": {"slo_ms": 50,  "capacity": 200, "mem_mi": 128, "rps_per_active_user": 0.5, "min_replicas": 2},
    "web-api":       {"slo_ms": 100, "capacity": 80,  "mem_mi": 192, "rps_per_active_user": 0.2, "min_replicas": 2},
    "analytics":     {"slo_ms": 250, "capacity": 100, "mem_mi": 256, "rps_per_active_user": 0.1, "min_replicas": 2},
}

UTILIZATION = 0.7        # plan pods to run at <= 70 % of capacity (headroom for bursts)
DEFAULT_BUDGET = 0.15    # $/hour for ALL nodes (about 6 t3.small)
LAMBDA = 0.002           # $/hour charged per ms of demand-weighted mean latency (10 ms ~ 1 node)
POD_OVERHEAD = 0.0005    # $/hour per pod (monitoring, scheduling): breaks ties, avoids useless pods

SCENARIO_DIR = Path(__file__).parent / "scenarios"


def list_scenarios():
    out = {}
    for f in sorted(SCENARIO_DIR.glob("*.json")):
        out[f.stem] = json.loads(f.read_text())
    return out


def load_scenario(name_or_path):
    """name ("normal") -> scenarios/normal.json, or a path to any .json file."""
    p = Path(name_or_path)
    if not (p.suffix == ".json" and os.path.exists(p)):
        p = SCENARIO_DIR / f"{name_or_path}.json"
    sc = json.loads(p.read_text())
    sc.setdefault("name", p.stem)
    missing = [r for r in REGIONS if r not in sc["users"]]
    if missing:
        raise ValueError(f"scenario {sc['name']}: no users for {missing}")
    return sc
