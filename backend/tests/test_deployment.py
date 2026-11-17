"""Structural release invariants plus API-server schema validation in CI."""

import importlib.util
from pathlib import Path
import pytest
from app.core.settings import validate_production_transport

spec = importlib.util.spec_from_file_location(
    "sentinel_deploy",
    Path(__file__).resolve().parents[2] / "deploy/kubernetes/render.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_manifest_separates_secrets_pins_images_and_survives_one_pod_disruption():
    items = module.render(
        "sentinel.example",
        dict.fromkeys(
            ("backend", "frontend", "gateway"),
            "registry.example/app@sha256:" + "a" * 64,
        ),
    )
    deployments = [x for x in items if x["kind"] == "Deployment"]
    assert len(deployments) == 3
    for d in deployments:
        assert d["spec"]["replicas"] == 2
        assert d["spec"]["strategy"]["rollingUpdate"] == {
            "maxSurge": 0,
            "maxUnavailable": 1,
        }
        pod = d["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["affinity"]["podAntiAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]
        c = pod["containers"][0]
        assert c["securityContext"]["allowPrivilegeEscalation"] is False
        assert c["securityContext"]["readOnlyRootFilesystem"]
        assert "livenessProbe" in c and "readinessProbe" in c
    api = next(d for d in deployments if d["metadata"]["name"] == "sentinel-api")
    refs = api["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert {"secretRef": {"name": "sentinel-runtime"}} in refs
    assert all(r.get("secretRef", {}).get("name") != "sentinel-migration" for r in refs)
    job = next(d for d in items if d["kind"] == "Job")
    assert {"secretRef": {"name": "sentinel-migration"}} in job["spec"]["template"][
        "spec"
    ]["containers"][0]["envFrom"]
    assert next(x for x in items if x["kind"] == "Ingress")["spec"]["tls"][0][
        "hosts"
    ] == ["sentinel.example"]
    with pytest.raises(ValueError):
        module.render(
            "sentinel.example",
            dict.fromkeys(("backend", "frontend", "gateway"), "app:latest"),
        )


def test_production_transports_reject_unencrypted_or_unverified_dependencies():
    good = (
        "postgresql://app:secret@db/sentinel?sslmode=verify-full&sslrootcert=/ca/db.pem"
    )
    validate_production_transport(good, "rediss://:secret@cache:6380/0")
    for db, redis in [
        (good, "redis://cache/0"),
        (good, "rediss://:secret@cache/0?ssl_check_hostname=false"),
        (good, "rediss://:secret@cache/0?ssl_cert_reqs=none"),
        ("postgresql://app:secret@db/sentinel", "rediss://:secret@cache/0"),
    ]:
        with pytest.raises(RuntimeError):
            validate_production_transport(db, redis)
