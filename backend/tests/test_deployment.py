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
    assert refs[-1] == {"configMapRef": {"name": "sentinel-config"}}
    assert {"secretRef": {"name": "sentinel-runtime"}} in refs
    assert all(r.get("secretRef", {}).get("name") != "sentinel-migration" for r in refs)
    gateway = next(
        d for d in deployments if d["metadata"]["name"] == "sentinel-gateway"
    )
    assert gateway["spec"]["template"]["spec"]["containers"][0]["readinessProbe"][
        "httpGet"
    ] == {
        "path": "/ready",
        "port": 8081,
        "httpHeaders": [{"name": "Host", "value": "sentinel.example"}],
    }
    gateway_service = next(
        d
        for d in items
        if d["kind"] == "Service" and d["metadata"]["name"] == "sentinel-gateway"
    )
    assert all(port["port"] != 8081 for port in gateway_service["spec"]["ports"])
    nginx = next(
        d
        for d in items
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"] == "sentinel-gateway-config"
    )["data"]["nginx.conf"]
    assert "location ^~ /api/health { return 404; }" in nginx
    assert (
        len(
            gateway["spec"]["template"]["metadata"]["annotations"][
                "sentinel/config-sha256"
            ]
        )
        == 64
    )
    job = next(d for d in items if d["kind"] == "Job")
    assert {"secretRef": {"name": "sentinel-migration"}} in job["spec"]["template"][
        "spec"
    ]["containers"][0]["envFrom"]
    edge = next(x for x in items if x["kind"] == "Gateway")
    assert edge["spec"]["gatewayClassName"] == "sentinel"
    assert edge["spec"]["listeners"][1]["tls"]["certificateRefs"] == [
        {"group": "", "kind": "Secret", "name": "sentinel-tls"}
    ]
    assert all(
        l["allowedRoutes"]["namespaces"]["from"] == "Same"
        for l in edge["spec"]["listeners"]
    )
    assert not any(x["kind"] in ("Ingress", "GatewayClass") for x in items)
    redirect = next(
        x
        for x in items
        if x["kind"] == "HTTPRoute"
        and x["metadata"]["name"] == "sentinel-http-redirect"
    )
    assert redirect["spec"]["rules"][0]["filters"][0]["requestRedirect"] == {
        "scheme": "https",
        "port": 443,
        "statusCode": 301,
    }
    secure = next(
        x
        for x in items
        if x["kind"] == "HTTPRoute" and x["metadata"]["name"] == "sentinel-https"
    )
    assert secure["spec"]["hostnames"] == ["sentinel.example"]
    assert secure["spec"]["parentRefs"][0]["sectionName"] == "https"
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


@pytest.mark.parametrize(
    "host",
    [
        "a..example",
        "-a.example",
        "a-.example",
        "a." + "b" * 64 + ".example",
        "127.0.0.1",
        "https://site.example",
    ],
)
def test_invalid_gateway_host_is_rejected_before_rendering(host):
    with pytest.raises(ValueError):
        module.render(
            host,
            dict.fromkeys(
                ("backend", "frontend", "gateway"), "registry/app@sha256:" + "a" * 64
            ),
        )


def test_custom_gateway_class_is_explicit_and_namespace_must_be_valid():
    images = dict.fromkeys(
        ("backend", "frontend", "gateway"), "registry/app@sha256:" + "a" * 64
    )
    items = module.render("app.example", images, gateway_class="platform-gateway")
    assert (
        next(x for x in items if x["kind"] == "Gateway")["spec"]["gatewayClassName"]
        == "platform-gateway"
    )
    with pytest.raises(ValueError):
        module.render("app.example", images, namespace="invalid-")
    with pytest.raises(ValueError):
        module.render("app.example", images, gateway_class="../../other")
