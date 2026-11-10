# Service portal (self-service onboarding)

`/portal` lets a developer add a service without touching Kubernetes or Git:
name, image, port, SLO, capacity per pod, RAM per pod, requests per active user, minimum replicas.

1. **Validate**: name is DNS-safe and not a system namespace; image from an allowed registry with a real tag
   (the same rules as the guardrail policies); numbers inside sane ranges.
2. **Optimise**: the whole platform is solved with and without the new service (`Preview`). If nothing fits the budget
   or the lab's node limit the request is refused, with the reason.
3. **Pull request** in the config-repo: `catalog/services/<name>.json` (model parameters), `apps/<name>/values.yaml`
   (image), re-optimised `envs/` (pods) and `infra/terraform/nodes.auto.tfvars.json` (nodes). The body shows nodes,
   pods and cost before/after.
4. A platform engineer reviews and **merges**. ArgoCD deploys the pods; if nodes were added the Terraform workflow
   creates them (then `infra/ansible/join-new-nodes.sh`).

The catalog is data in the config-repo, so adding a service needs no new image. The optimiser CLI
(`export_placement.py --config-repo ...`) and the pod both read it, so later optimiser runs include the service.

Configuration (environment of the optimiser pod): `PORTAL_REPO=owner/config-repo`, `GITHUB_TOKEN` (fine-grained token
with Contents and Pull requests read/write on that repo, from the `portal-github` Secret). Without them `Preview`
still works and `Open pull request` is disabled. The portal never merges and never talks to the cluster.
