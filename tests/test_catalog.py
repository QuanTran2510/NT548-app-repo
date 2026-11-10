import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import (app_values_yaml, catalog_json, load_catalog, merged_services, model_spec, normalize, validate)
from data import REGIONS, SERVICES
from model import build_instance, solve_exhaustive

GOOD = {"name": "shop-cart", "owner": "team-shop",
        "image": {"repository": "docker.io/nginxinc/nginx-unprivileged", "tag": "1.27-alpine"}, "port": 8080,
        "health_path": "/", "slo_ms": 150, "capacity": 120, "mem_mi": 96, "rps_per_active_user": 0.1, "min_replicas": 2}


def errs(**changes):
    spec = {**GOOD, **changes}
    return validate(normalize(spec), taken_names={"web-api"})


def test_good_spec_is_accepted_and_form_strings_are_coerced():
    assert errs() == []
    spec = normalize({**GOOD, "slo_ms": "150", "mem_mi": "96", "rps_per_active_user": "0.1", "min_replicas": "2", "port": "8080"})
    assert validate(spec) == [] and spec["slo_ms"] == 150 and spec["rps_per_active_user"] == 0.1


def test_names_are_dns_safe_not_reserved_and_not_taken():
    for bad in ("Shop", "shop_cart", "1shop", "shop-", "", "a" * 41, "kube-thing"):
        assert any(e.startswith("name") for e in errs(name=bad)), bad
    for reserved in ("monitoring", "argocd", "default", "placement-optimiser"):
        assert any("reserved" in e for e in errs(name=reserved)), reserved
    assert any("already exists" in e for e in errs(name="web-api"))


def test_image_rules_mirror_the_guardrails():
    assert any("latest" in e for e in errs(image={"repository": GOOD["image"]["repository"], "tag": "latest"}))
    assert any("tag" in e for e in errs(image={"repository": GOOD["image"]["repository"], "tag": ""}))
    assert any("must start with" in e for e in errs(image={"repository": "docker.io/library/busybox", "tag": "1.36"}))
    assert errs(image={"repository": "ghcr.io/quantran2510/shop", "tag": "abc1234"}) == []


def test_numbers_are_range_checked():
    assert any(e.startswith("mem_mi") for e in errs(mem_mi=5000))
    assert any(e.startswith("capacity") for e in errs(capacity=0))
    assert any(e.startswith("slo_ms") for e in errs(slo_ms=5))
    assert any(e.startswith("min_replicas") for e in errs(min_replicas=0))
    assert any(e.startswith("port") for e in errs(port=70000))
    assert any(e.startswith("slo_ms") for e in errs(slo_ms="fast"))
    assert any("whole number" in e for e in errs(mem_mi=96.5))


def test_files_roundtrip_and_values_match_the_chart_keys():
    spec = normalize(GOOD)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "shop-cart.json").write_text(catalog_json(spec))
        assert load_catalog(d) == {"shop-cart": spec}
    text = app_values_yaml(spec)
    assert "repository: docker.io/nginxinc/nginx-unprivileged" in text and 'tag: "1.27-alpine"' in text
    assert "containerPort: 8080" in text and "path: /" in text


def test_broken_catalog_files_are_skipped_not_fatal():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "good.json").write_text(catalog_json(normalize({**GOOD, "name": "good"})))
        (Path(d) / "broken.json").write_text("{not json")
        (Path(d) / "wrong-name.json").write_text(catalog_json(normalize(GOOD)))       # file name differs from "name"
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cat = load_catalog(d)
        assert list(cat) == ["good"] and len(w) == 2
    assert load_catalog("/nonexistent") == {}


def test_onboarded_service_joins_the_model():
    svc = merged_services({"shop-cart": normalize(GOOD)})
    assert set(svc) == set(SERVICES) | {"shop-cart"} and svc["shop-cart"] == model_spec(normalize(GOOD))
    inst = build_instance("normal", services=svc)
    assert inst["services"]["shop-cart"]["demand"]["us-east"] == 3000 * 0.10 * 0.1     # users x active x rps
    r = solve_exhaustive(inst)
    assert r["status"] == "ok" and "shop-cart" in r["placement"]
    assert sum(r["placement"]["shop-cart"]["pods"].values()) >= 2                       # min_replicas
    for j in REGIONS:
        assert r["memory"][j]["used_mi"] <= r["memory"][j]["capacity_mi"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
