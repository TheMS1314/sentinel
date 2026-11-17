"""Keep the destructive CI harness isolated and preserve production deployment invariants."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
import pytest

spec = importlib.util.spec_from_file_location(
    "kubernetes_smoke",
    Path(__file__).resolve().parents[2] / "scripts/kubernetes_acceptance.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_application_acceptance_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("SENTINEL_KUBERNETES_ACCEPTANCE", raising=False)
    with patch.object(module.edge, "kube") as kube:
        with pytest.raises(SystemExit):
            module.main()
        kube.assert_not_called()


def test_application_acceptance_refuses_reusing_namespace_before_any_mutation(
    monkeypatch,
):
    monkeypatch.setenv("SENTINEL_KUBERNETES_ACCEPTANCE", "1")
    with (
        patch.object(
            module.edge,
            "kube",
            side_effect=[
                b"https://127.0.0.1:6443",
                json.dumps(
                    dict(items=[dict(metadata=dict(name=module.NAMESPACE))])
                ).encode(),
            ],
        ) as kube,
        patch.object(module.edge, "apply") as apply,
    ):
        with pytest.raises(RuntimeError, match="fresh namespace"):
            module.main()
        assert kube.call_count == 2
        assert all(call.args[0] in ("config", "get") for call in kube.call_args_list)
        apply.assert_not_called()


def test_application_overrides_preserve_replica_security_and_runtime_separation():
    resources = module.application_resources(
        dict(
            backend="sentinel-ci-backend:" + "a" * 40,
            frontend="sentinel-ci-frontend:" + "a" * 40,
            gateway="registry.example/nginx@sha256:" + "b" * 64,
        )
    )
    for row in resources:
        if row["kind"] == "Deployment":
            assert row["spec"]["replicas"] == 2
            assert row["spec"]["strategy"]["rollingUpdate"] == dict(
                maxSurge=0, maxUnavailable=1
            )
            pod = row["spec"]["template"]["spec"]
            assert pod["affinity"]["podAntiAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]
            c = pod["containers"][0]
            assert c["securityContext"]["readOnlyRootFilesystem"]
            assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
            assert c["readinessProbe"] and c["livenessProbe"]
            if row["metadata"]["name"] == "sentinel-api":
                assert c["envFrom"][-1] == dict(secretRef=dict(name="sentinel-runtime"))
                assert c["imagePullPolicy"] == "Never"
        if row["kind"] == "ConfigMap" and row["metadata"]["name"] == "sentinel-config":
            assert row["data"]["SENTINEL_ENV"] == "development"
            assert row["data"]["SENTINEL_REQUIRE_RESTRICTED_DB"] == "1"
            assert row["data"]["SENTINEL_REQUIRE_MIGRATIONS"] == "1"


def test_application_acceptance_rejects_arbitrary_local_images():
    with pytest.raises(ValueError):
        module.application_resources(
            dict(
                backend="sentinel-backend:latest",
                frontend="sentinel-ci-frontend:" + "a" * 40,
                gateway="unused",
            )
        )
