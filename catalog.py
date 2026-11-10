"""Service catalog: services onboarded through the portal.

Each service is one file, config-repo/catalog/services/<name>.json. The optimiser reads the catalog and adds those
services to its model (next to the built-in ones in data.SERVICES), so a new service gets a placement, memory and
node capacity exactly like the others. The portal writes the file; nothing here needs a rebuild of the image.
"""
import json
import os
import re
import warnings
from pathlib import Path

from data import SERVICES

CATALOG_REL = Path("catalog/services")
NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$")      # <= 40 chars; the namespace and "<name>-<site>" derive from it
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.@-]{0,59}$")
# The service name becomes a namespace: these must never be taken over by a service.
RESERVED = {"default", "argocd", "monitoring", "placement-optimiser", "kube-system", "kube-public", "kube-node-lease"}
# Keep in sync with the guardrail-image-registry policy (platform/guardrails/policies.yaml).
ALLOWED_IMAGE_PREFIXES = tuple(p for p in os.environ.get(
    "ALLOWED_IMAGE_PREFIXES", "ghcr.io/quantran2510/,docker.io/nginxinc/").split(",") if p)
LIMITS = {"slo_ms": (10, 1000), "capacity": (1, 5000), "mem_mi": (16, 1024),
          "rps_per_active_user": (0.001, 10), "min_replicas": (1, 6), "port": (1, 65535)}
MODEL_KEYS = ("slo_ms", "capacity", "mem_mi", "rps_per_active_user", "min_replicas")


def normalize(spec):
    """Coerce the form values to the right types and fill defaults. Bad values are kept as-is for validate()."""
    out = dict(spec)
    image = out.get("image") or {}
    out["image"] = {"repository": str(image.get("repository", "")).strip(), "tag": str(image.get("tag", "")).strip()}
    out["name"] = str(out.get("name", "")).strip()
    out["owner"] = str(out.get("owner", "")).strip()
    out["health_path"] = str(out.get("health_path") or "/").strip()
    out.setdefault("port", 8080)
    for key in LIMITS:
        if key in out:
            try:
                v = float(out[key])
                out[key] = int(v) if key in ("slo_ms", "mem_mi", "min_replicas", "port", "capacity") and v == int(v) else v
            except (TypeError, ValueError):
                pass
    return out


def validate(spec, taken_names=()):
    """List of human-readable problems; empty = the service may be onboarded."""
    errors = []
    name = spec.get("name", "")
    if not NAME_RE.match(name):
        errors.append("name: use 1-40 characters, lowercase letters, digits and '-', starting with a letter and not ending with '-'")
    elif name in RESERVED or name.startswith("kube-"):
        errors.append(f"name: '{name}' is reserved (it would become a system namespace)")
    elif name in set(taken_names):
        errors.append(f"name: '{name}' already exists")
    for key, (lo, hi) in LIMITS.items():
        v = spec.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"{key}: must be a number")
        elif not lo <= v <= hi:
            errors.append(f"{key}: must be between {lo} and {hi}")
    for key in ("slo_ms", "capacity", "mem_mi", "min_replicas", "port"):
        v = spec.get(key)
        if isinstance(v, float) and v != int(v):
            errors.append(f"{key}: must be a whole number")
    repo, tag = spec["image"]["repository"], spec["image"]["tag"]
    if not repo or any(c.isspace() for c in repo):
        errors.append("image.repository: required")
    elif not repo.startswith(ALLOWED_IMAGE_PREFIXES):
        errors.append("image.repository: must start with " + " or ".join(ALLOWED_IMAGE_PREFIXES))
    if not TAG_RE.match(tag):
        errors.append("image.tag: required (letters, digits, '_', '.', '-')")
    elif tag == "latest":
        errors.append("image.tag: 'latest' is not allowed")
    if not spec.get("health_path", "").startswith("/"):
        errors.append("health_path: must start with '/'")
    if spec.get("owner") and not OWNER_RE.match(spec["owner"]):
        errors.append("owner: letters, digits, space and _ . @ - only (max 60)")
    return errors


def model_spec(spec):
    """The part of a catalog entry that the placement model needs."""
    return {k: spec[k] for k in MODEL_KEYS}


def load_catalog(directory):
    """{name: full spec} from <directory>/*.json. Broken files are skipped with a warning, never fatal."""
    out = {}
    d = Path(directory)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            spec = normalize(json.loads(f.read_text()))
            problems = validate(spec, taken_names=())
            if problems or spec["name"] != f.stem:
                raise ValueError("; ".join(problems) or f"name does not match file name {f.name}")
            out[spec["name"]] = spec
        except Exception as e:                                   # noqa: BLE001 - one bad file must not stop the optimiser
            warnings.warn(f"catalog: ignoring {f.name}: {e}")
    return out


def merged_services(catalog):
    """Built-in services plus the onboarded ones, in the shape build_instance() expects."""
    return {**SERVICES, **{name: model_spec(spec) for name, spec in catalog.items()}}


def catalog_json(spec):
    keys = ["name", "owner", "slo_ms", "capacity", "mem_mi", "rps_per_active_user", "min_replicas", "image", "port", "health_path"]
    return json.dumps({k: spec[k] for k in keys}, indent=2) + "\n"


def app_values_yaml(spec):
    """apps/<name>/values.yaml: what the Helm chart needs besides the placement (replicas, node, RAM)."""
    return (f"# Created by the portal for service '{spec['name']}' (owner: {spec['owner'] or '-'}).\n"
            f"# The service team / its CI owns image.tag. Placement lives in envs/ and is written by the optimiser.\n"
            f"image:\n"
            f"  repository: {spec['image']['repository']}\n"
            f"  tag: \"{spec['image']['tag']}\"\n"
            f"containerPort: {spec['port']}\n"
            f"probes:\n"
            f"  path: {spec['health_path']}\n")
