# placement-optimiser

Decides **how many pods of each service to run at each site, and how many t3.small nodes each site
needs**, from a traffic scenario (users per region). The result is written to a GitOps config-repo and
proposed as a pull request; ArgoCD deploys the pods, Terraform adds the nodes.

## Model (see `data.py`, `model.py`)
```
users[region] x active_ratio x requests-per-active-user[service]  = demand (req/s, per region and service)
pods   = ceil( load_at_site / (pod capacity x 70 % utilisation) )   (at least min_replicas per service)
nodes  = ceil( (RAM requested by pods + reserved) / node RAM )      (1..4 per site, 9 in total)
cost   = sum( t3.small price of the site x nodes )                   <= budget ($/h)
minimise  cost + 0.0005 $/pod + 0.002 $/ms x mean latency
```
Pods are free until a node is full, so cost rises in **steps**. Three solvers: `ilp` (PuLP/CBC, may split
a region's traffic between sites), `exhaustive` (exact for nearest-site routing), `greedy` (covers regions
first, cannot pack different services onto shared nodes). **Routing is part of the model only; it is not deployed.**

Everything that is an assumption lives in `data.py`: latency matrix, pod capacity, RAM per pod, requests per
user, node RAM and `reserved_mi` (measure with `kubectl describe node <n> | grep -A8 "Allocated resources"`),
t3.small prices per region (verify on the AWS Pricing Calculator).

## Scenarios (`scenarios/*.json`)
| Scenario | Users (us / eu / ap) | Active | ILP nodes (us/eu/ap) |
|---|---|---|---|
| normal | 3000 / 2000 / 1000 | 10 % | 1 / 1 / 1 (the 3 nodes the lab has) |
| asia-launch | 3000 / 2000 / 6000 | 15 % | 1 / 1 / 2 |
| peak | 6000 / 4000 / 2000 | 25 % | 3 / 2 / 2 |
| night | 750 / 500 / 250 | 3 % | 1 / 1 / 1 |

## Results (objective = cost + pod overhead + 0.002 x latency; lower is better; budget $0.18/h)
| Scenario | greedy | exhaustive | ilp |
|---|---|---|---|
| normal | 0.1599 (4 nodes) | 0.1363 (4 nodes) | 0.1237 (3 nodes) |
| asia-launch | 0.2023 (5 nodes) | 0.1672 (5 nodes) | 0.1542 (4 nodes) |
| peak | over_capacity | 0.2222 (7 nodes) | 0.2186 (7 nodes) |
| night | 0.1589 (4 nodes) | 0.1221 (3 nodes) | 0.1221 (3 nodes) |

ILP needs fewer nodes because it may split a region's traffic between sites and so packs pods tighter.
The ILP column was computed with an independent HiGHS solver while developing; regenerate it with CBC:
`python run_sensitivity.py --scenario normal ilp exhaustive greedy`.

## Memory calibration (`reserved_mi` in `data.py`)
`reserved_mi` is the RAM of a site's first node that pods must not use. It was calibrated on the live cluster:
`kubectl describe node` showed requests of 588 Mi (us-east) and 1024 Mi (eu-west), matching the model's own
accounting (pods + system / optimiser pod); `kubectl top nodes` showed 1259 Mi really used on the control-plane node
and about 330 Mi of ArgoCD on the ap-southeast node. Values: us-east 850, eu-west 150, ap-southeast 700.
ap-southeast also hosts the monitoring stack (Prometheus 200 Mi, Grafana 128 Mi, kube-state-metrics 48 Mi, ...),
which is why its reserve is 700 and the default budget went from $0.15 to $0.18/h (peak needs 7 nodes).

## Run
    python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
    python tests/test_model.py && python tests/test_exporter.py
    python app.py                                  # http://127.0.0.1:5000  (UI, /metrics, /api/*)

## Export to the config-repo
    python export_placement.py --config-repo ~/config-repo --scenario asia-launch --solver ilp --dry-run
    python export_placement.py --config-repo ~/config-repo --scenario asia-launch --solver ilp --commit   # local commit
    python export_placement.py --config-repo ~/config-repo --scenario asia-launch --solver ilp --pr       # branch + push + PR (needs `gh`)

It writes `envs/<service>-<site>/values.yaml` (replicas, nodeSelector, memory request) and
`infra/terraform/nodes.auto.tfvars.json` (node counts). It refuses `infeasible`, `over_budget` and
`over_capacity` results, and only deletes folders carrying the GENERATED marker.

Other API: `GET /api/solve`, `/api/plan` (the files a PR would contain), `/api/sensitivity?param=...`,
`/api/scenarios`, `/metrics` (Prometheus), `/healthz`. Query args: `scenario, solver, demand_mult, ap_demand,
active_ratio, cost_mult, slo_mult, budget`.

## Docker / CI
`Dockerfile` runs as user 10001 with one gunicorn worker (`/metrics` keeps state in memory).
`.github/workflows/ci.yml`: tests, build, non-root check, smoke test, Trivy, push to GHCR, bump the image tag in config-repo.
